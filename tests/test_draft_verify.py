"""只读核验（方案二）测试。

覆盖：DraftVerifyService 编排（找到/未找到/歧义/unsupported/错误）、
基类默认 unsupported、账号/租约交互。
全程 mock，不启动真实浏览器、不触网。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from account_sessions.draft_verify import (
    PROBE_ACCOUNT_INACTIVE,
    PROBE_NOT_FOUND,
    PROBE_TITLE_AMBIGUOUS,
    PROBE_TITLE_MISSING,
    PROBE_UNSUPPORTED,
    DraftVerifyService,
)
from account_sessions.errors import AccountSessionError
from account_sessions.permissions import AccessContext


class _FakeOperation:
    def __init__(self, *, title: str, platform: str = "zhihu") -> None:
        self.title = title
        self.platform = platform


class _FakeAccount:
    def __init__(self, *, account_id: str = "acc-1", status: str = "ACTIVE") -> None:
        self.account_id = account_id
        self.status = status
        self.platform = "zhihu"
        self.profile_path = "D:/fake/profiles/zhihu/acc-1"


def _access() -> AccessContext:
    return AccessContext(
        actor_id="tester",
        source="TEST",
        capabilities=frozenset({"draft.create", "session.read"}),
    )


def _service(platform_result=None, *, account=None):
    """构造 DraftVerifyService，mock 账号/投递/平台。"""
    operation = _FakeOperation(title="测试文章")
    delivery = MagicMock()
    delivery._load_operation = AsyncMock(return_value=(operation, account or _FakeAccount()))

    accounts = MagicMock()
    accounts._lease = MagicMock(
        return_value=MagicMock(
            __enter__=MagicMock(return_value=None),
            __exit__=MagicMock(return_value=False),
        )
    )
    accounts.database.session = MagicMock()

    platform = MagicMock()
    platform.initialize = AsyncMock()
    platform.cleanup = AsyncMock()
    platform.verify_draft_readonly = AsyncMock(return_value=platform_result or {})

    def factory(_account):
        return platform

    service = DraftVerifyService(
        accounts,
        delivery,
        platform_factory=factory,
    )
    return service, platform, delivery


class TestDraftVerifyService:
    @pytest.mark.asyncio
    async def test_found_single_draft(self) -> None:
        service, platform, _ = _service(
            {
                "title_matched": True,
                "match_count": 1,
                "draft_url": "https://zhuanlan.zhihu.com/p/123/edit",
                "structure": {"source": "draft_list"},
            }
        )
        result = await service.verify_draft("op-1", _access())
        assert result["title_matched"] is True
        assert result["match_count"] == 1
        assert result["draft_url"].startswith("https://")
        platform.verify_draft_readonly.assert_awaited_once_with("测试文章")

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        service, _, _ = _service({"error_code": "PROBE_NOT_FOUND", "error_message": "无"})
        result = await service.verify_draft("op-1", _access())
        assert result["error_code"] == PROBE_NOT_FOUND
        assert result["title_matched"] is False

    @pytest.mark.asyncio
    async def test_ambiguous(self) -> None:
        """match_count > 1 → normalize 推导为歧义。"""
        service, _, _ = _service({"title_matched": False, "match_count": 3})
        result = await service.verify_draft("op-1", _access())
        assert result["error_code"] == PROBE_TITLE_AMBIGUOUS
        assert "3 个同名" in result["error_message"]

    @pytest.mark.asyncio
    async def test_unsupported_platform(self) -> None:
        service, platform, _ = _service({"unsupported": True})
        result = await service.verify_draft("op-1", _access())
        assert result["error_code"] == PROBE_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_missing_title_rejected(self) -> None:
        delivery = MagicMock()
        delivery._load_operation = AsyncMock(
            return_value=(_FakeOperation(title="  "), _FakeAccount())
        )
        accounts = MagicMock()
        service = DraftVerifyService(accounts, delivery, platform_factory=lambda _a: None)
        with pytest.raises(AccountSessionError) as exc_info:
            await service.verify_draft("op-1", _access())
        assert exc_info.value.error_code == PROBE_TITLE_MISSING

    @pytest.mark.asyncio
    async def test_inactive_account_rejected(self) -> None:
        delivery = MagicMock()
        delivery._load_operation = AsyncMock(
            return_value=(_FakeOperation(title="x"), _FakeAccount(status="ARCHIVED"))
        )
        accounts = MagicMock()
        service = DraftVerifyService(accounts, delivery, platform_factory=lambda _a: None)
        with pytest.raises(AccountSessionError) as exc_info:
            await service.verify_draft("op-1", _access())
        assert exc_info.value.error_code == PROBE_ACCOUNT_INACTIVE

    @pytest.mark.asyncio
    async def test_platform_exception_maps_to_unknown(self) -> None:
        """平台异常 → AccountSessionError(PROBE_RESULT_UNKNOWN)，cleanup 仍执行。"""
        service, platform, _ = _service({})
        platform.verify_draft_readonly = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        with pytest.raises(AccountSessionError) as exc_info:
            await service.verify_draft("op-1", _access())
        assert exc_info.value.error_code == "PROBE_RESULT_UNKNOWN"
        platform.cleanup.assert_awaited()


class TestBasePlatformHook:
    @pytest.mark.asyncio
    async def test_default_unsupported(self) -> None:
        """基类默认 unsupported，未覆盖平台 fail-closed。"""
        from platforms.base import BasePlatform

        class _Concrete(BasePlatform):
            platform_name = "test"

            async def check_login(self):
                return False

            async def login(self):
                pass

            async def navigate_to_editor(self):
                pass

            async def fill_title(self, title):
                pass

            async def fill_content(self, content_blocks, images):
                pass

            async def select_topic(
                self, topic="", community="", selection_query="", selection_override=None
            ):
                pass

            async def save_draft(self, title=""):
                return ""

        platform = _Concrete()
        result = await platform.verify_draft_readonly("任何标题")
        assert result == {"unsupported": True}
