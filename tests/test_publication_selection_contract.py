"""平台发布选项冻结、幂等与执行结果契约测试。"""

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import math
import sqlite3
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.account_service import AccountSessionService
from account_sessions.contracts import ArticleInput, DeliveryRequest
from account_sessions.database import AccountDatabase
from account_sessions.database import sqlite_url as account_sqlite_url
from account_sessions.delivery_service import (
    DeliveryService,
    _apply_selection_outcome,
    _selection_outcome,
    operation_payload,
)
from account_sessions.errors import (
    AccountUnavailableError,
    ConfirmationInvalidError,
    ConfirmationRequiredError,
)
from account_sessions.models import DeliveryOperation, PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from account_sessions.security import (
    canonical_platform_selection,
    delivery_fingerprint,
    platform_selection_hash,
)
from content_studio.assets import AssetStore
from content_studio.contracts import (
    CreateDraftRequest,
    DraftTargetInput,
    ExecuteDeliveryPlanRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase
from content_studio.database import sqlite_url as content_sqlite_url
from content_studio.errors import DeliveryPlanStaleError
from content_studio.models import ContentVersion, DraftTarget
from content_studio.service import ContentStudioService
from content_studio.web import ContentStudioRuntimeState


def test_selection_contract_defaults_and_rejects_unbounded_values() -> None:
    request = DeliveryRequest(
        article=ArticleInput(title="标题", body="正文"),
        platform="xiaoheihe",
        account_id=str(uuid.uuid4()),
    )
    target = DraftTargetInput(
        platform="xiaoheihe",
        account_id=str(uuid.uuid4()),
    )
    assert request.platform_selection is None
    assert target.platform_selection is None
    assert DraftTargetInput(
        platform="xiaoheihe",
        account_id=str(uuid.uuid4()),
        platform_selection={"community": "社区", "topic": "话题"},
    ).platform_selection == {"community": "社区", "topic": "话题"}

    with pytest.raises(ValidationError):
        DraftTargetInput(
            platform="xiaoheihe",
            account_id=str(uuid.uuid4()),
            platform_selection={"community": ["不允许的嵌套值"]},
        )
    with pytest.raises(ValidationError):
        DeliveryRequest(
            article=ArticleInput(title="标题", body="正文"),
            platform="xiaoheihe",
            account_id=str(uuid.uuid4()),
            platform_selection={"community": "x" * 513},
        )
    with pytest.raises(ValidationError):
        DeliveryRequest(
            article=ArticleInput(title="标题", body="正文"),
            platform="xiaoheihe",
            account_id=str(uuid.uuid4()),
            platform_selection={"Community": "社区"},
        )
    with pytest.raises(ValidationError):
        DeliveryRequest(
            article=ArticleInput(title="标题", body="正文"),
            platform="xiaoheihe",
            account_id=str(uuid.uuid4()),
            platform_selection={"profile_path": "不应进入选择快照"},
        )
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            DeliveryRequest(
                article=ArticleInput(title="标题", body="正文"),
                platform="xiaoheihe",
                account_id=str(uuid.uuid4()),
                platform_selection={"score": value},
            )


def test_selection_is_canonical_and_bound_to_operation_fingerprint() -> None:
    first = {"topic": "话题", "community": "社区"}
    reordered = {"community": "社区", "topic": "话题"}
    changed = {"community": "社区", "topic": "另一个话题"}

    assert canonical_platform_selection(first) == canonical_platform_selection(reordered)
    assert platform_selection_hash(first) == platform_selection_hash(reordered)
    assert delivery_fingerprint(
        platform="xiaoheihe",
        account_id="account",
        title="标题",
        body="正文",
        mode="PUBLISH",
        platform_selection=first,
    ) == delivery_fingerprint(
        platform="xiaoheihe",
        account_id="account",
        title="标题",
        body="正文",
        mode="PUBLISH",
        platform_selection=reordered,
    )
    assert delivery_fingerprint(
        platform="xiaoheihe",
        account_id="account",
        title="标题",
        body="正文",
        mode="PUBLISH",
        platform_selection=first,
    ) != delivery_fingerprint(
        platform="xiaoheihe",
        account_id="account",
        title="标题",
        body="正文",
        mode="PUBLISH",
        platform_selection=changed,
    )


def test_selection_outcome_is_bounded_and_exposed_in_operation_payload() -> None:
    operation = DeliveryOperation(
        operation_id="operation",
        account_id="account",
        platform="xiaoheihe",
        mode="DRAFT",
        source="WEB",
        actor_id="actor",
        title="标题",
        body="正文",
        content_version="hash",
        account_display_name_snapshot="测试账号",
    )
    account = PlatformAccount(
        account_id="account",
        platform="xiaoheihe",
        platform_user_id="platform-user",
        display_name="测试账号",
        profile_path="C:\\fixture-profile",
        status="ACTIVE",
        session_status="VALID",
    )
    outcome = _selection_outcome(
        {
            "selection": {"community": "社区", "ignored": ["nested"]},
            "selection_status": "completed",
            "selection_error": "",
            "selection_error_code": "selection_applied",
        }
    )

    _apply_selection_outcome(operation, outcome)

    assert operation.platform_selection_result == {"community": "社区"}
    assert operation.platform_selection_status == "completed"
    assert operation.platform_selection_error is None
    assert operation.platform_selection_error_code == "SELECTION_APPLIED"
    payload = operation_payload(operation, account)
    assert payload["platform_selection_result"] == {"community": "社区"}
    assert payload["platform_selection_status"] == "completed"
    assert payload["platform_selection_error_code"] == "SELECTION_APPLIED"


def test_selection_columns_are_created_idempotently(tmp_path: Path) -> None:
    async def initialize_databases() -> None:
        account_database = AccountDatabase(account_sqlite_url(tmp_path / "accounts.db"))
        content_database = ContentDatabase(content_sqlite_url(tmp_path / "content.db"))
        await account_database.initialize()
        await account_database.initialize()
        await content_database.initialize()
        await content_database.initialize()
        await account_database.dispose()
        await content_database.dispose()

    asyncio.run(initialize_databases())

    with sqlite3.connect(tmp_path / "accounts.db") as connection:
        operation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(delivery_operations)")
        }
    with sqlite3.connect(tmp_path / "content.db") as connection:
        draft_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(draft_targets)")
        }
        plan_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(delivery_plan_targets)")
        }

    assert {
        "platform_selection_snapshot",
        "platform_selection_result",
        "platform_selection_status",
        "platform_selection_error",
        "platform_selection_error_code",
    }.issubset(operation_columns)
    assert {
        "platform_selection",
        "platform_selection_options",
        "platform_selection_hash",
        "platform_selection_source",
    }.issubset(draft_columns)
    assert {
        "platform_selection",
        "platform_selection_options",
        "platform_selection_hash",
        "platform_selection_source",
    }.issubset(plan_columns)


def test_delivery_request_selection_is_frozen_and_idempotent(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, dict]:
        account_database = AccountDatabase(account_sqlite_url(tmp_path / "accounts.db"))
        await account_database.initialize()
        account_id = str(uuid.uuid4())
        async with account_database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="xiaoheihe",
                    platform_user_id="fixture-user",
                    display_name="测试账号",
                    profile_path="C:\\fixture-profile",
                    status="ACTIVE",
                    session_status="VALID",
                )
            )
        accounts = AccountSessionService(
            account_database,
            seed_legacy_profiles=False,
        )
        delivery = DeliveryService(accounts)
        selection = {"community": "社区", "topic": "话题"}
        request = DeliveryRequest(
            article=ArticleInput(title="标题", body="正文"),
            platform="xiaoheihe",
            account_id=account_id,
            platform_selection=selection,
        )
        first = await delivery.request_delivery(
            request,
            LOCAL_WEB_CONTEXT,
            request_key="selection-idempotency",
        )
        repeated = await delivery.request_delivery(
            request.model_copy(
                update={
                    "platform_selection": {"topic": "话题", "community": "社区"}
                }
            ),
            LOCAL_WEB_CONTEXT,
            request_key="selection-idempotency",
        )
        with pytest.raises(AccountUnavailableError):
            await delivery.request_delivery(
                request.model_copy(update={"platform_selection": {"community": "其他社区"}}),
                LOCAL_WEB_CONTEXT,
                request_key="selection-idempotency",
            )
        await account_database.dispose()
        return first, repeated

    first, repeated = asyncio.run(scenario())
    assert first["platform_selection_snapshot"] == {
        "community": "社区",
        "topic": "话题",
    }
    assert repeated["operation_id"] == first["operation_id"]


def test_content_studio_execute_propagates_frozen_selection() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.target = {
                "target_id": "target",
                "platform": "xiaoheihe",
                "account_id": str(uuid.uuid4()),
                "mode": "DRAFT",
                "persist_login": True,
                "status": "READY",
                "operation_id": None,
                "platform_selection": {"community": "社区"},
            }

        async def get_plan_execution_context(self, _plan_id, _access):
            return {"title": "标题", "content_hash": "content", "version_id": "version"}, [
                self.target.copy()
            ]

        async def claim_plan_target(self, _plan_id, _target_id):
            return "claim"

        async def set_plan_target_result(self, _plan_id, _target_id, **_kwargs):
            return None

        async def get_delivery_plan(self, _plan_id, _access):
            return {"plan_id": "plan", "targets": [{**self.target, "status": "DRAFT_SAVED"}]}

    class FakeDelivery:
        def __init__(self) -> None:
            self.request = None

        async def request_delivery(self, request, _access, **_kwargs):
            self.request = request
            return {"operation_id": "operation", "status": "DRAFT_SAVED"}

    class FakeAccountState:
        auto_execute = False

        def __init__(self) -> None:
            self.delivery = FakeDelivery()

    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.service = FakeService()
    state.account_state = FakeAccountState()

    result = asyncio.run(
        state.execute_plan(
            "plan",
            ExecuteDeliveryPlanRequest(),
            LOCAL_WEB_CONTEXT,
        )
    )

    assert result["targets"][0]["status"] == "DRAFT_SAVED"
    assert state.account_state.delivery.request.platform_selection == {"community": "社区"}


def test_content_studio_persists_and_freezes_selection_without_content_version_leak(
    tmp_path: Path,
) -> None:
    account_id = str(uuid.uuid4())
    account = PlatformAccount(
        account_id=account_id,
        platform="xiaoheihe",
        platform_user_id="fixture-user",
        display_name="测试账号",
        profile_path="C:\\fixture-profile",
        status="ACTIVE",
        session_status="VALID",
        persist_login=True,
    )

    class FakeAccounts:
        async def require_account(self, requested_id, platform, _access, _capability):
            assert requested_id == account_id
            assert platform == "xiaoheihe"
            return account

    async def scenario() -> tuple[dict, dict, Path]:
        database_path = tmp_path / "content.db"
        database = ContentDatabase(content_sqlite_url(database_path))
        service = ContentStudioService(
            database,
            asset_store=AssetStore(tmp_path / "assets"),
            account_service=FakeAccounts(),
        )
        await database.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(
                title="带平台选项的草稿",
                blocks=[{"type": "text", "text": "冻结正文", "position": 0}],
            )
        )
        selection = {"community": "社区", "topic": "话题"}
        targeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=draft["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account_id,
                        "mode": "DRAFT",
                        "platform_selection": selection,
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        assert (
            targeted["targets"][0]["platform_selection_source"]
            == LOCAL_WEB_CONTEXT.source
        )
        plan = await service.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )
        context, targets = await service.get_plan_execution_context(
            plan["plan_id"], LOCAL_WEB_CONTEXT
        )
        assert targets[0]["platform_selection"] == selection
        assert targets[0]["platform_selection_hash"] == platform_selection_hash(selection)
        async with database.session() as session:
            source = await session.scalar(
                select(DraftTarget).where(DraftTarget.draft_id == draft["draft_id"])
            )
            assert source is not None
            source.platform_selection = {"community": "源选择已改"}
        with pytest.raises(DeliveryPlanStaleError):
            await service.get_plan_execution_context(plan["plan_id"], LOCAL_WEB_CONTEXT)

        retargeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=targeted["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account_id,
                        "mode": "DRAFT",
                        "platform_selection": selection,
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        assert retargeted["revision"] == targeted["revision"] + 1
        with pytest.raises(DeliveryPlanStaleError):
            await service.get_plan_execution_context(plan["plan_id"], LOCAL_WEB_CONTEXT)

        assert {
            column.name for column in ContentVersion.__table__.columns
        }.isdisjoint(
            {
                "platform_selection",
                "platform_selection_options",
                "platform_selection_hash",
                "platform_selection_source",
            }
        )
        await database.dispose()
        return plan, context, database_path

    plan, context, database_path = asyncio.run(scenario())
    assert context["content_hash"] == plan["content_version"]
    with sqlite3.connect(database_path) as connection:
        version_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(content_versions)")
        }
    assert not {
        "platform_selection",
        "platform_selection_options",
        "platform_selection_hash",
        "platform_selection_source",
    } & version_columns


def test_delivery_execute_passes_selection_override_and_persists_outcome(
    tmp_path: Path,
) -> None:
    selection = {"community": "社区", "topic": "话题"}

    class FakePlatform:
        context = None

        def __init__(self) -> None:
            self.publish_kwargs = None

        async def initialize(self) -> None:
            return None

        async def publish(self, **kwargs):
            self.publish_kwargs = kwargs
            return {
                "success": True,
                "draft_url": "https://example.invalid/xiaoheihe/article/123",
                "post_url": "",
                "verification_evidence": {
                    "draft_entity_bound": True,
                    "reopen_title_match": True,
                    "reopen_dom_blocks_match": True,
                },
                "selection": {"community": "社区"},
                "selection_status": "completed",
                "selection_error": "",
                "selection_error_code": "SELECTION_APPLIED",
                "media_status": "complete",
                "cover_status": "not_required",
                "degraded": None,
            }

        async def cleanup(self) -> None:
            return None

    async def scenario() -> tuple[dict, FakePlatform]:
        database = AccountDatabase(account_sqlite_url(tmp_path / "accounts.db"))
        await database.initialize()
        account_id = str(uuid.uuid4())
        async with database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="xiaoheihe",
                    platform_user_id="fixture-user",
                    display_name="测试账号",
                    profile_path="C:\\fixture-profile",
                    status="ACTIVE",
                    session_status="VALID",
                    persist_login=True,
                )
            )
        platform = FakePlatform()
        accounts = AccountSessionService(database, seed_legacy_profiles=False)

        async def assert_identity(_account, _platform, _access) -> None:
            return None

        accounts.assert_delivery_identity = assert_identity
        accounts._lease = lambda _account, *, purpose: nullcontext()
        delivery = DeliveryService(accounts, platform_factory=lambda _account: platform)
        request = DeliveryRequest(
            article=ArticleInput(title="标题", body="正文"),
            platform="xiaoheihe",
            account_id=account_id,
            platform_selection=selection,
        )
        queued = await delivery.request_delivery(
            request,
            LOCAL_WEB_CONTEXT,
            request_key="selection-execute",
            persist_login_snapshot=True,
        )
        completed = await delivery.execute_operation(
            queued["operation_id"], LOCAL_WEB_CONTEXT
        )
        await database.dispose()
        return completed, platform

    completed, platform = asyncio.run(scenario())
    assert platform.publish_kwargs["selection_override"] == selection
    assert completed["status"] == "DRAFT_SAVED"
    assert completed["platform_selection_snapshot"] == selection
    assert completed["platform_selection_result"] == {"community": "社区"}
    assert completed["platform_selection_status"] == "completed"
    assert completed["platform_selection_error"] is None
    assert completed["platform_selection_error_code"] == "SELECTION_APPLIED"


def test_publish_confirmation_token_is_invalidated_by_selection_change(
    tmp_path: Path,
) -> None:
    async def scenario() -> str:
        database = AccountDatabase(account_sqlite_url(tmp_path / "accounts.db"))
        await database.initialize()
        account_id = str(uuid.uuid4())
        async with database.session() as session:
            session.add(
                PlatformAccount(
                    account_id=account_id,
                    platform="xiaoheihe",
                    platform_user_id="fixture-user",
                    display_name="测试账号",
                    profile_path="C:\\fixture-profile",
                    status="ACTIVE",
                    session_status="VALID",
                    persist_login=True,
                )
            )
        delivery = DeliveryService(
            AccountSessionService(database, seed_legacy_profiles=False),
            public_publish_enabled=False,
        )
        request = DeliveryRequest(
            article=ArticleInput(title="公开标题", body="公开正文"),
            platform="xiaoheihe",
            account_id=account_id,
            mode="PUBLISH",
            platform_selection={"community": "社区"},
        )
        with pytest.raises(ConfirmationRequiredError) as required:
            await delivery.request_delivery(request, LOCAL_WEB_CONTEXT)
        token = required.value.token
        changed = request.model_copy(
            update={
                "confirmation_token": token,
                "platform_selection": {"community": "另一个社区"},
            }
        )
        with pytest.raises(ConfirmationInvalidError) as invalid:
            await delivery.request_delivery(changed, LOCAL_WEB_CONTEXT)
        await database.dispose()
        return invalid.value.error_code

    assert asyncio.run(scenario()) == "PUBLISH_CONFIRMATION_INVALID"
