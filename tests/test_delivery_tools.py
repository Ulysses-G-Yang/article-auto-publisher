"""delivery_tools 外挂模块测试。

全部使用 mock，不启动真实浏览器、不触碰网络、不读取凭据。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delivery_tools.human_delay_util import (
    DEFAULT_DELAY_CONFIG,
    DelayConfig,
    configure_delays,
    human_action_delay,
    human_page_delay,
    human_type,
    no_delay,
    task_gap_sleep,
)
from delivery_tools.pw_shared_sdk import SharedPlaywright
from delivery_tools.session_heartbeat import (
    STATUS_ALIVE,
    STATUS_DEAD,
    STATUS_EXPIRE,
    AccountMeta,
    _coerce_meta,
    check_account_healthy,
    heartbeat_loop,
    single_account_heartbeat,
)

# ---------------------------------------------------------------------------
# 模块一：SharedPlaywright
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试前重置单例，避免跨测试状态污染。"""
    SharedPlaywright._instance = None
    yield
    SharedPlaywright._instance = None


class TestSharedPlaywright:
    def test_singleton(self):
        a = SharedPlaywright.get_instance()
        b = SharedPlaywright.get_instance()
        assert a is b

    @pytest.mark.asyncio
    async def test_init_global_browser_is_idempotent(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = False
        sdk.pw = None
        sdk.browser = None

        fake_pw = AsyncMock()
        fake_browser = MagicMock()

        with patch(
            "delivery_tools.pw_shared_sdk.async_playwright",
            return_value=AsyncMock(start=AsyncMock(return_value=fake_pw)),
        ) as mock_pw:
            fake_pw.chromium.launch = AsyncMock(return_value=fake_browser)
            await sdk.init_global_browser(headless=True)
            await sdk.init_global_browser(headless=True)

        assert sdk.is_inited is True
        assert sdk.browser is fake_browser
        assert mock_pw.call_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_ctx_caches_per_account(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx_a = AsyncMock()
        ctx_b = AsyncMock()
        sdk.browser.new_context = AsyncMock(side_effect=[ctx_a, ctx_b])
        ctx_a.is_connected = MagicMock(return_value=True)
        ctx_b.is_connected = MagicMock(return_value=True)

        got_a1 = await sdk.get_or_create_ctx("zhihu", "acc001")
        got_a2 = await sdk.get_or_create_ctx("zhihu", "acc001")
        got_b = await sdk.get_or_create_ctx("weibo", "acc002")

        assert got_a1 is ctx_a
        assert got_a2 is ctx_a
        assert got_b is ctx_b
        assert sdk.browser.new_context.call_count == 2
        assert sdk.active_context_count == 2

    @pytest.mark.asyncio
    async def test_get_or_create_ctx_injects_cookies(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)

        cookies = [{"name": "session", "value": "x", "domain": "example.com", "path": "/"}]
        await sdk.get_or_create_ctx("zhihu", "acc001", cookies=cookies)
        ctx.add_cookies.assert_awaited_once_with(cookies)

    @pytest.mark.asyncio
    async def test_get_or_create_ctx_before_init_raises(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = False
        sdk.browser = None
        with pytest.raises(RuntimeError):
            await sdk.get_or_create_ctx("zhihu", "acc001")

    @pytest.mark.asyncio
    async def test_dump_ctx_cookies(self):
        sdk = SharedPlaywright.get_instance()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "a"}])
        result = await sdk.dump_ctx_cookies(ctx, "https://example.com")
        ctx.cookies.assert_awaited_once_with("https://example.com")
        assert result == [{"name": "a"}]

    @pytest.mark.asyncio
    async def test_close_one_ctx(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)

        await sdk.get_or_create_ctx("zhihu", "acc001")
        assert sdk.active_context_count == 1
        await sdk.close_one_ctx("zhihu", "acc001")
        assert sdk.active_context_count == 0
        # 关闭不存在的 key 不报错
        await sdk.close_one_ctx("zhihu", "nope")

    @pytest.mark.asyncio
    async def test_close_all_and_shutdown(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)

        await sdk.get_or_create_ctx("zhihu", "acc001")
        await sdk.get_or_create_ctx("weibo", "acc002")
        assert sdk.active_context_count == 2

        await sdk.close_all_ctx()
        assert sdk.active_context_count == 0

        await sdk.shutdown()
        assert sdk.is_inited is False


# ---------------------------------------------------------------------------
# 模块二：session_heartbeat
# ---------------------------------------------------------------------------


class TestSessionHeartbeat:
    def test_account_meta_to_dict(self):
        meta = AccountMeta("zhihu", "acc001", "https://example.com/me")
        d = meta.to_dict()
        assert d["site"] == "zhihu"
        assert d["account_id"] == "acc001"
        assert d["check_url"] == "https://example.com/me"

    def test_coerce_meta_dict(self):
        meta = _coerce_meta({"site": "zhihu", "account_id": "acc001", "check_url": "https://x"})
        assert isinstance(meta, AccountMeta)
        assert meta.site == "zhihu"
        assert meta.account_id == "acc001"

    def test_coerce_meta_rejects_bad_type(self):
        with pytest.raises(TypeError):
            _coerce_meta("not-a-meta")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_single_heartbeat_alive(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)
        page = AsyncMock()
        page.url = "https://example.com/me"
        ctx.new_page = AsyncMock(return_value=page)

        statuses = []
        saved = []

        async def save_cookie(site, account_id, cookies):
            saved.append(cookies)

        async def update_status(site, account_id, status):
            statuses.append(status)

        page.locator = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=0)

        result = await single_account_heartbeat(
            "zhihu",
            "acc001",
            "https://example.com/me",
            load_cookie_fn=None,
            save_cookie_fn=save_cookie,
            update_status_fn=update_status,
        )

        assert result == STATUS_ALIVE
        assert statuses == [STATUS_ALIVE]
        ctx.new_page.assert_awaited_once()
        page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_single_heartbeat_expire(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)
        page = AsyncMock()
        page.url = "https://example.com/login"
        ctx.new_page = AsyncMock(return_value=page)

        statuses = []

        async def update_status(site, account_id, status):
            statuses.append(status)

        result = await single_account_heartbeat(
            "zhihu",
            "acc001",
            "https://example.com/login",
            save_cookie_fn=None,
            update_status_fn=update_status,
        )

        assert result == STATUS_EXPIRE
        assert statuses == [STATUS_EXPIRE]

    @pytest.mark.asyncio
    async def test_single_heartbeat_dead_on_goto_error(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("boom"))
        ctx.new_page = AsyncMock(return_value=page)

        statuses = []

        async def update_status(site, account_id, status):
            statuses.append(status)

        result = await single_account_heartbeat(
            "zhihu",
            "acc001",
            "https://example.com/me",
            save_cookie_fn=None,
            update_status_fn=update_status,
        )

        assert result == STATUS_DEAD
        assert statuses == [STATUS_DEAD]

    @pytest.mark.asyncio
    async def test_heartbeat_loop_runs_once_and_sleeps(self):
        sdk = SharedPlaywright.get_instance()
        sdk.is_inited = True
        sdk.browser = MagicMock()
        ctx = AsyncMock()
        ctx.is_connected = MagicMock(return_value=True)
        sdk.browser.new_context = AsyncMock(return_value=ctx)
        page = AsyncMock()
        page.url = "https://example.com/me"
        ctx.new_page = AsyncMock(return_value=page)
        page.locator = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=0)

        with patch("delivery_tools.session_heartbeat.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                task = asyncio.create_task(
                    heartbeat_loop(
                        [{"site": "zhihu", "account_id": "acc001", "check_url": "https://example.com/me"}],
                        heartbeat_interval_sec=300,
                    )
                )
                await asyncio.sleep(0.2)
                task.cancel()
                await task

    @pytest.mark.asyncio
    async def test_check_account_healthy_without_storage_returns_true(self):
        assert await check_account_healthy("zhihu", "acc001") is True

    @pytest.mark.asyncio
    async def test_check_account_healthy_with_cookies(self):
        async def load(site, account_id):
            return [{"name": "session"}]

        assert await check_account_healthy("zhihu", "acc001", load_cookie_fn=load) is True

    @pytest.mark.asyncio
    async def test_check_account_healthy_empty_cookies_false(self):
        async def load(site, account_id):
            return []

        assert await check_account_healthy("zhihu", "acc001", load_cookie_fn=load) is False


# ---------------------------------------------------------------------------
# 模块三：human_delay_util
# ---------------------------------------------------------------------------


class TestHumanDelayUtil:
    def test_config_defaults(self):
        cfg = DelayConfig()
        assert cfg.values["action_min"] == DEFAULT_DELAY_CONFIG["action_min"]
        assert cfg.values["action_max"] == DEFAULT_DELAY_CONFIG["action_max"]

    def test_config_custom_values(self):
        cfg = DelayConfig({"action_min": 0.1, "action_max": 0.2})
        assert cfg.values["action_min"] == 0.1

    @pytest.mark.asyncio
    async def test_action_delay_decorator_sleeps_before_call(self):
        calls = []

        @human_action_delay(delay=lambda: 0.0)
        async def do_click(page, sel):
            calls.append(("click", sel))

        page = MagicMock()
        with patch("delivery_tools.human_delay_util.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await do_click(page, "#btn")
        assert calls == [("click", "#btn")]
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_action_delay_preserves_return_value(self):
        @human_action_delay(delay=lambda: 0.0)
        async def returns_value():
            return 42

        result = await returns_value()
        assert result == 42

    @pytest.mark.asyncio
    async def test_page_delay(self):
        with patch("delivery_tools.human_delay_util.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await human_page_delay(delay=lambda: 0.0)()
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_task_gap_sleep(self):
        with patch("delivery_tools.human_delay_util.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await task_gap_sleep(delay=lambda: 0.0)
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_human_type_uses_char_delay(self):
        page = AsyncMock()
        page.click = AsyncMock()
        page.type = AsyncMock()

        with patch("delivery_tools.human_delay_util.asyncio.sleep", new=AsyncMock()):
            await human_type(page, "#title", "ab", char_min=0.001, char_max=0.001)

        page.click.assert_awaited_once_with("#title")
        assert page.type.await_count == 2

    def test_configure_delays(self):
        cfg = configure_delays({"action_min": 1.5, "action_max": 3.5})
        assert cfg.values["action_min"] == 1.5
        assert cfg.values["action_max"] == 3.5

    def test_no_delay_zeroes_everything(self):
        cfg = no_delay()
        delay_keys = (
            "action_min",
            "action_max",
            "page_wait_min",
            "page_wait_max",
            "task_gap_min",
            "task_gap_max",
        )
        for k in delay_keys:
            assert cfg.values[k] == 0.0