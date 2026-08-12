from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_official_coreui_source_and_runtime_are_vendored() -> None:
    package = read("frontend/coreui-free-bootstrap-admin-template/package.json")
    lock = read("frontend/coreui-free-bootstrap-admin-template/package-lock.json")
    notices = read("THIRD_PARTY_NOTICES.md")
    base = read("web/templates/base.html")

    assert '"version": "5.6.0"' in package
    assert '"@coreui/coreui": "^5.9.0"' in package
    assert '"node_modules/@coreui/coreui"' in lock
    assert '"version": "5.9.0"' in lock
    assert "da2c89f5e71a762fb46a3583f42d5f740d965b1d" in notices
    assert "coreui-template/css/style.min.css" in base
    assert "coreui-template/js/coreui.bundle.min.js" in base
    assert "coreui-template/simplebar/simplebar.min.js" in base
    assert "cdn.jsdelivr.net/npm/bootstrap@" not in base


def test_navigation_has_one_content_entry() -> None:
    base = read("web/templates/base.html")
    dashboard = read("src/article_mvp/web/templates/dashboard.html")

    assert base.count('href="/upload"') == 2  # sidebar and header primary action
    assert "创作与投递" in base
    assert 'href="/delivery/new"' not in base
    assert "内容投递</span>" not in base
    assert "创作与投递" in dashboard
    assert "创建文章" not in dashboard


def test_upload_is_content_studio_not_legacy_queue_form() -> None:
    template = read("web/templates/upload.html")

    assert 'id="content-studio"' in template
    assert 'data-drafts-url="/api/content-drafts"' in template
    assert 'data-docx-url="/api/content-drafts/import-docx"' in template
    assert 'data-legacy-url="/api/content-sources/legacy-articles"' in template
    assert 'data-plan-execute-url-template="/api/delivery-plans/{plan_id}/execute"' in template
    assert "/api/upload" not in template
    assert "开始处理并发布" not in template
    assert "凌晨三点" not in template


def test_autosave_and_conflict_paths_are_explicit() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="save-indicator"' in template
    assert "indexedDB.open('articleops-content-studio', 1)" in script
    assert "setTimeout(() => saveDraftNow(), 1000)" in script
    assert "method: 'PATCH'" in script
    assert "DRAFT_REVISION_CONFLICT" in script
    assert 'id="conflict-use-server"' in template
    assert 'id="conflict-save-copy"' in template
    assert "已恢复本地未同步内容" in script


def test_block_editor_has_drag_keyboard_and_mobile_alternatives() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert 'id="asset-upload"' in template
    assert "article.draggable = true" in script
    assert "reorderByDrop" in script
    assert "move-up" in script
    assert "move-down" in script
    assert "delete-block" in script
    assert "image-alt" in script
    assert "@media (max-width: 575.98px)" in styles


def test_accounts_load_after_platform_without_auto_selection() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/platforms/{platform}/accounts?usable=true" in template
    assert "state.accountRequestController?.abort()" in script
    assert "const sequence = ++state.accountRequestSequence" in script
    assert "account.session_status === 'VALID'" in script
    assert "不会自动选中" in script
    assert "loadAccounts('xiaoheihe')" not in script
    assert "loadAccounts('zol')" not in script
    assert 'role="switch"' in template
    assert "不代表当前已登录" in template


def test_multi_target_and_confirmation_contracts_are_separate() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/content-drafts/{draft_id}/targets" in template
    assert "state.draft.targets.some(target => target.account_id === account.account_id)" in script
    assert "draft_batch_confirmed" in script
    assert "confirmation_token" in script
    assert 'id="draft-batch-confirmed"' in template
    assert 'id="publish-confirm-modal"' in template
    assert "逐条确认公开发布" in template
    assert "submitDelivery" not in script
    assert "/api/delivery-operations" not in template + script


def test_primary_action_reports_and_focuses_missing_fields() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="create-plan"' in template
    assert "disabled" not in template.split('id="create-plan"', 1)[1].split(">", 1)[0]
    assert 'id="validation-summary"' in template
    assert "showValidation(issues)" in script
    assert "issues[0].focus" in script
    assert "填写文章标题" in script
    assert "至少添加一个投递目标" in script

