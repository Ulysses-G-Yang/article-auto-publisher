"""网页计划到私密发布执行单的隔离回归；不访问真实账号或平台。"""

import copy
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from flask import Flask
from pydantic import ValidationError

from account_sessions.contracts import DeliveryRequest
from account_sessions.models import PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from account_sessions.web import create_account_session_blueprint
from content_studio.contracts import DraftTargetInput
from content_studio.database import sqlite_url
from content_studio.importers import LegacyDatabaseSource
from content_studio.web import create_content_studio_blueprint


class PrivatePlatform:
    platform_name = "xiaohongshu"

    def __init__(self):
        self.context = SimpleNamespace(clear_cookies=AsyncMock())
        self.calls = []
        self.cleaned = False
        self.result = {
            "success": True, "status": "SUBMITTED", "visibility": "SELF_ONLY",
            "media_status": "completed",
            "verification_evidence": {"submit_acknowledged": True, "visibility": "SELF_ONLY"},
        }

    async def initialize(self):
        pass

    async def check_login(self):
        return True

    async def fetch_identity_payload(self):
        return {"ok": True, "user_id": "private-test-id", "display_name": "隔离账号"}

    async def publish(self, **_kwargs):
        raise AssertionError("私密执行不得走云端草稿/公开发布入口")

    async def publish_private_article(self, **kwargs):
        assert kwargs["confirmed"] is True
        self.calls.append(kwargs)
        return copy.deepcopy(self.result)

    async def cleanup(self):
        self.cleaned = True


@pytest.fixture
def private_app(tmp_path: Path):
    platform = PrivatePlatform()
    app = Flask(__name__)
    app.secret_key = "isolated-test-only"
    app.register_blueprint(create_account_session_blueprint(
        database_url=sqlite_url(tmp_path / "accounts.db"), seed_legacy_profiles=False,
        auto_execute=False, public_publish_enabled=False, platform_factory=lambda _: platform,
        allowed_profile_roots=(tmp_path,), heartbeat_enabled=False,
    ))
    accounts = app.extensions["account_sessions"]
    app.register_blueprint(create_content_studio_blueprint(
        account_state=accounts, database_url=sqlite_url(tmp_path / "content.db"),
        asset_root=tmp_path / "assets", work_root=tmp_path / "work",
        legacy_source=LegacyDatabaseSource(
            database_path=tmp_path / "unused-legacy.db", allowed_image_roots=(tmp_path,),
        ),
    ))
    client = app.test_client()
    client.get("/api/platforms")
    account_id = str(uuid.uuid4())
    profile = tmp_path / "profile"
    profile.mkdir()

    async def seed_account():
        async with accounts.database.session() as session:
            session.add(PlatformAccount(
                account_id=account_id, platform="xiaohongshu", platform_user_id="private-test-id",
                display_name="隔离账号", profile_path=str(profile.resolve()),
                status="ACTIVE", session_status="VALID", persist_login=True,
            ))

    accounts.run(seed_account())
    try:
        yield client, accounts, app.extensions["content_studio"], platform, account_id
    finally:
        app.extensions["content_studio"].close()
        accounts.close()


def make_private_plan(client, account_id):
    import io

    from tests.test_content_studio import seven_image_edge_docx_bytes

    word, _ = seven_image_edge_docx_bytes()
    response = client.post("/api/content-drafts/import-docx", data={
        "file": (io.BytesIO(word), "isolated-seven.docx"),
    })
    assert response.status_code == 201, response.get_json()
    draft = response.get_json()
    response = client.put(f"/api/content-drafts/{draft['draft_id']}/targets", json={
        "revision": draft["revision"], "targets": [{
            "platform": "xiaohongshu", "account_id": account_id,
            "mode": "PRIVATE_PUBLISH", "persist_login": True,
        }],
    })
    assert response.status_code == 200, response.get_json()
    saved = response.get_json()
    response = client.post(f"/api/content-drafts/{draft['draft_id']}/delivery-plans", json={
        "revision": saved["revision"],
    })
    assert response.status_code == 201, response.get_json()
    plan = response.get_json()
    assert plan["targets"][0]["status"] == "READY"
    return plan, draft


def test_private_web_plan_confirmation_execution_and_terminal_status(private_app):
    client, accounts, studio, platform, account_id = private_app
    plan, draft = make_private_plan(client, account_id)
    execute_url = f"/api/delivery-plans/{plan['plan_id']}/execute"
    response = client.post(execute_url, json={})
    assert response.status_code == 428
    target = response.get_json()["targets"][0]
    assert platform.calls == []
    response = client.post(execute_url, json={
        "target_ids": [target["target_id"]],
        "confirmations": {target["target_id"]: target["confirmation_token"]},
    })
    assert response.status_code == 202, response.get_json()
    operation_id = response.get_json()["targets"][0]["operation_id"]
    result = accounts.run(accounts.delivery.execute_operation(operation_id, LOCAL_WEB_CONTEXT))
    assert result["status"] == "SUBMITTED"
    assert result["platform_url"] is None
    assert result["article_mapping_status"] == "NOT_PENDING"
    assert accounts.delivery.public_publish_enabled is False
    assert len(platform.calls) == 1
    assert len(platform.calls[0]["images"]) == 7
    blocks = platform.calls[0]["content_blocks"]
    assert sum(block["type"] == "image" for block in blocks) == 7
    assert platform.cleaned is True
    platform.context.clear_cookies.assert_not_awaited()
    synced = client.get(f"/api/delivery-plans/{plan['plan_id']}").get_json()
    assert synced["status"] == "SUCCESS"
    assert synced["targets"][0]["status"] == "SUBMITTED"
    client.post(execute_url, json={})
    accounts.run(accounts.delivery.execute_operation(operation_id, LOCAL_WEB_CONTEXT))
    assert len(platform.calls) == 1
    current = client.get(f"/api/content-drafts/{draft['draft_id']}").get_json()
    assert current["document"] == draft["document"]


@pytest.mark.parametrize("mode", ["DRAFT", "PUBLISH"])
def test_private_mode_does_not_open_other_modes(private_app, mode):
    client, accounts, _studio, platform, account_id = private_app
    assert accounts.delivery.public_publish_enabled is False
    catalog = client.get("/api/platforms").get_json()["platforms"]
    xhs = next(item for item in catalog if item["id"] == "xiaohongshu")
    assert xhs["delivery_enabled"] is False
    assert xhs["private_publish_enabled"] is True
    assert all(
        not item["private_publish_enabled"] for item in catalog if item["id"] != "xiaohongshu"
    )
    if mode == "PUBLISH":
        from account_sessions.errors import ConfirmationRequiredError, PublicPublishDisabledError
        request = DeliveryRequest.model_validate({
            "article": {"title": "标题", "body": "正文"}, "platform": "xiaohongshu",
            "account_id": account_id, "mode": mode,
        })
        with pytest.raises(ConfirmationRequiredError) as raised:
            accounts.run(accounts.delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
        request.confirmation_token = raised.value.token
        with pytest.raises(PublicPublishDisabledError):
            accounts.run(accounts.delivery.request_delivery(request, LOCAL_WEB_CONTEXT))
    assert not platform.calls


@pytest.mark.parametrize("contract", [DeliveryRequest, DraftTargetInput])
def test_private_mode_rejected_for_other_platform(contract):
    data = {"platform": "zhihu", "account_id": str(uuid.uuid4()), "mode": "PRIVATE_PUBLISH"}
    if contract is DeliveryRequest:
        data["article"] = {"title": "标题", "body": "正文"}
    with pytest.raises(ValidationError, match="仅小红书"):
        contract.model_validate(data)
