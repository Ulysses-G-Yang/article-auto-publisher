from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_legacy_account_area_and_endpoints_are_preserved() -> None:
    template = read("web/templates/accounts.html")

    assert 'x-data="accounts" x-init="init()"' in template
    assert "fetch('/api/accounts')" in template
    assert "/api/accounts/${platform}/logout" in template
    assert "/api/accounts/${platform}/clear-cookies" in template
    assert "fetch('/api/accounts/' + platform + '/login'" in template
    assert "fetch('/api/cleanup'" in template
    assert 'id="reloginModal"' in template


def test_multi_account_area_waits_for_platform_before_loading() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    assert 'id="multi-account-sessions"' in template
    assert "/api/platforms/{platform}/accounts?usable=false" in template
    assert "input.name = 'session-platform'" in script
    assert "if (!state.platform) return" in script
    assert "loadAccounts('zol')" not in script
    assert "loadAccounts('xiaoheihe')" not in script


def test_account_platform_matrix_is_dynamic_and_capability_aware() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")
    styles = read("web/static/css/account-sessions.css")

    assert 'data-platforms-url="/api/platforms"' in template
    assert 'id="session-platforms"' in template
    assert 'id="session-platform-xiaoheihe"' not in template
    assert 'id="session-platform-zol"' not in template
    assert "fetch(root.dataset.platformsUrl" in script
    assert "Array.isArray(payload.platforms)" in script
    assert "input.disabled = !platform.account_enabled" in script
    assert "platform.delivery_enabled ? '账号与投递' : '仅账号管理'" in script
    assert "'即将接入'" in script
    assert "loadPlatforms();" in script
    assert "grid-auto-flow: column" in styles
    assert "overflow-x: auto" in styles
    assert "session-platform-logo" in styles


def test_only_public_account_fields_are_rendered() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    for field in (
        "account_id", "display_name", "masked_platform_user_id", "status",
        "session_status", "persist_login", "last_verified_at",
    ):
        assert f"'{field}'" in script
    assert "PUBLIC_ACCOUNT_FIELDS" in script
    assert "profile_path" not in script + template
    assert "raw_platform_user_id" not in script + template
    assert "account.platform_user_id" not in script
    assert "Cookie" not in script
    assert "access_token" not in script


def test_account_actions_follow_frozen_session_states() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    assert "['UNVERIFIED', 'ERROR', 'EXPIRED']" in script
    assert "account.session_status === 'LOGIN_REQUIRED'" in script
    assert "account.session_status === 'VALID'" in script
    assert "验证现有登录态" in script
    assert "不会自动打开扫码" in script
    assert "/api/accounts/{account_id}/verify" in template
    assert "重新登录" in script
    assert "function loginAccount(account)" in script
    assert "/api/account-sessions/{account_id}/login" in template
    assert "/api/account-sessions/{account_id}/logout" in template


def test_add_account_creates_isolated_profile_without_auto_selection() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    assert "/api/platforms/{platform}/accounts/login" in template
    assert "添加该平台账号" in template
    assert "隔离浏览器 Profile" in template
    assert "打开交互登录" in template
    assert "selectedAccount" not in script
    assert ".checked = true" not in script


def test_account_removal_confirms_before_delete_and_uses_delete_endpoint() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    assert 'data-delete-account-url-template="/api/account-sessions/{account_id}"' in template
    assert "删除账号" in script
    assert "function removeAccount(account)" in script
    assert "method: 'DELETE'" in script
    assert "window.confirm" in script
    assert "拥有投递历史的账号会被拒绝删除" in script
    assert "隔离浏览器 Profile" in script
    assert "此操作不可撤销" in script
    assert "loadAccounts()" in script


def test_session_policy_and_activity_have_independent_states() -> None:
    template = read("web/templates/accounts.html")
    script = read("web/static/js/account-sessions.js")

    assert "/api/accounts/{account_id}/session-policy" in template
    assert "JSON.stringify({ persist_login: input.checked })" in script
    assert "/api/account-sessions/{account_id}/activity" in template
    assert "/api/account-sessions/{account_id}/logout" in template
    assert 'id="account-activity-loading"' in template
    assert 'id="account-activity-error"' in template
    assert 'id="account-activity-empty"' in template
    assert "bootstrap.Offcanvas" in script


def test_polling_is_targeted_bounded_and_not_global() -> None:
    script = read("web/static/js/account-sessions.js")

    assert "const MAX_TARGETED_POLLS = 12" in script
    assert "const POLL_INTERVAL_MS = 2500" in script
    assert "function startTargetedPolling(accountId)" in script
    assert "state.targetedPolls.forEach(timer => clearTimeout(timer))" in script
    assert "attempts >= MAX_TARGETED_POLLS" in script
    assert "setInterval" not in script
    assert "window.setInterval" not in script


def test_multi_account_area_has_mobile_and_accessibility_contracts() -> None:
    template = read("web/templates/accounts.html")
    styles = read("web/static/css/account-sessions.css")

    assert 'aria-labelledby="multi-account-heading"' in template
    assert 'role="radiogroup"' in template
    assert 'aria-live="polite"' in template
    assert 'aria-labelledby="account-activity-title"' in template
    assert 'aria-label="关闭活动日志"' in template
    assert "@media (max-width: 575.98px)" in styles
    assert "grid-template-columns: 1fr" in styles
