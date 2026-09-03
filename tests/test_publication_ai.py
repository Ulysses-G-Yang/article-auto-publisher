"""DeepSeek 只读发布建议的配置、客户端与 HTTP 契约测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from flask import Flask

from account_sessions.web import create_account_session_blueprint
from content_studio.database import sqlite_url
from content_studio.web import create_content_studio_blueprint
from publication_ai.contracts import (
    PublicationAIModelListRequest,
    PublicationAISettingsUpdate,
    PublicationGuidanceResponse,
)
from publication_ai.deepseek import DeepSeekPublicationAdvisor, PublicationAISettings
from publication_ai.errors import PublicationAIError
from publication_ai.settings_store import PublicationAISettingsStore


def run(coroutine):
    return asyncio.run(coroutine)


def settings(**overrides) -> PublicationAISettings:
    values = {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 90,
        "max_output_tokens": 2000,
    }
    values.update(overrides)
    return PublicationAISettings.from_mapping(values)


def guidance_payload(platforms=("xiaoheihe",)) -> dict:
    return {
        "recommendations": [
            {
                "platform": platform,
                "topic_queries": ["显示器"],
                "suggested_topics": ["显示器选购"],
                "suggested_community": "硬件交流",
                "keywords": ["显示器", "桌面"],
                "reason": "主题与平台的硬件讨论场景匹配。",
            }
            for platform in platforms
        ]
    }


def upstream_response(platforms=("xiaoheihe",)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            guidance_payload(platforms), ensure_ascii=False
                        )
                    }
                }
            ]
        },
    )


def test_ai_config_defaults_disabled_and_strips_yaml_secret(tmp_path: Path, monkeypatch) -> None:
    import config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "ai:\n  publication_guidance:\n"
        "    api_key: must-not-survive\n"
        "    apiKey: must-not-survive-either\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("ARTICLEOPS_AI_GUIDANCE_ENABLED", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config._config = None
    try:
        loaded = config.load_config()
        guidance = loaded["ai"]["publication_guidance"]
        assert guidance["enabled"] is False
        assert guidance["base_url"] == "https://api.deepseek.com"
        assert "api_key" not in guidance
        assert "apiKey" not in guidance
        assert "must-not-survive" not in repr(loaded)
    finally:
        config._config = None


def test_ai_config_environment_overrides_and_rejects_insecure_url(monkeypatch) -> None:
    import config

    monkeypatch.setenv("ARTICLEOPS_AI_GUIDANCE_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "1234")
    config._config = None
    try:
        loaded = config.load_config()
        assert loaded["ai"]["publication_guidance"]["model"] == "deepseek-v4-pro"
        assert loaded["ai"]["publication_guidance"]["max_output_tokens"] == 1234
    finally:
        config._config = None

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://api.deepseek.com")
    with pytest.raises(ValueError, match="HTTPS"):
        config.load_config()
    config._config = None


def test_settings_rejects_credentialed_or_non_https_url() -> None:
    for base_url in ("http://api.deepseek.com", "https://user:pass@example.com"):
        with pytest.raises(PublicationAIError) as captured:
            settings(base_url=base_url)
        assert captured.value.error_code == "AI_CONFIGURATION_ERROR"


def test_client_is_disabled_or_unconfigured_without_network(monkeypatch) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return upstream_response()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    advisor = DeepSeekPublicationAdvisor(settings(enabled=False), http_client=client)
    with pytest.raises(PublicationAIError) as disabled:
        run(advisor.advise("标题", "正文", ["xiaoheihe"]))
    assert disabled.value.error_code == "AI_GUIDANCE_DISABLED"

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    advisor = DeepSeekPublicationAdvisor(settings(), http_client=client)
    with pytest.raises(PublicationAIError) as missing:
        run(advisor.advise("标题", "正文", ["xiaoheihe"]))
    assert missing.value.error_code == "AI_CONFIGURATION_ERROR"
    assert called is False
    run(client.aclose())


def test_client_sends_full_text_once_and_returns_strict_order(monkeypatch) -> None:
    secret = "deepseek-test-secret"
    sentinel = "FULL_ARTICLE_SENTINEL_全文"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is False
        assert sentinel in payload["messages"][1]["content"]
        assert request.headers["Authorization"] == f"Bearer {secret}"
        # 上游顺序不可信，本地必须恢复请求顺序。
        return upstream_response(("zol", "xiaoheihe"))

    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    advisor = DeepSeekPublicationAdvisor(settings(), http_client=client)
    result = run(advisor.advise("标题", sentinel, ["xiaoheihe", "zol"]))
    assert [item.platform for item in result.recommendations] == ["xiaoheihe", "zol"]
    assert len(seen) == 1
    assert secret not in repr(advisor)
    assert sentinel not in repr(result)
    run(client.aclose())


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (401, "AI_AUTH_FAILED"),
        (403, "AI_AUTH_FAILED"),
        (429, "AI_RATE_LIMITED"),
        (500, "AI_UPSTREAM_ERROR"),
    ],
)
def test_client_maps_upstream_status_without_leaking_body(
    status: int,
    error_code: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-leak-key")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, text="UPSTREAM_PRIVATE_BODY")
        )
    )
    advisor = DeepSeekPublicationAdvisor(settings(), http_client=client)
    with pytest.raises(PublicationAIError) as captured:
        run(advisor.advise("标题", "BODY_PRIVATE_SENTINEL", ["xiaoheihe"]))
    assert captured.value.error_code == error_code
    rendered = f"{captured.value!s} {captured.value!r}"
    assert "never-leak-key" not in rendered
    assert "BODY_PRIVATE_SENTINEL" not in rendered
    assert "UPSTREAM_PRIVATE_BODY" not in rendered
    run(client.aclose())


def test_client_maps_timeout_invalid_json_and_platform_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout", request=request)

    timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    timeout_advisor = DeepSeekPublicationAdvisor(settings(), http_client=timeout_client)
    with pytest.raises(PublicationAIError) as timeout:
        run(timeout_advisor.advise("标题", "正文", ["xiaoheihe"]))
    assert timeout.value.error_code == "AI_TIMEOUT"
    run(timeout_client.aclose())

    for response in (
        httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]}),
        upstream_response(("zol",)),
    ):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request, value=response: value)
        )
        advisor = DeepSeekPublicationAdvisor(settings(), http_client=client)
        with pytest.raises(PublicationAIError) as invalid:
            run(advisor.advise("标题", "正文", ["xiaoheihe"]))
        assert invalid.value.error_code == "AI_RESPONSE_INVALID"
        run(client.aclose())


class FakeAdvisor:
    model = "fake-deepseek"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    async def advise(self, title: str, body: str, platforms: list[str]):
        self.calls.append((title, body, platforms))
        return PublicationGuidanceResponse.model_validate(guidance_payload(tuple(platforms)))


def make_http_app(tmp_path: Path, advisor: FakeAdvisor) -> tuple[Flask, object, object]:
    app = Flask("publication-ai-test")
    app.secret_key = "test"
    account_url = sqlite_url(tmp_path / "accounts.db")
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=account_url,
            seed_legacy_profiles=False,
            auto_execute=False,
            heartbeat_enabled=False,
        )
    )
    account_state = app.extensions["account_sessions"]
    app.register_blueprint(
        create_content_studio_blueprint(
            account_state=account_state,
            database_url=sqlite_url(tmp_path / "content.db"),
            asset_root=tmp_path / "assets",
            work_root=tmp_path / "work",
            publication_advisor=advisor,
        )
    )
    return app, account_state, app.extensions["content_studio"]


def test_publication_advice_endpoint_is_read_only_and_no_store(tmp_path: Path) -> None:
    advisor = FakeAdvisor()
    app, account_state, studio_state = make_http_app(tmp_path, advisor)
    client = app.test_client()
    created = client.post(
        "/api/content-drafts",
        json={
            "title": "测试标题",
            "blocks": [
                {"type": "text", "text": "第一段", "position": 2},
                {"type": "text", "text": "第二段", "position": 3},
            ],
        },
    ).get_json()
    response = client.post(
        f"/api/content-drafts/{created['draft_id']}/publication-advice",
        json={"revision": created["revision"], "platforms": ["xiaoheihe", "zol"]},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert set(payload) == {
        "draft_id",
        "revision",
        "model",
        "recommendations",
        "generated_at",
    }
    assert advisor.calls == [("测试标题", "第一段\n第二段", ["xiaoheihe", "zol"])]
    after = client.get(f"/api/content-drafts/{created['draft_id']}").get_json()
    assert after == created

    conflict = client.post(
        f"/api/content-drafts/{created['draft_id']}/publication-advice",
        json={"revision": created["revision"] + 1, "platforms": ["xiaoheihe"]},
    )
    assert conflict.status_code == 409
    assert conflict.get_json() == {
        "error": "DRAFT_REVISION_CONFLICT",
        "message": "草稿已更新，请刷新后重新请求 AI 建议",
    }
    assert conflict.headers["Cache-Control"] == "no-store"

    studio_state.close()
    account_state.close()


def test_publication_advice_rejects_oversized_body_before_ai(tmp_path: Path) -> None:
    advisor = FakeAdvisor()
    app, account_state, studio_state = make_http_app(tmp_path, advisor)
    client = app.test_client()
    created = client.post(
        "/api/content-drafts",
        json={
            "title": "超长正文",
            "blocks": [
                {"type": "text", "text": "a" * 100_001, "position": 0},
                {"type": "text", "text": "b" * 100_001, "position": 1},
            ],
        },
    ).get_json()
    response = client.post(
        f"/api/content-drafts/{created['draft_id']}/publication-advice",
        json={"revision": created["revision"], "platforms": ["xiaoheihe"]},
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "AI_INPUT_TOO_LARGE"
    assert advisor.calls == []

    studio_state.close()
    account_state.close()


def test_runtime_settings_store_never_exposes_key_and_falls_back_to_environment() -> None:
    environ = {"DEEPSEEK_API_KEY": "environment-secret"}
    store = PublicationAISettingsStore(
        settings(enabled=False).__dict__,
        environ=environ,
    )
    initial = store.public_view()
    assert initial == {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key_configured": True,
        "api_key_source": "environment",
        "persistence": "process",
    }
    runtime_secret = "runtime-secret-value"
    updated = store.update(
        PublicationAISettingsUpdate.model_validate(
            {
                "enabled": True,
                "base_url": "https://proxy.example.com",
                "model": "deepseek-v4-pro",
                "api_key": runtime_secret,
            }
        )
    )
    assert updated["api_key_source"] == "runtime"
    assert runtime_secret not in json.dumps(updated)
    assert runtime_secret not in repr(store)

    cleared = store.update(
        PublicationAISettingsUpdate.model_validate(
            {
                "enabled": True,
                "base_url": "https://proxy.example.com",
                "model": "deepseek-v4-pro",
                "clear_api_key": True,
            }
        )
    )
    assert cleared["api_key_source"] == "environment"
    assert cleared["api_key_configured"] is True


def test_runtime_settings_rejects_blank_key_without_changing_state() -> None:
    store = PublicationAISettingsStore(settings(enabled=False).__dict__, environ={})
    before = store.public_view()

    with pytest.raises(ValueError):
        PublicationAISettingsUpdate.model_validate(
            {
                "enabled": True,
                "base_url": "https://proxy.example.com",
                "model": "deepseek-v4-pro",
                "api_key": "   ",
            }
        )

    assert store.public_view() == before


def test_connection_check_uses_models_endpoint_without_article() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "deepseek-v4-flash", "object": "model"},
                    {"id": "deepseek-v4-pro", "object": "model"},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    advisor = DeepSeekPublicationAdvisor(
        settings(),
        http_client=client,
        api_key="connection-secret",
        api_key_env=None,
    )
    result = run(advisor.check_connection())
    assert result == {"model": "deepseek-v4-flash", "available_model_count": 2}
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/models"
    assert seen[0].content == b""
    assert "connection-secret" not in repr(result)
    run(client.aclose())

    unavailable_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "another-model"}]},
            )
        )
    )
    unavailable = DeepSeekPublicationAdvisor(
        settings(),
        http_client=unavailable_client,
        api_key="connection-secret",
        api_key_env=None,
    )
    with pytest.raises(PublicationAIError) as captured:
        run(unavailable.check_connection())
    assert captured.value.error_code == "AI_MODEL_UNAVAILABLE"
    run(unavailable_client.aclose())


def test_model_list_supports_custom_base_url_and_deduplicates_ids() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": " model-a ", "object": "model"},
                    {"id": "model-b", "object": "model"},
                    {"id": "model-a", "object": "model"},
                    {"id": "", "object": "model"},
                    {"object": "model"},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    advisor = DeepSeekPublicationAdvisor(
        settings(base_url="https://gateway.example/openai/v1", model="model-a"),
        http_client=client,
        api_key="catalog-secret",
        api_key_env=None,
    )
    assert run(advisor.list_models()) == ["model-a", "model-b"]
    assert seen[0].url == "https://gateway.example/openai/v1/models"
    assert seen[0].headers["Authorization"] == "Bearer catalog-secret"
    assert seen[0].content == b""
    run(client.aclose())


@pytest.mark.parametrize("status_code", [404, 405, 501])
def test_model_list_has_explicit_manual_fallback_when_endpoint_is_unsupported(
    status_code: int,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, text="PRIVATE_UPSTREAM_BODY")
        )
    )
    advisor = DeepSeekPublicationAdvisor(
        settings(),
        http_client=client,
        api_key="catalog-secret",
        api_key_env=None,
    )
    with pytest.raises(PublicationAIError) as captured:
        run(advisor.list_models())
    assert captured.value.error_code == "AI_MODEL_UNAVAILABLE"
    assert captured.value.safe_message == "该服务未提供标准模型列表接口，请手动输入模型 ID"
    assert "PRIVATE_UPSTREAM_BODY" not in repr(captured.value)
    run(client.aclose())


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"data": "not-a-list"},
            "该服务返回的模型列表格式不兼容，请手动输入模型 ID",
        ),
        ({"data": []}, "该服务未返回可用模型，请手动输入模型 ID"),
    ],
)
def test_model_list_invalid_or_empty_payload_keeps_manual_fallback(
    payload: dict,
    message: str,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        )
    )
    advisor = DeepSeekPublicationAdvisor(
        settings(),
        http_client=client,
        api_key="catalog-secret",
        api_key_env=None,
    )
    with pytest.raises(PublicationAIError) as captured:
        run(advisor.list_models())
    assert captured.value.error_code == "AI_RESPONSE_INVALID"
    assert captured.value.safe_message == message
    run(client.aclose())


def test_model_list_advisor_is_transient_and_reuses_or_overrides_effective_key() -> None:
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    store = PublicationAISettingsStore(
        settings(enabled=False).__dict__,
        environ={"DEEPSEEK_API_KEY": "environment-secret"},
    )
    before = store.public_view()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    inherited = PublicationAIModelListRequest.model_validate(
        {"base_url": "https://gateway.example/v1"}
    )
    advisor = store.create_model_list_advisor(inherited, http_client=client)
    assert run(advisor.list_models()) == ["model-a"]

    overridden = PublicationAIModelListRequest.model_validate(
        {
            "base_url": "https://other-gateway.example/v1",
            "api_key": "one-time-secret",
        }
    )
    advisor = store.create_model_list_advisor(overridden, http_client=client)
    assert run(advisor.list_models()) == ["model-a"]

    assert seen_authorization == [
        "Bearer environment-secret",
        "Bearer one-time-secret",
    ]
    assert store.public_view() == before
    assert "one-time-secret" not in repr(store)
    run(client.aclose())


class FakeConnectionAdvisor:
    async def check_connection(self) -> dict:
        return {"model": "deepseek-v4-pro", "available_model_count": 2}


class FakeModelListAdvisor:
    async def list_models(self) -> list[str]:
        return ["model-a", "model-b"]


def test_runtime_settings_http_contract_requires_csrf_and_never_returns_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = PublicationAISettingsStore(settings(enabled=False).__dict__, environ={})
    app = Flask("publication-ai-settings-test")
    app.secret_key = "test-secret"
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=sqlite_url(tmp_path / "accounts.db"),
            seed_legacy_profiles=False,
            auto_execute=False,
            heartbeat_enabled=False,
        )
    )
    account_state = app.extensions["account_sessions"]
    app.register_blueprint(
        create_content_studio_blueprint(
            account_state=account_state,
            database_url=sqlite_url(tmp_path / "content.db"),
            asset_root=tmp_path / "assets",
            work_root=tmp_path / "work",
            publication_settings_store=store,
        )
    )
    studio_state = app.extensions["content_studio"]
    client = app.test_client()

    initial = client.get("/api/settings/publication-ai")
    assert initial.status_code == 200
    assert initial.headers["Cache-Control"] == "no-store"
    assert set(initial.get_json()) == {
        "enabled",
        "base_url",
        "model",
        "api_key_configured",
        "api_key_source",
        "persistence",
    }

    update_payload = {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "api_key": "http-contract-secret",
        "clear_api_key": False,
    }
    unauthorized = client.put("/api/settings/publication-ai", json=update_payload)
    assert unauthorized.status_code == 403
    assert unauthorized.get_json()["error"] == "AI_SETTINGS_UNAUTHORIZED"

    with client.session_transaction() as session:
        session["articleops_ai_settings_csrf"] = "csrf-token"
    headers = {"X-ArticleOps-AI-Settings": "csrf-token"}

    unauthorized_models = client.post(
        "/api/settings/publication-ai/models",
        json={"base_url": "https://gateway.example/v1"},
    )
    assert unauthorized_models.status_code == 403
    assert unauthorized_models.get_json()["error"] == "AI_SETTINGS_UNAUTHORIZED"

    captured_model_list_request: list[PublicationAIModelListRequest] = []

    def create_model_list_advisor(payload: PublicationAIModelListRequest):
        captured_model_list_request.append(payload)
        return FakeModelListAdvisor()

    monkeypatch.setattr(store, "create_model_list_advisor", create_model_list_advisor)
    listed = client.post(
        "/api/settings/publication-ai/models",
        json={
            "base_url": "https://gateway.example/v1",
            "api_key": "one-time-http-secret",
        },
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.get_json()["models"] == ["model-a", "model-b"]
    assert listed.get_json()["available_model_count"] == 2
    assert "checked_at" in listed.get_json()
    assert "one-time-http-secret" not in listed.get_data(as_text=True)
    assert captured_model_list_request[0].base_url == "https://gateway.example/v1"
    assert (
        captured_model_list_request[0].api_key.get_secret_value()
        == "one-time-http-secret"
    )

    updated = client.put(
        "/api/settings/publication-ai",
        json=update_payload,
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.headers["Cache-Control"] == "no-store"
    assert updated.get_json()["api_key_configured"] is True
    assert "http-contract-secret" not in updated.get_data(as_text=True)
    assert set(updated.get_json()) == {
        "enabled",
        "base_url",
        "model",
        "api_key_configured",
        "api_key_source",
        "persistence",
    }

    monkeypatch.setattr(store, "create_advisor", lambda: FakeConnectionAdvisor())
    checked = client.post(
        "/api/settings/publication-ai/test",
        json={},
        headers=headers,
    )
    assert checked.status_code == 200
    assert checked.headers["Cache-Control"] == "no-store"
    assert checked.get_json()["ok"] is True
    assert checked.get_json()["model"] == "deepseek-v4-pro"
    assert "http-contract-secret" not in checked.get_data(as_text=True)

    def raise_timeout(coroutine, *, timeout):
        coroutine.close()
        raise TimeoutError("test timeout")

    monkeypatch.setattr(studio_state, "run", raise_timeout)
    timed_out = client.post(
        "/api/settings/publication-ai/test",
        json={},
        headers=headers,
    )
    assert timed_out.status_code == 504
    assert timed_out.get_json() == {
        "error": "AI_TIMEOUT",
        "message": "AI 服务响应超时",
    }

    studio_state.close()
    account_state.close()
