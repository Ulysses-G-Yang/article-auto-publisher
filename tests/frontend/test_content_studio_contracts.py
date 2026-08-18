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
    assert '"@coreui/coreui": "5.9.0"' in package
    assert '"@coreui/coreui": "5.9.0"' in lock
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

    # 侧边栏是 /upload 的唯一导航入口（顶部按钮已去重，避免多入口冗余）。
    assert base.count('href="/upload"') == 1
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
    assert 'id="save-draft-now"' in template
    assert "saveDraftNow()" in script
    assert "indexedDB.open('articleops-content-studio', 1)" in script
    assert "setTimeout(() => saveDraftNow(), 1000)" in script
    assert "method: 'PATCH'" in script
    assert "DRAFT_REVISION_CONFLICT" in script
    assert 'id="conflict-use-server"' in template
    assert 'id="conflict-save-copy"' in template
    assert "已恢复本地未同步内容" in script


def test_rich_editor_has_continuous_editing_and_drag_import() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert 'id="asset-upload"' in template
    assert 'id="rich-editor"' in template
    assert 'contenteditable="true"' in template
    assert "function blocksToHtml(blocks)" in script
    assert "function parseEditorToBlocks()" in script
    assert "handleEditorInput" in script
    assert "insertImageIntoEditor" in script
    assert "document.execCommand('insertText'" in script
    assert "pastedImages" in script
    assert "item.type.startsWith('image/')" in script
    assert "getAsFile()" in script
    assert "is-drop-target" in script
    assert "dataTransfer?.types" in script
    assert "startsWith('image/')" in script
    assert "endsWith('.docx')" in script
    assert "importDocx(docx)" in script
    assert "drop-overlay" in template
    assert ".rich-editor" in styles
    assert ".rich-image" in styles
    assert "@media (max-width: 575.98px)" in styles


def test_mobile_studio_does_not_hide_or_clip_overflow() -> None:
    base_styles = read("web/static/css/style.css")
    studio_styles = read("web/static/css/content-studio.css")

    body_rule = base_styles.split("body {", 1)[1].split("}", 1)[0]
    assert "overflow-x: hidden" not in body_rule
    assert ".studio-shell { width: 100%; max-width: 1500px; min-width: 0;" in studio_styles
    assert ".studio-layout { display: grid; width: 100%; min-width: 0;" in studio_styles
    assert ".studio-card { width: 100%; min-width: 0;" in studio_styles
    assert ".content-block { display: grid; width: 100%; min-width: 0;" in studio_styles
    assert ".studio-card-header > .d-flex .btn { flex: 1 1 calc(50% - .5rem);" in studio_styles
    assert "#draft-title, .block-content textarea { width: 100%; min-width: 0; max-width: 100%; }" in studio_styles
    assert ".block-actions { grid-column: 1 / -1; justify-content: flex-end; }" in studio_styles


def test_target_switcher_loads_accounts_without_auto_selection() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/platforms/{platform}/accounts?usable=true" in template
    assert "state.switcherController?.abort()" in script
    assert "const sequence = ++state.switcherSequence" in script
    assert "account.session_status === 'VALID'" in script
    assert "系统不会自动选择账号" in template
    assert "loadAccounts('xiaoheihe')" not in script
    assert "loadAccounts('zol')" not in script
    assert "loadSwitcherAccounts" in script
    assert "function togglePlatform(platformId, checked)" in script


def test_target_switcher_is_dynamic_vertical_rows_without_legacy_radios() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    stylesheet = read("web/static/css/content-studio.css")

    assert 'data-platforms-url="/api/platforms"' in template
    assert 'id="target-switcher-list"' in template
    assert 'id="platform-grid-loading"' in template
    assert "fetch(root.dataset.platformsUrl" in script
    assert "row.dataset.platformId = platform.id" in script
    assert "function loadPlatforms()" not in script
    assert "platformLabels" not in script
    assert 'input[name="target-platform"]' not in script
    assert 'id="platform-selector-grid"' not in template
    assert "target-platform-row" in stylesheet
    assert ".target-mode-switch" in stylesheet
    assert "target-platform-switch" in stylesheet


def test_target_switcher_uses_full_width_single_column_layout() -> None:
    stylesheet = read("web/static/css/content-studio.css")

    assert (
        ".target-builder { display: grid; "
        "grid-template-columns: minmax(0,1fr); align-items: stretch;"
    ) in stylesheet
    assert ".target-switcher-list { display: flex; width: 100%; min-width: 0;" in stylesheet
    assert ".target-platform-row { width: 100%; min-width: 0;" in stylesheet
    assert ".target-platform-head { display: flex; min-width: 0;" in stylesheet
    assert ".target-platform-name { min-width: 0; flex: 1 1 auto;" in stylesheet
    assert "    .target-builder { grid-template-columns: repeat(2,minmax(0,1fr)); }" not in stylesheet
    assert "    .target-builder, .studio-side, .source-actions" not in stylesheet
    assert ".target-platform-head { flex-wrap: wrap; }" in stylesheet
    assert ".target-mode-switch { order: 5; width: 100%; }" in stylesheet


def test_studio_initialization_refreshes_drafts_and_always_terminates() -> None:
    script = read("web/static/js/content-studio.js")

    assert "function openLocalDb(timeoutMs = 1500)" in script
    assert "request.onblocked = () => finish(null)" in script
    assert "const timer = setTimeout(() => finish(null), timeoutMs)" in script
    assert "const drafts = await refreshDrafts()" in script
    assert script.rstrip().endswith("init();\n})();")

def test_multi_target_and_confirmation_contracts_are_separate() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/content-drafts/{draft_id}/targets" in template
    assert "function rebuildTargets()" in script
    assert "saveTargets(targets)" in script
    assert "draft_batch_confirmed" in script
    assert "confirmation_token" in script
    assert 'id="draft-batch-confirmed"' in template
    assert 'id="publish-confirm-modal"' in template
    assert "逐条确认公开发布" in template
    assert "submitDelivery" not in script
    assert "/api/delivery-operations" not in template + script
    assert "addTarget" not in script


def test_delivery_statuses_are_explicit_and_never_auto_retry_publish() -> None:
    script = read("web/static/js/content-studio.js")
    docs = read("docs/frontend/CONTENT_STUDIO_UX.md")

    assert "CREATING: '正在创建执行单'" in script
    assert "PARTIAL_FAIL: '部分失败'" in script
    assert "RESULT_UNKNOWN: '结果未知，需人工核对'" in script
    assert (
        "'PARTIAL_FAIL', 'CONFIRMATION_REQUIRED', 'RESULT_UNKNOWN', "
        "'FORMAT_REVIEW_REQUIRED'"
    ) in script
    assert "['CREATING', 'QUEUED', 'RUNNING']" in script
    assert "系统不会自动重试" in script
    assert "platformDraftBoxUrl" in script
    assert "查看平台草稿箱" in script
    assert "target.status === 'DRAFT_SAVED'" in script
    assert "retry-target" not in script
    assert "不提供公开发布自动重试按钮" in docs


def test_primary_action_reports_and_focuses_missing_fields() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="create-plan"' in template
    assert "disabled" not in template.split('id="create-plan"', 1)[1].split(">", 1)[0]
    assert 'id="validation-summary"' in template
    assert "showValidation(issues)" in script
    assert "issues[0].focus" in script
    assert "填写文章标题" in script
    assert "至少选择一个投递目标" in script


def test_v2_draft_state_and_patch_never_silently_downgrade() -> None:
    script = read("web/static/js/content-studio.js")

    assert "function isV2Draft(draft = state.draft)" in script
    assert "content_schema_version: 2" in script
    assert "document: cloneValue(state.draft.document)" in script
    assert "cover: cloneValue(normalizedCover(state.draft.cover))" in script
    assert "const requestIsV2 = isV2Draft()" in script
    assert "const requestBlocks = requestIsV2 ? null : publicBlocks();" in script
    assert "const requestDocument = requestIsV2 ? cloneValue(state.draft.document) : null;" in script
    assert "const requestBody = requestIsV2" in script
    assert "document: requestDocument" in script
    assert "cover: requestCover" in script
    assert "asset_id: value.strategy === 'EXPLICIT' ? value.asset_id : null" in script
    assert "const requestSnapshot = contentSnapshot(state.draft);" in script
    assert "const changedDuringRequest = contentSnapshot(state.draft) !== requestSnapshot;" in script
    assert "DRAFT_CONTENT_SCHEMA_CONFLICT" in script
    assert "editor.contentEditable = readonly ? 'false' : 'true';" in script
    assert "if (isV2Draft()) return [];" in script
    assert "if (isV2Draft()) {\n            setMessage('content-error', v2ReadonlyMessage());\n            return;\n        }" in script
    assert "assetUpload.disabled = readonly" in script
    assert "Word 富文档受保护，当前正文只读；重新导入可替换" in script
    assert "showDropOverlay(false);" in script


def test_v2_local_recovery_conflict_copy_and_title_stay_schema_aware() -> None:
    script = read("web/static/js/content-studio.js")

    assert "content_schema_version: schemaVersion" in script
    assert "document: schemaVersion === 2 ? cloneValue(snapshot.document) : null" in script
    assert "state.draft.document = cloneValue(local.document);" in script
    assert "const localMatchesSchema = serverIsV2" in script
    assert "const localSameRevision = Boolean(local?.dirty && local.revision === payload.revision);" in script
    assert "已保留本地副本且未静默降级" in script
    assert "state.draft.document.title = event.target.value;" in script
    assert "content_schema_version: 2,\n                document: cloneValue(state.draft.document)" in script
    assert "cover: coverRequest(state.draft.cover)" in script


def test_v1_patch_keeps_legacy_blocks_and_cover_contract() -> None:
    script = read("web/static/js/content-studio.js")

    assert "blocks: requestBlocks" in script
    assert "cover: requestCover" in script
    assert "content_schema_version: 2" in script
    assert "content_schema_version: 1" in script


def test_v2_readonly_preview_uses_safe_dom_and_excludes_title_block() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert 'id="v2-readonly-notice"' in template
    assert "正文当前只读；重新导入可替换" in template
    assert 'id="asset-upload-trigger"' in template
    assert "function renderV2Document(editor, documentValue)" in script
    assert "editor.replaceChildren();" in script
    assert "document.createElement" in script
    assert "document.createTextNode" in script
    assert "title_block_id" in script
    assert "function countV2Document(documentValue)" in script
    assert "renderV2Document(editor, state.draft.document)" in script
    assert "assetUrl(node?.asset_id)" in script
    assert "safeHttpHref" in script
    assert "noopener noreferrer" in script
    assert "v2MarkTags" in script
    assert "block.ordered ? 'ol' : 'ul'" in script
    assert "className = 'rich-table-v2'" in script
    renderer = script.split("function renderV2Document(editor, documentValue)", 1)[1].split(
        "function v2ReadonlyMessage()", 1
    )[0]
    assert "innerHTML" not in renderer
    assert ".rich-editor.is-v2-readonly" in styles
    assert ".rich-image-v2" in styles
    assert ".rich-unsupported-node" in styles


def test_v2_controls_have_explicit_readonly_semantics_without_changing_v1_markup() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'contenteditable="true"' in template
    assert "assetTrigger.setAttribute('aria-disabled', String(readonly));" in script
    assert "clearButton.setAttribute('aria-disabled', String(readonly));" in script
    assert "notice?.classList.toggle('d-none', !readonly);" in script
    assert "editor.contentEditable = readonly ? 'false' : 'true';" in script


def test_cover_ui_uses_controlled_assets_and_preserves_request_contract() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert 'id="cover-panel"' in template
    assert 'id="cover-none"' in template
    assert 'id="cover-first-body-image"' in template
    assert 'id="cover-explicit"' in template
    assert 'id="cover-assets-list"' in template
    assert 'id="cover-auto-note"' in template
    assert "function collectV2ImageCandidates(documentValue)" in script
    assert "function contentImageCandidates()" in script
    assert "function renderCover()" in script
    assert "image.src = assetUrl(candidate.asset_id);" in script
    assert "function setCoverStrategy(strategy, assetId = null" in script
    assert "const currentExplicitId" in script
    assert "asset_id: value.strategy === 'EXPLICIT' ? value.asset_id : null" in script
    assert "coverAutoSelectionDismissed" in script
    assert "cover_auto_selection_dismissed" in script
    assert ".cover-asset-card" in styles


def test_docx_auto_cover_is_import_only_and_can_be_dismissed() -> None:
    script = read("web/static/js/content-studio.js")

    assert "async function maybeAutoSelectImportedCover()" in script
    assert "state.draft.source_type !== 'DOCX'" in script
    assert "normalizedCover(state.draft.cover).strategy !== 'NONE'" in script
    assert "state.coverAutoSelectionDismissed" in script
    assert (
        "setCoverStrategy('FIRST_BODY_IMAGE', candidates[0].asset_id, "
        "{ automatic: true })"
    ) in script
    assert "await maybeAutoSelectImportedCover();" in script
    assert "state.coverAutoSelectionDismissed = automatic ? false : strategy === 'NONE';" in script
    assert (
        "state.coverAutoSelectionDismissed = "
        "Boolean(local.cover_auto_selection_dismissed);"
    ) in script


def test_format_review_targets_are_warning_only_and_never_execute() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert "FORMAT_REVIEW_REQUIRED: '待格式复核'" in script
    assert "CONTENT_FORMAT_UNSUPPORTED" in script
    assert "PLATFORM_FORMAT_CAPABILITIES_UNDECLARED" in script
    assert "function isFormatBlockedTarget(target)" in script
    assert "function executablePlanTargets(plan = state.plan)" in script
    assert 'id="plan-format-warning"' in template
    assert "target_ids: executable.map(target => target.target_id)" in script
    assert "系统不会调用执行接口" in script
    assert ".plan-review-card.is-format-review" in styles
    assert "draft-confirmation-row" in script
