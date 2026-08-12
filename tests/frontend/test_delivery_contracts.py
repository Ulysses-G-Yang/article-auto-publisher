from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_delivery_page_starts_with_platform_choice_only() -> None:
    template = read("web/templates/delivery.html")
    script = read("web/static/js/delivery.js")

    assert 'name="delivery-platform"' in template
    assert 'id="account-placeholder"' in template
    assert "请先选择平台" in template
    assert "loadAccounts(platform)" in script
    assert "loadAccounts('xiaoheihe')" not in script
    assert "loadAccounts('zol')" not in script


def test_platform_change_clears_selection_and_blocks_stale_responses() -> None:
    script = read("web/static/js/delivery.js")

    assert "state.accounts = []" in script
    assert "state.selectedAccountId = null" in script
    assert "state.accountRequestController?.abort()" in script
    assert "const requestSequence = ++state.accountRequestSequence" in script
    assert "requestSequence !== state.accountRequestSequence || platform !== state.platform" in script
    assert "/api/platforms/{platform}/accounts?usable=true" in read("web/templates/delivery.html")


def test_accounts_are_never_automatically_selected_and_only_valid_is_enabled() -> None:
    script = read("web/static/js/delivery.js")

    assert "input.disabled = account.session_status !== 'VALID'" in script
    assert "if (!account || account.session_status !== 'VALID') return" in script
    assert "input.checked" not in script
    assert "系统不会自动选择" in script
    for status in ("VALID", "EXPIRED", "VERIFYING", "BUSY", "LOGIN_REQUIRED"):
        assert status in script


def test_account_labels_are_safe_and_use_masked_identifier() -> None:
    script = read("web/static/js/delivery.js")
    template = read("web/templates/delivery.html")

    assert "account.display_name" in script
    assert "account.masked_platform_user_id" in script
    assert "profile_path" not in script + template
    assert "account.platform_user_id" not in script
    assert "raw_platform_user_id" not in script + template
    assert "Cookie" not in script + template
    assert "access_token" not in script + template


def test_session_policy_is_not_a_login_status_switch() -> None:
    template = read("web/templates/delivery.html")
    script = read("web/static/js/delivery.js")

    assert 'role="switch"' in template
    assert "仅控制会话持久化策略，不代表当前登录状态" in template
    assert "/api/accounts/{account_id}/session-policy" in template
    assert "JSON.stringify({ persist_login: persistLogin })" in script
    assert "session-policy-error" in template


def test_draft_is_default_and_shows_confirmation_before_submit() -> None:
    template = read("web/templates/delivery.html")
    script = read("web/static/js/delivery.js")

    assert 'id="mode-draft" value="DRAFT" autocomplete="off" checked' in template
    assert "mode: 'DRAFT'" in script
    assert "showDraftConfirmation()" in script
    assert "确认保存到平台草稿箱" in template
    assert "确认保存草稿" in template


def test_publish_requires_428_token_and_explicit_second_confirmation() -> None:
    template = read("web/templates/delivery.html")
    script = read("web/static/js/delivery.js")

    assert "response.status === 428" in script
    assert "PUBLISH_CONFIRMATION_REQUIRED" in script
    assert "state.confirmationToken = payload.confirmation_token" in script
    assert "submitDelivery(state.confirmationToken)" in script
    assert "submitDelivery(null)" in script
    assert "高风险操作" in template
    assert "我已核对，确认公开发布" in template
    assert "切换模式不会直接执行公开发布" in template


def test_delivery_payload_and_mismatch_failures_follow_contract() -> None:
    script = read("web/static/js/delivery.js")
    template = read("web/templates/delivery.html")

    assert "article: { title:" in script
    assert "platform: state.platform" in script
    assert "account_id: state.selectedAccountId" in script
    assert "mode: state.mode" in script
    assert "confirmation_token: confirmationToken" in script
    assert "平台与账号响应不匹配" in script
    assert "执行单与所选平台或账号不匹配" in script
    assert "account-error" in script
    assert "session-policy-error" in script
    assert "delivery-error" in script
    assert "/api/account-sessions/{account_id}/activity" in template
    assert 'data-activity-url-template="/api/accounts/{account_id}/activity"' not in template


def test_delivery_page_has_mobile_keyboard_and_aria_basics() -> None:
    template = read("web/templates/delivery.html")
    styles = read("web/static/css/delivery.css")
    base = read("web/templates/base.html")

    assert 'role="radiogroup"' in template
    assert 'aria-live="polite"' in template
    assert 'aria-describedby="account-help"' in template
    assert 'aria-labelledby="publish-confirmation-title"' in template
    assert "overflow-x: auto" in styles
    assert "@media (max-width: 575.98px)" in styles
    assert "request.path.startswith('/delivery/')" in base
    assert 'href="/delivery/new"' in base


def test_example_content_uses_safe_server_injected_jinja_defaults() -> None:
    template = read("web/templates/delivery.html")

    assert "default_article_title" in template
    assert "default('凌晨三点，公司的智能马桶开始给我做绩效面谈', true) | e" in template
    assert "{{ default_article_body | default('', true) | e }}" in template
    assert "{% if default_article_body | default('', true) %}" in template
    assert "示例正文已预填，投递前仍可编辑并核对" in template
    assert "当前会话未提供完整正文" in template
    assert '<textarea id="article-body"' in template
    assert "不自行编造" not in template
