from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from pydantic import ValidationError

from account_sessions.contracts import (
    DeliveryRequest,
    PublishOptionCandidate,
    PublishOptionsRequest,
    PublishOptionsResponse,
)
from account_sessions.database import AccountDatabase
from account_sessions.errors import AccountSessionError, AccountUnavailableError
from account_sessions.models import PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT, AccessContext, PermissionDeniedError
from account_sessions.publish_options import PublishOptionsService
from account_sessions.web import create_account_session_blueprint
from platforms.base import BasePlatform
from platforms.xiaoheihe import XiaoheihePlatform
from platforms.zol import ZOLPlatform


def run(coroutine):
    return asyncio.run(coroutine)


def account(
    *,
    account_id: str = "account-1",
    platform: str = "xiaoheihe",
    status: str = "ACTIVE",
    session_status: str = "VALID",
):
    return SimpleNamespace(
        account_id=account_id,
        platform=platform,
        status=status,
        session_status=session_status,
    )


class FakeAccounts:
    def __init__(self, value, *, factory=None):
        self.value = value
        self.platform_factory = factory or (lambda _account: None)
        self.get_calls = 0

    async def get_account(self, account_id: str):
        self.get_calls += 1
        if self.value is None:
            from account_sessions.errors import AccountNotFoundError

            raise AccountNotFoundError("平台账号不存在")
        return self.value


def test_publish_options_request_is_strict_bounded_and_deduplicates_kinds() -> None:
    payload = PublishOptionsRequest.model_validate(
        {
            "queries": ["  显卡  "],
            "kinds": ["topic", "topic", "community"],
            "limit": 20,
            "mode": "PUBLISH",
        }
    )
    assert payload.queries == ["显卡"]
    assert payload.kinds == ["topic", "community"]

    for invalid in (
        {"queries": []},
        {"queries": ["x"] * 5},
        {"queries": ["x" * 31]},
        {"queries": ["   "]},
        {"queries": "topic"},
        {"queries": ["x"], "kinds": ["other"]},
        {"queries": ["x"], "limit": 21},
        {"queries": ["x"], "mode": "PUBLIC"},
        {"queries": ["x"], "platform": "zol"},
        {"queries": ["x"], "account_id": "account-1"},
    ):
        with pytest.raises(ValidationError):
            PublishOptionsRequest.model_validate(invalid)


def test_publish_options_response_and_candidate_are_redacted_strict_models() -> None:
    candidate = PublishOptionCandidate(
        candidate_key="topic-1",
        label="显示器",
        platform_option_id=None,
        source_query="显示器",
    )
    response = PublishOptionsResponse(
        account_id="account-1",
        platform="zol",
        supported=False,
        mode="DRAFT",
        groups=[{"kind": "topic", "candidates": [candidate]}],
        observed_at="2026-09-02T00:00:00+00:00",
        error_code="PUBLISH_OPTIONS_UNSUPPORTED",
    )
    assert set(response.model_dump()) == {
        "account_id",
        "platform",
        "supported",
        "mode",
        "groups",
        "observed_at",
        "expires_at",
        "error_code",
    }
    assert set(response.model_dump()["groups"][0]["candidates"][0]) == {
        "candidate_key",
        "label",
        "platform_option_id",
        "source_query",
    }
    with pytest.raises(ValidationError):
        PublishOptionsResponse(
            account_id="account-1",
            platform="zol",
            supported=False,
            mode="DRAFT",
            observed_at="now",
            profile_path="must-not-escape",
        )
    with pytest.raises(ValidationError):
        PublishOptionCandidate(
            candidate_key="topic-1",
            label="显示器",
            dom_html="must-not-escape",
        )

    # 账号级候选响应允许 douyin 作为未来的平台账号类型；投递请求仍由
    # DeliveryRequest 的受支持平台字面量单独约束（见下一个断言）。
    assert PublishOptionsResponse(
        account_id="account-1",
        platform="douyin",
        supported=False,
        mode="DRAFT",
        observed_at="now",
    ).platform == "douyin"
    with pytest.raises(ValidationError):
        DeliveryRequest.model_validate(
            {
                "article": {"title": "测试", "body": "正文"},
                "platform": "douyin",
                "account_id": "a" * 36,
            }
        )

    with pytest.raises(ValidationError):
        PublishOptionsResponse(
            account_id="account-1",
            platform="douyin",
            supported=True,
            mode="DRAFT",
            groups=[],
            observed_at="now",
        )


def test_publish_options_requires_session_read_and_account_allowlist_before_lookup() -> None:
    fake = FakeAccounts(account())
    denied = AccessContext(actor_id="test", source="TEST", capabilities=frozenset())
    with pytest.raises(PermissionDeniedError):
        run(
            PublishOptionsService(fake).discover(
                "account-1", PublishOptionsRequest(queries=["x"]), denied
            )
        )
    assert fake.get_calls == 0

    allowlisted = AccessContext(
        actor_id="test",
        source="TEST",
        capabilities=frozenset({"session.read"}),
        allowed_account_ids=frozenset({"another-account"}),
    )
    with pytest.raises(PermissionDeniedError):
        run(
            PublishOptionsService(fake).discover(
                "account-1", PublishOptionsRequest(queries=["x"]), allowlisted
            )
        )
    assert fake.get_calls == 0


def test_publish_options_not_found_archived_and_nonvalid_are_fail_closed() -> None:
    request = PublishOptionsRequest(queries=["x"])
    with pytest.raises(AccountSessionError) as missing:
        run(
            PublishOptionsService(FakeAccounts(None)).discover(
                "missing", request, LOCAL_WEB_CONTEXT
            )
        )
    assert missing.value.error_code == "ACCOUNT_NOT_FOUND"

    with pytest.raises(AccountSessionError) as archived:
        run(
            PublishOptionsService(FakeAccounts(account(status="ARCHIVED"))).discover(
                "account-1", request, LOCAL_WEB_CONTEXT
            )
        )
    assert archived.value.error_code == "ACCOUNT_ARCHIVED"

    with pytest.raises(AccountUnavailableError) as invalid:
        run(
            PublishOptionsService(FakeAccounts(account(session_status="ERROR"))).discover(
                "account-1", request, LOCAL_WEB_CONTEXT
            )
        )
    assert invalid.value.error_code == "ACCOUNT_SESSION_UNAVAILABLE"


@pytest.mark.parametrize("platform", ["xiaoheihe", "zol"])
def test_editor_platforms_return_unverified_without_factory_or_side_effects(platform: str) -> None:
    calls: list[str] = []

    def forbidden_factory(_account):
        calls.append("factory")
        raise AssertionError("editor platform must fail closed before factory")

    fake = FakeAccounts(account(platform=platform), factory=forbidden_factory)
    result = run(
        PublishOptionsService(fake).discover(
            "account-1",
            PublishOptionsRequest(queries=["x"]),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert result.supported is False
    assert result.error_code == "PUBLISH_OPTIONS_READONLY_UNVERIFIED"
    assert result.groups == []
    assert calls == []


def test_other_platform_uses_base_unsupported_without_initialize() -> None:
    calls: list[str] = []

    class FakePlatform:
        platform_name = "smzdm"
        discover_publish_options_readonly = BasePlatform.discover_publish_options_readonly

        async def initialize(self):
            calls.append("initialize")
            raise AssertionError("unsupported discovery must not initialize")

    fake = FakeAccounts(account(platform="smzdm"), factory=lambda _account: FakePlatform())
    result = run(
        PublishOptionsService(fake).discover(
            "account-1",
            PublishOptionsRequest(queries=["x"]),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert result.supported is False
    assert result.error_code == "PUBLISH_OPTIONS_UNSUPPORTED"
    assert calls == []


def test_xiaoheihe_offline_parser_deduplicates_limits_and_source() -> None:
    raw = [
        {
            "id": "community-1",
            "name": "  游戏硬件  ",
            "platform_option_id": "community-1",
        },
        {
            "id": "community-1",
            "name": "重复",
            "platform_option_id": "community-1",
        },
        {"key": "topic-1", "label": "显示器"},
    ]
    parsed = XiaoheihePlatform.parse_publish_option_candidates(
        raw,
        kind="community",
        source_query="显卡",
        limit=1,
    )
    assert parsed == [
        {
            "candidate_key": "community-1",
            "label": "游戏硬件",
            "platform_option_id": "community-1",
            "source_query": "显卡",
        }
    ]
    assert XiaoheihePlatform.parse_publish_option_candidates(
        object(), kind="topic"
    ) == []


def test_zol_offline_parser_supports_topic_only_and_deduplicates() -> None:
    raw = [
        {
            "candidate_key": "1",
            "label": "显示器",
            "platform_option_id": "1",
        },
        {
            "candidate_key": "1",
            "label": "重复",
            "platform_option_id": "1",
        },
        {
            "candidate_key": "2",
            "label": "键盘",
            "platform_option_id": "2",
        },
    ]
    assert ZOLPlatform.parse_publish_option_candidates(raw, kind="community") == []
    parsed = ZOLPlatform.parse_publish_option_candidates(raw, kind="topic", limit=2)
    assert parsed == [
        {
            "candidate_key": "1",
            "label": "显示器",
            "platform_option_id": "1",
            "source_query": None,
        },
        {
            "candidate_key": "2",
            "label": "键盘",
            "platform_option_id": "2",
            "source_query": None,
        },
    ]


_OFFLINE_OPTION_PARSERS = (
    pytest.param(
        XiaoheihePlatform.parse_publish_option_candidates,
        "community",
        id="xiaoheihe",
    ),
    pytest.param(
        ZOLPlatform.parse_publish_option_candidates,
        "topic",
        id="zol",
    ),
)
_SENSITIVE_OPTION_VALUES = (
    "https://example.invalid/option",
    "<div>html</div>",
    r"C:\\Users\\Administrator\\profile",
    "/tmp/profile/option",
    "token-value",
    "cookie-value",
    "profile-value",
    "path-value",
    "selector-value",
    "auth-value",
    "secret-value",
)
_SENSITIVE_OPTION_KEYS = (
    "url",
    "html",
    "token",
    "cookie",
    "profile",
    "path",
    "selector",
    "auth",
    "secret",
)


def _parser_fixture(parser, kind: str, item: dict) -> object:
    """构造两个平台都接受的有界 Mapping fixture。"""

    return [item] if parser is XiaoheihePlatform.parse_publish_option_candidates else [item]


@pytest.mark.parametrize("parser,kind", _OFFLINE_OPTION_PARSERS)
@pytest.mark.parametrize("bad_value", _SENSITIVE_OPTION_VALUES)
def test_offline_option_parsers_reject_sensitive_values(parser, kind, bad_value) -> None:
    item = {
        "candidate_key": "safe-key",
        "label": bad_value,
        "platform_option_id": "safe-id",
    }
    assert parser(_parser_fixture(parser, kind, item), kind=kind) == []


@pytest.mark.parametrize("parser,kind", _OFFLINE_OPTION_PARSERS)
@pytest.mark.parametrize("bad_key", _SENSITIVE_OPTION_KEYS)
def test_offline_option_parsers_reject_sensitive_keys(parser, kind, bad_key) -> None:
    item = {
        "candidate_key": "safe-key",
        "label": "安全候选",
        "platform_option_id": "safe-id",
        bad_key: "must-not-escape",
    }
    assert parser(_parser_fixture(parser, kind, item), kind=kind) == []


@pytest.mark.parametrize("parser,kind", _OFFLINE_OPTION_PARSERS)
def test_offline_option_parsers_reject_nested_candidates(parser, kind) -> None:
    item = {
        "candidate_key": {"nested": "value"},
        "label": "安全候选",
        "platform_option_id": "safe-id",
    }
    assert parser(_parser_fixture(parser, kind, item), kind=kind) == []


@pytest.mark.parametrize("parser,kind", _OFFLINE_OPTION_PARSERS)
def test_offline_option_parsers_reject_more_than_40_candidates(parser, kind) -> None:
    raw = [
        {
            "candidate_key": f"safe-{index}",
            "label": f"候选{index}",
            "platform_option_id": f"safe-id-{index}",
        }
        for index in range(41)
    ]
    assert parser(raw, kind=kind) == []


def test_publish_options_route_is_public_no_store_and_platform_is_account_derived(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'accounts.db').as_posix()}"
    database = AccountDatabase(database_url)
    account_id = str(uuid.uuid4())

    async def seed() -> None:
        await database.initialize()
        async with database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="xiaoheihe",
                    platform_user_id="safe-user",
                    display_name="测试账号",
                    profile_path=str(tmp_path / "profile"),
                    session_status="VALID",
                )
            )

    run(seed())
    run(database.dispose())
    app = Flask("publish-options-test")
    app.secret_key = "test"
    app.register_blueprint(
        create_account_session_blueprint(
            database_url=database_url,
            seed_legacy_profiles=False,
            auto_execute=False,
            heartbeat_enabled=False,
        )
    )
    client = app.test_client()
    response = client.post(
        f"/api/account-sessions/{account_id}/publish-options",
        json={"queries": ["显卡"], "mode": "PUBLISH"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert set(payload) == {
        "account_id",
        "platform",
        "supported",
        "mode",
        "groups",
        "observed_at",
        "expires_at",
        "error_code",
    }
    assert payload["account_id"] == account_id
    assert payload["platform"] == "xiaoheihe"
    assert payload["mode"] == "PUBLISH"
    assert payload["error_code"] == "PUBLISH_OPTIONS_READONLY_UNVERIFIED"
    publish_routes = {
        rule.rule for rule in app.url_map.iter_rules() if "publish-options" in rule.rule
    }
    assert "/api/internal/mcp/" not in publish_routes

    for field, value in (("platform", "zol"), ("account_id", "other-account")):
        forbidden = client.post(
            f"/api/account-sessions/{account_id}/publish-options",
            json={"queries": ["显卡"], field: value},
        )
        assert forbidden.status_code == 422
        assert forbidden.headers["Cache-Control"] == "no-store"
    app.extensions["account_sessions"].close()
