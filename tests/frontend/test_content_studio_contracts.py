import re
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
    assert 'id="source-library-modal"' in template
    assert 'id="source-library-title" class="modal-title">历史草稿' in template
    assert 'id="legacy-tab"' not in template
    assert 'data-legacy-url=' not in template
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
    assert (
        "#draft-title, .block-content textarea { width: 100%; min-width: 0; max-width: 100%; }"
        in studio_styles
    )
    assert ".block-actions { grid-column: 1 / -1; justify-content: flex-end; }" in studio_styles


def test_target_switcher_loads_accounts_without_auto_selection() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/platforms/{platform}/accounts?usable=true" in template
    assert "state.switcherControllers[platformId]?.abort()" in script
    assert "state.switcherSequences[platformId]" in script
    assert "const controller = new AbortController()" in script
    assert "account.session_status === 'VALID'" in script
    assert "系统不会自动选择账号" in template
    assert "loadAccounts('xiaoheihe')" not in script
    assert "loadAccounts('zol')" not in script
    assert "loadSwitcherAccounts" in script
    assert "function togglePlatform(platformId, checked)" in script


def test_bulk_target_controls_select_only_deliverable_platforms_and_valid_accounts() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert 'id="toggle-all-platforms"' in template
    assert 'id="toggle-all-accounts"' in template
    assert 'id="target-selection-summary"' in template
    assert 'id="account-load-progress"' in template
    assert "function deliverablePlatforms()" in script
    assert "state.platforms.filter(platform => platform.delivery_enabled)" in script
    assert "account.session_status === 'VALID'" in script
    assert "function toggleAllPlatforms()" in script
    assert "function toggleAllAccounts()" in script
    assert "取消全选所有平台" in script
    assert "取消全选所有账户" in script
    assert "请先选择至少一个可投递平台" in script
    assert "state.switcherSelected = nextSelected;" in script
    assert ".target-bulk-actions .btn { min-height: 44px; }" in styles
    assert ".target-platform-head { display: flex; min-width: 0; min-height: 44px;" in styles
    assert ".target-account-check { display: flex; min-height: 44px;" in styles


def test_target_saves_are_serialized_and_latest_selection_is_coalesced() -> None:
    script = read("web/static/js/content-studio.js")

    assert "targetSavePending: null" in script
    assert "targetSavePromise: null" in script
    assert "function saveTargets(targets)" in script
    assert "async function drainTargetSaveQueue()" in script
    assert "while (state.targetSavePending !== null)" in script
    assert "saved = await persistTargets(targets);" in script
    assert "state.targetSavePending = cloneValue(targets);" in script
    assert "async function flushTargetSaveQueue()" in script
    assert "if (!(await flushTargetSaveQueue())" in script
    assert "targetSaving: false" in script
    assert "mutationPromise: null" in script
    assert "function enqueueDraftMutation(operation)" in script
    assert "return enqueueDraftMutation(saveDraftNowUnlocked);" in script
    assert "state.targetSaving = true;" in script
    assert "state.targetSaving = false;" in script
    assert "const requestDraftId = state.draft.draft_id;" in script
    assert "if (state.draft?.draft_id !== requestDraftId) return true;" in script
    assert "restoreSwitcherSelection(serverTargets);" in script
    assert "已恢复到最后一次服务端状态" in script


def test_delayed_content_patch_is_bound_to_its_source_draft() -> None:
    script = read("web/static/js/content-studio.js")

    assert "const requestDraft = state.draft;" in script
    assert "const requestDraftId = requestDraft.draft_id;" in script
    assert "const requestIsCurrent = () => state.draft === requestDraft" in script
    assert "if (!requestIsCurrent()) return true;" in script
    assert "requestDraft.revision = payload.revision;" in script
    assert "requestDraft.source_type = payload.source_type;" in script


def test_existing_targets_survive_account_metadata_loading() -> None:
    script = read("web/static/js/content-studio.js")

    assert "const existingTargets = (state.draft.targets || [])" in script
    assert "const existingTarget = existingTargets" in script
    assert "const accountsLoading = !Array.isArray" in script
    assert "if (!account && accountsLoading && existingTarget)" in script
    assert "...existingTarget," in script
    assert "function reconcileLoadedAccountSelection" in script
    assert "selected.filter(accountId => !validIds.has(accountId))" in script
    assert "if (render && invalidIds.length) rebuildTargets();" in script
    assert "当前不是 VALID" in script
    assert "switcherAccountLoadFailed: {}" in script
    assert "部分平台账号加载失败，本次全选未保存" in script


def test_failed_publish_confirmation_stays_open_for_retry() -> None:
    script = read("web/static/js/content-studio.js")

    assert "const accepted = await executePlan(" in script
    assert "if (!accepted) return;" in script
    assert "manageFollowup = true" in script
    assert "{ manageFollowup: false }" in script
    assert "state.pendingPublishTargets = state.plan.targets.filter" in script


def test_platform_row_click_does_not_capture_nested_mode_or_switch_space() -> None:
    script = read("web/static/js/content-studio.js")

    assert (
        "button, input, a, label, .target-mode-switch, .target-platform-switch"
        in script
    )


def test_target_switcher_is_dynamic_capability_matrix_without_legacy_radios() -> None:
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
    assert "state.platforms.map(platform => switcherRow(platform))" in script
    assert "platformCapability(platform)" in script
    assert "仅账号管理" in script
    assert "即将接入" in script


def test_target_switcher_uses_comfortable_responsive_matrix_layout() -> None:
    stylesheet = read("web/static/css/content-studio.css")

    assert (
        ".target-builder { display: grid; "
        "grid-template-columns: minmax(0,1fr); align-items: stretch;"
    ) in stylesheet
    assert ".target-switcher-list { display: grid; width: 100%; min-width: 0;" in stylesheet
    assert "grid-template-columns: repeat(2,minmax(0,1fr));" in stylesheet
    assert ".target-platform-row { width: 100%; min-width: 0;" in stylesheet
    assert ".target-platform-head { display: flex; min-width: 0;" in stylesheet
    assert ".target-platform-name { min-width: 0; flex: 1 1 auto;" in stylesheet
    assert ".target-switcher-list { grid-template-columns: minmax(0,1fr); }" in stylesheet
    assert "    .target-builder, .studio-side, .source-actions" not in stylesheet
    assert "flex-wrap: wrap; align-items: center;" in stylesheet
    assert ".target-mode-switch { order: 5; display: inline-flex; width: 100%;" in stylesheet


def test_studio_initialization_is_blank_until_explicit_draft_id() -> None:
    script = read("web/static/js/content-studio.js")

    assert "function openLocalDb(timeoutMs = 1500)" in script
    assert "request.onblocked = () => finish(null)" in script
    assert "const timer = setTimeout(() => finish(null), timeoutMs)" in script
    assert "const requestedId = new URLSearchParams(window.location.search)" in script
    assert "await openDraft(blankDraft(), { historyMode: 'replace' });" in script
    assert "const drafts = await refreshDrafts()" not in script
    assert "draft = drafts[0]" not in script
    init_body = script.split("async function init()", 1)[1].split("init();", 1)[0]
    assert "method: 'POST'" not in init_body
    assert script.rstrip().endswith("init();\n})();")


def test_explicit_restore_import_and_history_use_draft_urls() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "endpoint(root.dataset.draftUrlTemplate, 'draft_id', requestedId)" in script
    assert "historyMode: 'push'" in script
    assert "history.pushState({}, '', url)" in script
    assert "history.replaceState({}, '', url)" in script
    assert "await openDraft(draft, { historyMode: 'replace' });" in script
    assert "独立创建一份新草稿，不会覆盖历史" in template
    assert "async function loadDraftLibrary()" in script
    assert "shown.coreui.modal" in script
    assert "source_ref" in script
    assert "创建：${formatDate(draft.created_at)}" in script
    assert "状态：${draftStatusLabels" in script
    assert "copyLegacyArticle" not in script


def test_history_source_summary_hides_internal_source_refs() -> None:
    script = read("web/static/js/content-studio.js")

    assert "LEGACY_ARTICLE: '历史文章'" in script
    assert "SYSTEM_SEED: '系统草稿'" in script
    assert "function draftSourceSummary(draft)" in script
    assert "sourceType !== 'DOCX' || !draft?.source_ref" in script
    assert "split(/[\\\\/]+/).filter(Boolean).pop()" in script
    assert "meta.textContent = draftSourceSummary(draft);" in script

def test_multi_target_and_confirmation_contracts_are_separate() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/content-drafts/{draft_id}/targets" in template
    assert "function rebuildTargets()" in script
    assert "saveTargets(targets)" in script
    assert "draft_batch_confirmed" in script
    assert "confirmation_token" in script
    assert 'id="draft-batch-confirmed"' not in template
    assert 'id="draft-confirmation-row"' not in template
    assert "draft_batch_confirmed: true" in script
    assert "executeImmediately = true" in script
    assert 'id="publish-confirm-modal"' in template
    assert "逐条确认公开发布" in template
    assert "submitDelivery" not in script
    # 前端不直接提交执行单；只读核验（verify-draft）是唯一允许的
    # delivery-operations 前缀调用，且必须是 POST + /verify-draft 后缀。
    assert "/api/delivery-operations" not in template
    assert re.search(r"fetch\(`/api/delivery-operations/\$\{", script) is not None
    assert "/verify-draft" in script
    assert "addTarget" not in script


def test_delivery_statuses_are_explicit_and_never_auto_retry_publish() -> None:
    script = read("web/static/js/content-studio.js")
    docs = read("docs/frontend/CONTENT_STUDIO_UX.md")

    assert "CREATING: '正在创建执行单'" in script
    assert "PARTIAL_FAIL: '部分成功 / 部分失败'" in script
    assert "SUCCESS: '全部成功'" in script
    assert "FATAL: '全部失败'" in script
    assert "RESULT_UNKNOWN: '结果未知，需人工核对'" in script
    assert "DELIVERY_INCOMPLETE: '投递未完成'" in script
    assert "XHS_CLOUD_DRAFT_UNAVAILABLE" in script
    assert "网页端无云端草稿" in script
    assert "DRAFT_SAVED_WITH_WARNINGS: '草稿已保存（有警告）'" in script
    assert "function targetNeedsRelogin(target)" in script
    assert "return '需要重新登录'" in script
    assert "const MAX_PLAN_POLLS = 240" in script
    assert "['CREATING', 'QUEUED', 'RUNNING']" in script
    assert "系统不会自动重试" in script
    assert "platformDraftBoxUrl" in script
    assert "查看平台草稿箱" in script
    assert "['DRAFT_SAVED', 'DRAFT_SAVED_WITH_WARNINGS'].includes(target.status)" in script
    assert "草稿已保存（草稿箱确认）" in script
    assert "retry-target" not in script
    assert "不提供公开发布自动重试按钮" in docs


def test_draft_evidence_and_readonly_verify_contracts() -> None:
    """草稿保存证据展示 + 只读核验按钮（方案一+方案二）契约。"""
    script = read("web/static/js/content-studio.js")

    # 证据链展示：草稿箱有唯一匹配时给出明确文案
    assert "verification_evidence" in script
    assert "平台草稿箱已存在标题唯一匹配的草稿" in script
    assert "ev.summary" in script

    # 只读核验按钮与 API 调用
    assert "核验平台草稿" in script
    assert "async function verifyDraft(target)" in script
    assert "verify-draft" in script
    assert (
        "['FAILED', 'RESULT_UNKNOWN', 'DELIVERY_INCOMPLETE', "
        "'DRAFT_SAVED_WITH_WARNINGS']" in script
    )
    assert "PROBE_UNSUPPORTED_PLATFORM" in script
    assert "PROBE_NOT_FOUND" in script
    assert "PROBE_TITLE_AMBIGUOUS" in script
    # 核验必须只读：前端只 POST verify-draft，绝不触发执行接口
    assert "核验中…" in script


def test_primary_action_reports_and_focuses_missing_fields() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="create-plan"' in template
    assert "disabled" not in template.split('id="create-plan"', 1)[1].split(">", 1)[0]
    assert 'id="validation-summary"' in template
    assert "showValidation(issues)" in script
    assert "focusValidationIssue(issues[0])" in script
    assert "step: 'content-section'" in script
    assert "step: 'targets-section'" in script
    assert "填写文章标题" in script
    assert "至少选择一个投递目标" in script


def test_v2_draft_state_and_patch_never_silently_downgrade() -> None:
    script = read("web/static/js/content-studio.js")

    assert "function isV2Draft(draft = state.draft)" in script
    assert "content_schema_version: 2" in script
    assert "document: cloneValue(state.draft.document)" in script
    assert "cover: cloneValue(normalizedCover(state.draft.cover))" in script
    assert "const requestIsV2 = isV2Draft(requestDraft)" in script
    assert (
        "const requestBlocks = requestIsV2 ? null : publicBlocks(requestDraft.blocks);"
        in script
    )
    assert (
        "const requestDocument = requestIsV2 ? cloneValue(requestDraft.document) : null;"
        in script
    )
    assert "const requestBody = requestIsV2" in script
    assert "document: requestDocument" in script
    assert "cover: requestCover" in script
    assert "asset_id: value.strategy === 'EXPLICIT' ? value.asset_id : null" in script
    assert "const requestSnapshot = contentSnapshot(requestDraft);" in script
    assert (
        "const changedDuringRequest = contentSnapshot(requestDraft) !== requestSnapshot;"
        in script
    )
    assert "DRAFT_CONTENT_SCHEMA_CONFLICT" in script
    assert "editor.contentEditable = readonly ? 'false' : 'true';" in script
    assert "if (isV2Draft()) return [];" in script
    assert (
        "if (isV2Draft()) {\n"
        "            setMessage('content-error', v2ReadonlyMessage());\n"
        "            return;\n"
        "        }"
        in script
    )
    assert "assetUpload.disabled = readonly" in script
    assert "Word 富文档受保护，当前正文只读；重新导入可替换" in script
    assert "showDropOverlay(false);" in script


def test_v2_local_recovery_conflict_copy_and_title_stay_schema_aware() -> None:
    script = read("web/static/js/content-studio.js")

    assert "content_schema_version: schemaVersion" in script
    assert "document: schemaVersion === 2 ? cloneValue(snapshot.document) : null" in script
    assert "state.draft.document = cloneValue(local.document);" in script
    assert "const localMatchesSchema = serverIsV2" in script
    assert (
        "const localSameRevision = Boolean(local?.dirty && local.revision === payload.revision);"
        in script
    )
    assert "已保留本地副本且未静默降级" in script
    assert "state.draft.document.title = event.target.value;" in script
    assert (
        "content_schema_version: 2,\n                document: cloneValue(state.draft.document)"
        in script
    )
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


def test_docx_import_preserves_explicit_no_cover_default() -> None:
    script = read("web/static/js/content-studio.js")

    assert "maybeAutoSelectImportedCover" not in script
    import_body = script.split("async function importDocx(file)", 1)[1].split(
        "async function resolveConflict", 1
    )[0]
    assert "FIRST_BODY_IMAGE" not in import_body
    assert "openDraft(draft, { historyMode: 'replace' })" in import_body
    assert "state.coverAutoSelectionDismissed = automatic ? false : strategy === 'NONE';" in script
    assert (
        "state.coverAutoSelectionDismissed = "
        "Boolean(local.cover_auto_selection_dismissed);"
    ) in script


def test_studio_steps_are_clickable_and_keep_sections_close() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert template.count('class="studio-step-link"') == 3
    assert 'href="#targets-section"' in template
    assert 'data-step-target="content-section"' in template
    assert 'data-step-target="targets-section"' in template
    assert 'data-step-target="execution-section"' in template
    assert 'id="go-to-targets"' in template
    assert 'id="back-to-content"' in template
    assert 'id="go-to-execution"' in template
    assert "function scrollToStudioStep(targetId)" in script
    assert "window.addEventListener('hashchange', syncStudioStepFromLocation)" in script
    assert "prefers-reduced-motion: reduce" in script
    assert ".studio-progress { position: sticky;" in styles
    assert "pointer-events: none;" in styles
    assert ".studio-step-section { scroll-margin-top:" in styles


def test_platform_modes_follow_the_platform_switch_state() -> None:
    script = read("web/static/js/content-studio.js")
    styles = read("web/static/css/content-studio.css")

    assert "modeDraft.disabled = !on" in script
    assert "modePublish.disabled = !on" in script
    assert "toggle.disabled = !canDeliver" in script
    assert "if (!state.switcherToggles[platformId]) return;" in script
    assert ".target-mode-switch .mode-seg:disabled" in styles


def test_blank_workspace_uses_minimal_lazy_create_and_never_restores_indexeddb() -> None:
    script = read("web/static/js/content-studio.js")

    assert "function blankDraft()" in script
    assert "async function createPersistedDraft()" in script
    assert "if (!requestDraftId)" in script
    assert (
        "if (!state.localDb || !state.draft || !state.draft.draft_id) return Promise.resolve();"
        in script
    )
    assert (
        "const local = payload?.draft_id ? await localDraftGet(payload.draft_id) : null;"
        in script
    )
    assert "if (!(await createPersistedDraft())) return;" in script
    assert "state.draft.draft_id ? await localDraftGet" not in script


def test_history_navigation_reloads_the_explicit_location() -> None:
    script = read("web/static/js/content-studio.js")

    assert "async function openDraftFromLocation()" in script
    assert "window.addEventListener('popstate'" in script
    assert "await openDraftFromLocation();" in script
    assert "historyMode: 'push'" in script


def test_accounts_page_honors_platform_query_without_visual_refactor() -> None:
    script = read("web/static/js/account-sessions.js")

    assert "new URLSearchParams(window.location.search).get('platform')" in script
    assert "item.id === requestedPlatform && item.account_enabled" in script
    assert "selectPlatform(requested.id)" in script


def test_empty_valid_account_state_links_to_platform_account_management() -> None:
    script = read("web/static/js/content-studio.js")

    empty_branch = script.split("if (accounts.length === 0)", 1)[1].split(
        "return accounts.map", 1
    )[0]
    assert "accounts?platform=" in empty_branch
    assert "VALID 登录态" in empty_branch


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
    assert "state.plan.targets.some(isFormatBlockedTarget)" in script
    assert "为避免部分误投，本计划不会创建任何执行单" in script
