(function () {
    'use strict';

    const root = document.getElementById('content-studio');
    if (!root) return;

    const byId = id => document.getElementById(id);
    const sourceLabels = {
        BLANK: '空白草稿',
        DOCX: 'DOCX 导入',
        LEGACY_ARTICLE: '历史文章',
        SYSTEM_SEED: '系统草稿',
    };

    function draftSourceSummary(draft) {
        const sourceType = draft?.source_type || '';
        const source = sourceLabels[sourceType] || (sourceType ? '其他来源' : '草稿');
        if (sourceType !== 'DOCX' || !draft?.source_ref) return source;
        const sourceRef = String(draft.source_ref).split(/[\\/]+/).filter(Boolean).pop() || '';
        return sourceRef ? `${source} · 文件/来源：${sourceRef}` : source;
    }
    const planStatusLabels = {
        READY: '待执行', CREATING: '正在创建执行单', QUEUED: '已排队', RUNNING: '执行中', SUCCESS: '全部成功',
        PARTIAL_FAIL: '部分成功 / 部分失败', FATAL: '全部失败', CONFIRMATION_REQUIRED: '待公开确认',
        DRAFT_SAVED: '平台草稿已保存', PUBLISHED: '已公开发布', BLOCKED: '已拦截', FAILED: '失败',
        DRAFT_SAVED_WITH_WARNINGS: '草稿已保存（需核对）',
        PUBLISHED_WITH_WARNINGS: '已发布（需核对）',
        RESULT_UNKNOWN: '结果未知，需人工核对', FORMAT_REVIEW_REQUIRED: '待格式复核',
        DELIVERY_INCOMPLETE: '投递未完成',
        AWAITING_CONFIRMATION: '等待公开确认', EXECUTING: '执行中',
    };
    const sharedUiText = window.ArticleOpsUi || {};

    function localizedStatus(value) {
        return planStatusLabels[value]
            || sharedUiText.statusLabel?.(value)
            || (value ? '状态待确认' : '状态未知');
    }

    function localizedErrorMessage(code, message, fallback) {
        return sharedUiText.userFacingErrorMessage?.(code, message, fallback)
            || fallback
            || '操作未完成，请查看执行记录。';
    }
    const reLoginErrorCodes = new Set([
        'LOGIN_REQUIRED', 'SESSION_EXPIRED', 'ACCOUNT_SESSION_EXPIRED',
    ]);
    const MAX_PLAN_POLLS = 240;
    const formatErrorLabels = {
        CONTENT_FORMAT_UNSUPPORTED: '当前内容格式超出平台已验证能力',
        PLATFORM_FORMAT_CAPABILITIES_UNDECLARED: '平台格式能力尚未声明',
    };
    const state = {
        platforms: [],
        drafts: [],
        draft: null,

        accounts: [],
        selectedPlatform: null,
        selectedAccountId: null,
        accountRequestController: null,
        accountRequestSequence: 0,
        // iOS 风格目标选择器（竖排滑块）：平台开关 → 账号多选 → 每平台模式
        switcherToggles: {},
        switcherAccounts: {},
        switcherAccountDiagnostics: {},
        switcherSelected: {},
        switcherModes: {},
        switcherControllers: {},
        switcherSequences: {},
        switcherAccountLoadFailed: {},
        bulkAccountSelecting: false,
        accountRefreshPromise: null,
        lastAccountRefreshAt: 0,
        targetSaveDesired: [],
        targetSavePending: null,
        targetSavePromise: null,
        targetSaving: false,
        mutationPromise: null,
        draftLibraryLoading: false,
        navigationSequence: 0,
        saveTimer: null,
        saving: false,
        dirty: false,
        conflictServerDraft: null,
        plan: null,
        planBusy: false,
        pendingPublishTargets: [],
        activePublishTarget: null,
        pollCount: 0,
        pollTimer: null,
        verifyingOperations: new Set(),
        draggedBlockId: null,
        localDb: null,
        coverAutoSelectionDismissed: false,
        coverAutoNoteVisible: false,
    };

    function endpoint(template, key, value) {
        return template.replace(`{${key}}`, encodeURIComponent(value));
    }

    function uid() {
        return globalThis.crypto?.randomUUID?.() || `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function cloneValue(value) {
        if (value === null || value === undefined) return value;
        try { return JSON.parse(JSON.stringify(value)); } catch (_) { return value; }
    }

    function isV2Draft(draft = state.draft) {
        return Number(draft?.content_schema_version) === 2;
    }

    function assetUrl(assetId) {
        if (typeof assetId !== 'string' || !assetId.trim()) return '';
        return `/api/content-assets/${encodeURIComponent(assetId)}`;
    }

    function normalizedCover(cover) {
        const value = cover && typeof cover === 'object' ? cover : {};
        const strategy = ['NONE', 'FIRST_BODY_IMAGE', 'EXPLICIT'].includes(value.strategy)
            ? value.strategy : 'NONE';
        const assetId = typeof value.asset_id === 'string' && value.asset_id.trim()
            ? value.asset_id : null;
        return { strategy, asset_id: assetId, asset_url: assetUrl(assetId) };
    }

    function coverRequest(cover) {
        const value = normalizedCover(cover);
        return {
            strategy: value.strategy,
            asset_id: value.strategy === 'EXPLICIT' ? value.asset_id : null,
        };
    }

    function contentSnapshot(draft = state.draft) {
        if (!draft) return '';
        if (isV2Draft(draft)) {
            return JSON.stringify({
                content_schema_version: 2,
                title: draft.title || '',
                document: cloneValue(draft.document),
                cover: normalizedCover(draft.cover),
            });
        }
        return JSON.stringify({
            content_schema_version: 1,
            title: draft.title || '',
            blocks: publicBlocks(draft.blocks),
            cover: normalizedCover(draft.cover),
        });
    }

    function coreModal(id) {
        const library = window.coreui || window.bootstrap;
        return library.Modal.getOrCreateInstance(byId(id));
    }

    function setMessage(id, message) {
        const element = byId(id);
        if (!element) return;
        element.textContent = message || '';
        element.classList.toggle('d-none', !message);
    }

    async function jsonResponse(response) {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(payload.message || `请求失败（${response.status}）`);
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function setSaveState(kind, text) {
        const indicator = byId('save-indicator');
        indicator.className = `save-indicator is-${kind}`;
        byId('save-indicator-text').textContent = text;
        const saveButton = byId('save-draft-now');
        if (saveButton) {
            const canSave = kind === 'local' || kind === 'error';
            saveButton.disabled = !canSave;
            saveButton.setAttribute('aria-disabled', String(!canSave));
            saveButton.title = canSave ? '立即同步当前草稿' : text;
        }
    }

    function setCurrentStudioStep(stepNumber) {
        document.querySelectorAll('[data-studio-step]').forEach(item => {
            const current = Number(item.dataset.studioStep) === stepNumber;
            item.classList.toggle('is-current', current);
            const button = item.querySelector('.studio-step-link');
            if (button) {
                if (current) button.setAttribute('aria-current', 'step');
                else button.removeAttribute('aria-current');
            }
        });
    }

    function updateStudioProgress() {
        const hasContent = Boolean(state.draft?.title?.trim() && publicBlocks().length);
        const hasTargets = Boolean(state.draft?.targets?.length);
        const hasPlan = Boolean(state.plan);
        const stepStates = [
            [1, hasContent, hasContent ? '已完成' : '待完成'],
            [2, hasTargets, hasTargets ? '已选择' : '待选择'],
            [3, hasPlan, hasPlan ? '已生成' : '待生成'],
        ];
        stepStates.forEach(([number, complete, label]) => {
            const step = document.querySelector(`[data-studio-step="${number}"]`);
            step?.classList.toggle('is-complete', complete);
            const status = step?.querySelector('[data-step-status]');
            if (status) status.textContent = label;
        });
    }

    function scrollToStudioStep(targetId) {
        const section = byId(targetId);
        if (!section) return;
        const stepNumber = targetId === 'content-section' ? 1 : targetId === 'targets-section' ? 2 : 3;
        setCurrentStudioStep(stepNumber);
        section.scrollIntoView({
            behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
            block: 'start',
        });
        const heading = section.querySelector('h2');
        if (heading) {
            heading.setAttribute('tabindex', '-1');
            window.setTimeout(() => heading.focus({ preventScroll: true }), 250);
        }
    }

    function syncStudioStepFromLocation() {
        const targetId = window.location.hash.replace(/^#/, '');
        if (['content-section', 'targets-section', 'execution-section'].includes(targetId)) {
            setCurrentStudioStep(targetId === 'content-section' ? 1 : targetId === 'targets-section' ? 2 : 3);
        }
    }

    function focusValidationIssue(issue) {
        if (issue.step) scrollToStudioStep(issue.step);
        const target = byId(issue.focus);
        if (target?.matches?.('input, button, textarea, [contenteditable="true"], [tabindex]')) {
            window.setTimeout(() => target.focus({ preventScroll: false }), 280);
            return;
        }
        if (issue.focus === 'target-switcher-list') {
            const firstSwitch = target?.querySelector('input[type="checkbox"]:not(:disabled)');
            window.setTimeout(() => firstSwitch?.focus({ preventScroll: false }), 280);
        }
    }

    function openLocalDb(timeoutMs = 1500) {
        if (!window.indexedDB) return Promise.resolve(null);
        return new Promise(resolve => {
            let settled = false;
            const finish = database => {
                if (settled) {
                    database?.close?.();
                    return;
                }
                settled = true;
                clearTimeout(timer);
                resolve(database || null);
            };
            const timer = setTimeout(() => finish(null), timeoutMs);
            let request;
            try {
                request = indexedDB.open('articleops-content-studio', 1);
            } catch (_) {
                finish(null);
                return;
            }
            request.onupgradeneeded = () => {
                const database = request.result;
                if (!database.objectStoreNames.contains('drafts')) database.createObjectStore('drafts', { keyPath: 'draft_id' });
            };
            request.onsuccess = () => finish(request.result);
            request.onerror = () => finish(null);
            request.onblocked = () => finish(null);
        });
    }

    function localDraftGet(draftId) {
        if (!state.localDb) return Promise.resolve(null);
        return new Promise(resolve => {
            const request = state.localDb.transaction('drafts', 'readonly').objectStore('drafts').get(draftId);
            request.onsuccess = () => {
                const snapshot = request.result;
                if (!snapshot) { resolve(null); return; }
                const schemaVersion = Number(snapshot.content_schema_version) === 2 ? 2 : 1;
                resolve({
                    ...snapshot,
                    content_schema_version: schemaVersion,
                    document: schemaVersion === 2 ? cloneValue(snapshot.document) : null,
                    cover: snapshot.cover === undefined ? undefined : cloneValue(snapshot.cover),
                });
            };
            request.onerror = () => resolve(null);
        });
    }

    function localDraftPut(dirty = state.dirty) {
        if (!state.localDb || !state.draft || !state.draft.draft_id) return Promise.resolve();
        const snapshot = {
            draft_id: state.draft.draft_id,
            revision: state.draft.revision,
            title: state.draft.title,
            content_schema_version: Number(state.draft.content_schema_version) === 2 ? 2 : 1,
            document: isV2Draft() ? cloneValue(state.draft.document) : null,
            blocks: isV2Draft() ? [] : cloneValue(state.draft.blocks),
            cover: cloneValue(normalizedCover(state.draft.cover)),
            cover_auto_selection_dismissed: Boolean(state.coverAutoSelectionDismissed),
            targets: cloneValue(state.draft.targets || []),
            dirty,
            saved_at: new Date().toISOString(),
        };
        return new Promise(resolve => {
            const request = state.localDb.transaction('drafts', 'readwrite').objectStore('drafts').put(snapshot);
            request.onsuccess = () => resolve();
            request.onerror = () => resolve();
        });
    }

    function publicBlocks(blocks = state.draft?.blocks || []) {
        return blocks
            .filter(block => block.type === 'image' ? Boolean(block.asset_id) : Boolean((block.text || '').trim()))
            .map((block, position) => {
                const output = { block_id: block.block_id, type: block.type, position };
                if (block.type === 'text') output.text = block.text;
                if (block.type === 'image') {
                    output.asset_id = block.asset_id;
                    if (block.alt) output.alt = block.alt;
                }
                return output;
            });
    }

    function invalidatePlan() {
        state.plan = null;
        state.pendingPublishTargets = [];
        state.activePublishTarget = null;
        clearTimeout(state.pollTimer);
        state.pollCount = 0;
        byId('plan-result').classList.add('d-none');
    }

    function blankDraft() {
        return {
            draft_id: null,
            source_type: 'BLANK',
            source_ref: null,
            title: '',
            content_schema_version: 1,
            document: null,
            blocks: [],
            cover: normalizedCover({ strategy: 'NONE' }),
            status: 'UNSAVED',
            revision: null,
            targets: [],
            created_at: null,
            updated_at: null,
        };
    }

    function clearStudioMessages() {
        ['studio-fatal', 'content-error', 'target-builder-error', 'execution-error',
            'plan-review-error', 'publish-confirm-error'].forEach(id => setMessage(id, ''));
        byId('validation-summary')?.classList.add('d-none');
    }

    function requestDraftPayload(draft = state.draft) {
        if (!draft) return { title: '', blocks: [], cover: coverRequest({ strategy: 'NONE' }) };
        if (isV2Draft(draft)) {
            return {
                title: draft.title || '',
                content_schema_version: 2,
                document: cloneValue(draft.document),
                cover: coverRequest(draft.cover),
            };
        }
        return {
            title: draft.title || '',
            blocks: publicBlocks(draft.blocks),
            cover: coverRequest(draft.cover),
        };
    }

    async function createPersistedDraft() {
        if (!state.draft) return false;
        if (state.draft.draft_id) return true;
        const localSnapshot = contentSnapshot(state.draft);
        const payload = await jsonResponse(await fetch(root.dataset.draftsUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify(requestDraftPayload()),
        }));
        const changedDuringRequest = contentSnapshot(state.draft) !== localSnapshot;
        const currentDraft = state.draft;
        applyDraft(payload);
        if (changedDuringRequest) {
            state.draft.title = currentDraft.title;
            state.draft.blocks = cloneValue(currentDraft.blocks);
            state.draft.cover = normalizedCover(currentDraft.cover);
            state.draft.targets = cloneValue(currentDraft.targets || []);
            state.dirty = true;
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            renderCover();
            renderTargets();
            updateDraftMeta();
            setSaveState('local', '本地已保存');
            await localDraftPut(true);
            clearTimeout(state.saveTimer);
            state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
            return false;
        }
        state.dirty = false;
        await localDraftPut(false);
        setSaveState('synced', '已同步');
        return true;
    }

    async function markDirty() {
        if (!state.draft) return;
        state.dirty = true;
        invalidatePlan();
        setSaveState('local', '本地已保存');
        updateStudioProgress();
        await localDraftPut(true);
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
    }

    function enqueueDraftMutation(operation) {
        const previous = state.mutationPromise || Promise.resolve();
        const pending = previous.catch(() => undefined).then(operation);
        const tracked = pending.finally(() => {
            if (state.mutationPromise === tracked) state.mutationPromise = null;
        });
        state.mutationPromise = tracked;
        return tracked;
    }

    function saveDraftNow() {
        clearTimeout(state.saveTimer);
        return enqueueDraftMutation(saveDraftNowUnlocked);
    }

    async function saveDraftNowUnlocked() {
        if (!state.draft || !state.dirty || state.saving || state.conflictServerDraft) return !state.dirty;
        const requestDraft = state.draft;
        const requestDraftId = requestDraft.draft_id;
        const requestIsCurrent = () => state.draft === requestDraft
            && state.draft?.draft_id === requestDraftId;
        state.saving = true;
        setSaveState('saving', '正在同步');
        if (!requestDraftId) {
            try {
                return await createPersistedDraft();
            } catch (error) {
                setSaveState('error', '同步失败');
                setMessage('content-error', `${error.message || '草稿同步失败'}；本地恢复副本已保留。`);
                return false;
            } finally {
                state.saving = false;
            }
        }
        const baseRevision = requestDraft.revision;
        try {
            const requestTitle = requestDraft.title;
            const requestIsV2 = isV2Draft(requestDraft);
            const requestDocument = requestIsV2 ? cloneValue(requestDraft.document) : null;
            const requestBlocks = requestIsV2 ? null : publicBlocks(requestDraft.blocks);
            const requestCover = coverRequest(requestDraft.cover);
            const requestBody = requestIsV2
                ? {
                    revision: baseRevision,
                    title: requestTitle,
                    content_schema_version: 2,
                    document: requestDocument,
                    cover: requestCover,
                }
                : {
                    revision: baseRevision,
                    title: requestTitle,
                    blocks: requestBlocks,
                    cover: requestCover,
                };
            const requestSnapshot = contentSnapshot(requestDraft);
            const response = await fetch(endpoint(
                root.dataset.draftUrlTemplate,
                'draft_id',
                requestDraftId,
            ), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(requestBody),
            });
            const payload = await response.json().catch(() => ({}));
            if (!requestIsCurrent()) return true;
            if (response.status === 409 && ['DRAFT_REVISION_CONFLICT', 'DRAFT_CONTENT_SCHEMA_CONFLICT'].includes(payload.error)) {
                state.conflictServerDraft = payload.server_draft;
                byId('conflict-local-revision').textContent = String(baseRevision);
                byId('conflict-server-revision').textContent = String(payload.server_draft?.revision ?? '—');
                setSaveState('error', '同步冲突');
                setMessage('content-error', payload.error === 'DRAFT_CONTENT_SCHEMA_CONFLICT'
                    ? '当前草稿是 Word 富文档，旧版保存请求不能覆盖它；请选择采用服务端版本或另存副本。'
                    : '草稿发生修订冲突，请选择如何处理当前本地内容。');
                coreModal('conflict-modal').show();
                return false;
            }
            if (!response.ok) throw new Error(payload.message || '草稿同步失败');
            requestDraft.revision = payload.revision;
            requestDraft.updated_at = payload.updated_at;
            requestDraft.source_type = payload.source_type;
            const changedDuringRequest = contentSnapshot(requestDraft) !== requestSnapshot;
            if (!changedDuringRequest && requestIsV2 && Number(payload.content_schema_version) === 2) {
                requestDraft.content_schema_version = 2;
                requestDraft.document = cloneValue(payload.document);
                requestDraft.cover = normalizedCover(payload.cover);
            } else if (!changedDuringRequest && !requestIsV2) {
                requestDraft.cover = normalizedCover(payload.cover);
            }
            renderCover();
            state.dirty = changedDuringRequest;
            await localDraftPut(changedDuringRequest);
            if (!requestIsCurrent()) return true;
            updateDraftMeta();
            setSaveState(changedDuringRequest ? 'local' : 'synced', changedDuringRequest ? '本地已保存' : '已同步');
            if (changedDuringRequest) {
                clearTimeout(state.saveTimer);
                state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
            }
            return !changedDuringRequest;
        } catch (error) {
            if (!requestIsCurrent()) return true;
            setSaveState('error', '同步失败');
            setMessage('content-error', `${error.message}；本地恢复副本已保留。`);
            return false;
        } finally {
            state.saving = false;
        }
    }

    function restoreSwitcherSelection(targets) {
        state.switcherToggles = {};
        state.switcherSelected = {};
        state.switcherModes = {};
        for (const target of targets || []) {
            state.switcherToggles[target.platform] = true;
            state.switcherModes[target.platform] = target.mode;
            const selected = state.switcherSelected[target.platform]
                || (state.switcherSelected[target.platform] = []);
            if (!selected.includes(target.account_id)) selected.push(target.account_id);
        }
    }

    function applyDraft(payload, { render = true, historyMode = 'replace' } = {}) {
        const source = payload || blankDraft();
        const schemaVersion = Number(source.content_schema_version) === 2 ? 2 : 1;
        state.draft = {
            ...source,
            title: source.title || '',
            blocks: Array.isArray(source.blocks) ? source.blocks.map((block, position) => ({ ...block, position })) : [],
            content_schema_version: schemaVersion,
            document: schemaVersion === 2 ? cloneValue(source.document) : null,
            cover: normalizedCover(source.cover),
            targets: Array.isArray(source.targets) ? source.targets : [],
        };
        // 回填滑块状态：已有目标 → 平台开关/账号勾选/模式
        restoreSwitcherSelection(state.draft.targets);
        state.targetSaveDesired = cloneValue(state.draft.targets);
        state.targetSavePending = null;
        state.dirty = false;
        state.conflictServerDraft = null;
        state.coverAutoSelectionDismissed = false;
        state.coverAutoNoteVisible = false;
        invalidatePlan();
        if (render) {
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            renderCover();
            renderTargetSwitcher();
            renderTargets();
            updateDraftMeta();
        }
        const url = new URL(window.location.href);
        if (state.draft.draft_id) url.searchParams.set('draft_id', state.draft.draft_id);
        else url.searchParams.delete('draft_id');
        if (historyMode === 'push') history.pushState({}, '', url);
        else history.replaceState({}, '', url);
    }

    async function openDraft(payload, options = {}) {
        applyDraft(payload, options);
        const local = payload?.draft_id ? await localDraftGet(payload.draft_id) : null;
        const serverIsV2 = isV2Draft(state.draft);
        const localMatchesSchema = serverIsV2
            ? Number(local?.content_schema_version) === 2
            : Number(local?.content_schema_version || 1) !== 2;
        const localSameRevision = Boolean(local?.dirty && local.revision === payload.revision);
        if (local && local.revision === payload.revision && local.cover_auto_selection_dismissed !== undefined) {
            state.coverAutoSelectionDismissed = Boolean(local.cover_auto_selection_dismissed);
        }
        if (localSameRevision && !localMatchesSchema) {
            setSaveState('error', '恢复副本版本不一致');
            setMessage('content-error', '本地恢复副本与服务端内容版本不一致，已保留本地副本且未静默降级。请确认后再编辑或另存。');
        } else if (localSameRevision && localMatchesSchema) {
            state.draft.title = local.title || '';
            if (serverIsV2) {
                state.draft.content_schema_version = 2;
                state.draft.document = cloneValue(local.document);
                if (state.draft.document && typeof state.draft.document === 'object') {
                    state.draft.document.title = state.draft.title;
                }
                if (local.cover !== undefined) state.draft.cover = normalizedCover(local.cover);
            } else {
                state.draft.blocks = Array.isArray(local.blocks) ? local.blocks : [];
                if (local.cover !== undefined) state.draft.cover = normalizedCover(local.cover);
            }
            state.dirty = true;
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            renderCover();
            updateDraftMeta();
            setSaveState('local', '已恢复本地未同步内容');
            clearTimeout(state.saveTimer);
            state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
        } else if (payload?.draft_id) {
            await localDraftPut(false);
            setSaveState('synced', '已同步');
        } else {
            setSaveState('synced', '空白工作台（未保存）');
        }
        byId('studio-loading').classList.add('d-none');
        byId('studio-workspace').classList.remove('d-none');
    }

    function updateDraftMeta() {
        if (!state.draft) return;
        const source = sourceLabels[state.draft.source_type]
            || (state.draft.source_type ? '其他来源' : '草稿');
        const indicatorText = byId('save-indicator-text');
        if (indicatorText) {
             const sourceBadge = byId('draft-source-badge');
             if (sourceBadge) sourceBadge.textContent = source;
             const revElem = byId('draft-revision');
             if (revElem) revElem.textContent = state.draft.revision ? `修订 ${state.draft.revision}` : '未保存';
             const updatedElem = byId('draft-updated-at');
             if (updatedElem) updatedElem.textContent = state.draft.updated_at ? `更新于 ${formatDate(state.draft.updated_at)}` : '';
        }
        const sideTitle = byId('side-draft-title');
        if (sideTitle) {
            sideTitle.textContent = state.draft.title || '未命名草稿';
            byId('side-draft-source').textContent = source;
            const counts = isV2Draft()
                ? countV2Document(state.draft.document)
                : {
                    paragraphs: state.draft.blocks.filter(block => block.type === 'text').length,
                    headings: 0,
                    images: state.draft.blocks.filter(block => block.type === 'image').length,
                };
            byId('side-block-count').textContent = `${counts.paragraphs + counts.headings} 段 · ${counts.images} 图`;
            byId('side-target-count').textContent = String(state.draft.targets.length);
            byId('side-revision').textContent = state.draft.revision ? String(state.draft.revision) : '未保存';
        }
        updateStudioProgress();
    }

    function formatDate(value) {
        if (!value) return '—';
        const date = new Date(value);
        return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
    }

    function escapeHtml(value) {
        return String(value || '').replace(/[&<>"']/g, ch => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
        ));
    }

    function blocksToHtml(blocks) {
        return (blocks || []).map(block => {
            if (block.type === 'image') {
                const src = block.asset_url
                    || `/api/content-assets/${encodeURIComponent(block.asset_id)}`;
                return `<figure class="rich-image" data-asset-id="${encodeURIComponent(block.asset_id)}">`
                    + `<img src="${src}" alt="${escapeHtml(block.alt || '')}" `
                    + `data-asset-id="${encodeURIComponent(block.asset_id)}" loading="lazy">`
                    + `<figcaption>${escapeHtml(block.alt || '')}</figcaption></figure>`;
            }
            return `<p>${escapeHtml(block.text || '')}</p>`;
        }).join('');
    }

    const v2MarkTags = Object.freeze({
        bold: 'strong',
        italic: 'em',
        underline: 'u',
        strike: 's',
        code: 'code',
    });

    function v2Placeholder(message, block = false) {
        const element = document.createElement(block ? 'div' : 'span');
        element.className = 'rich-unsupported-node';
        element.setAttribute('role', 'note');
        element.textContent = message;
        return element;
    }

    function safeHttpHref(value) {
        if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return '';
        try {
            const parsed = new URL(value);
            return ['http:', 'https:'].includes(parsed.protocol) && parsed.hostname ? parsed.href : '';
        } catch (_) {
            return '';
        }
    }

    function safeDimension(value) {
        const number = Number(value);
        return Number.isInteger(number) && number > 0 && number <= 100000 ? number : null;
    }

    function appendV2Text(parent, node) {
        if (!node || typeof node.text !== 'string') {
            parent.appendChild(v2Placeholder('此文字内容暂无法预览'));
            return false;
        }
        const marks = node.marks === undefined ? [] : node.marks;
        if (!Array.isArray(marks) || marks.some(mark => typeof mark !== 'string' || !v2MarkTags[mark])) {
            parent.appendChild(v2Placeholder('此文字格式暂无法预览'));
            return false;
        }
        let content = document.createTextNode(node.text);
        for (const mark of marks) {
            const wrapper = document.createElement(v2MarkTags[mark]);
            wrapper.appendChild(content);
            content = wrapper;
        }
        if (node.link !== undefined && node.link !== null) {
            const href = safeHttpHref(node.link.href);
            if (!href) {
                parent.appendChild(v2Placeholder('此链接暂无法安全预览'));
                return false;
            }
            const link = document.createElement('a');
            link.href = href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            if (typeof node.link.title === 'string') link.title = node.link.title;
            link.appendChild(content);
            content = link;
        }
        parent.appendChild(content);
        return true;
    }

    function appendV2Image(parent, node) {
        const source = assetUrl(node?.asset_id);
        if (!source) {
            parent.appendChild(v2Placeholder('此图片缺少受控资产引用'));
            return false;
        }
        if (node.anchor !== undefined && node.anchor !== null
            && !['inline', 'floating'].includes(node.anchor.kind)) {
            parent.appendChild(v2Placeholder('此图片定位方式暂无法预览'));
            return false;
        }
        const width = node.width === undefined || node.width === null ? null : safeDimension(node.width);
        const height = node.height === undefined || node.height === null ? null : safeDimension(node.height);
        if ((node.width !== undefined && node.width !== null && width === null)
            || (node.height !== undefined && node.height !== null && height === null)) {
            parent.appendChild(v2Placeholder('此图片尺寸暂无法预览'));
            return false;
        }
        if (node.alt !== undefined && node.alt !== null && typeof node.alt !== 'string') {
            parent.appendChild(v2Placeholder('此图片说明暂无法预览'));
            return false;
        }
        if (node.caption !== undefined && node.caption !== null && typeof node.caption !== 'string') {
            parent.appendChild(v2Placeholder('此图片图注暂无法预览'));
            return false;
        }
        const figure = document.createElement('figure');
        figure.className = 'rich-image rich-image-v2';
        figure.dataset.assetId = node.asset_id;
        if (node.anchor?.kind) figure.dataset.anchorKind = node.anchor.kind;
        const image = document.createElement('img');
        image.src = source;
        image.alt = node.alt || '';
        image.loading = 'lazy';
        image.decoding = 'async';
        image.dataset.assetId = node.asset_id;
        if (width !== null) image.setAttribute('width', String(width));
        if (height !== null) image.setAttribute('height', String(height));
        figure.appendChild(image);
        if (node.caption) {
            const caption = document.createElement('figcaption');
            caption.textContent = node.caption;
            figure.appendChild(caption);
        }
        parent.appendChild(figure);
        return true;
    }

    function appendV2Inline(parent, node) {
        if (!node || typeof node !== 'object') {
            parent.appendChild(v2Placeholder('此内容节点暂无法预览'));
            return false;
        }
        if (node.kind === 'text') return appendV2Text(parent, node);
        if (node.kind === 'image') return appendV2Image(parent, node);
        parent.appendChild(v2Placeholder('此内容节点类型暂不支持预览'));
        return false;
    }

    function appendV2InlineChildren(parent, children) {
        if (!Array.isArray(children)) {
            parent.appendChild(v2Placeholder('此段落结构暂无法预览'));
            return false;
        }
        let rendered = false;
        for (const child of children) rendered = appendV2Inline(parent, child) || rendered;
        return rendered;
    }

    function safeTableSpan(value) {
        const number = Number(value === undefined || value === null ? 1 : value);
        return Number.isInteger(number) && number >= 1 && number <= 100 ? number : null;
    }

    function appendV2Block(parent, block, titleBlockId) {
        if (!block || typeof block !== 'object') {
            parent.appendChild(v2Placeholder('此正文节点暂无法预览', true));
            return true;
        }
        if (titleBlockId && block.block_id === titleBlockId) return false;
        if (block.kind === 'paragraph' || block.kind === 'heading') {
            const level = Number(block.level);
            if (block.kind === 'heading' && (!Number.isInteger(level) || level < 1 || level > 6)) {
                parent.appendChild(v2Placeholder('此标题层级暂无法预览', true));
                return true;
            }
            const element = document.createElement(block.kind === 'heading' ? `h${level}` : 'p');
            if (typeof block.style_name === 'string' && block.style_name) element.dataset.styleName = block.style_name;
            appendV2InlineChildren(element, block.children);
            parent.appendChild(element);
            return true;
        }
        if (block.kind === 'list') {
            if (typeof block.ordered !== 'boolean' || !Array.isArray(block.items)) {
                parent.appendChild(v2Placeholder('此列表结构暂无法预览', true));
                return true;
            }
            const list = document.createElement(block.ordered ? 'ol' : 'ul');
            for (const item of block.items) {
                const listItem = document.createElement('li');
                if (!item || typeof item !== 'object' || !Array.isArray(item.blocks)) {
                    listItem.appendChild(v2Placeholder('此列表项暂无法预览'));
                } else {
                    for (const nested of item.blocks) appendV2Block(listItem, nested, titleBlockId);
                }
                list.appendChild(listItem);
            }
            parent.appendChild(list);
            return true;
        }
        if (block.kind === 'table') {
            if (!Array.isArray(block.rows)) {
                parent.appendChild(v2Placeholder('此表格结构暂无法预览', true));
                return true;
            }
            const table = document.createElement('table');
            table.className = 'rich-table-v2';
            const body = document.createElement('tbody');
            for (const row of block.rows) {
                const tableRow = document.createElement('tr');
                if (!row || typeof row !== 'object' || !Array.isArray(row.cells)) {
                    const invalidCell = document.createElement('td');
                    invalidCell.appendChild(v2Placeholder('此表格行暂无法预览'));
                    tableRow.appendChild(invalidCell);
                } else {
                    for (const cell of row.cells) {
                        const tableCell = document.createElement('td');
                        const colspan = safeTableSpan(cell?.colspan);
                        const rowspan = safeTableSpan(cell?.rowspan);
                        if (!cell || typeof cell !== 'object' || !Array.isArray(cell.blocks)
                            || colspan === null || rowspan === null) {
                            tableCell.appendChild(v2Placeholder('此表格单元格暂无法预览'));
                        } else {
                            if (colspan > 1) tableCell.colSpan = colspan;
                            if (rowspan > 1) tableCell.rowSpan = rowspan;
                            for (const nested of cell.blocks) appendV2Block(tableCell, nested, titleBlockId);
                        }
                        tableRow.appendChild(tableCell);
                    }
                }
                body.appendChild(tableRow);
            }
            table.appendChild(body);
            parent.appendChild(table);
            return true;
        }
        parent.appendChild(v2Placeholder('此正文节点类型暂不支持预览', true));
        return true;
    }

    function countV2Document(documentValue) {
        const counts = { blocks: 0, paragraphs: 0, headings: 0, images: 0 };
        const titleBlockId = typeof documentValue?.title_block_id === 'string'
            ? documentValue.title_block_id : null;
        const countInline = children => {
            if (!Array.isArray(children)) return;
            for (const child of children) {
                if (child?.kind === 'image') counts.images += 1;
            }
        };
        const countBlock = block => {
            if (!block || typeof block !== 'object' || block.block_id === titleBlockId) return;
            if (block.kind === 'paragraph' || block.kind === 'heading') {
                counts.blocks += 1;
                if (block.kind === 'paragraph') counts.paragraphs += 1;
                else counts.headings += 1;
                countInline(block.children);
                return;
            }
            if (block.kind === 'list') {
                counts.blocks += 1;
                for (const item of block.items || []) for (const nested of item?.blocks || []) countBlock(nested);
                return;
            }
            if (block.kind === 'table') {
                counts.blocks += 1;
                for (const row of block.rows || []) for (const cell of row?.cells || []) {
                    for (const nested of cell?.blocks || []) countBlock(nested);
                }
                return;
            }
            counts.blocks += 1;
        };
        if (Array.isArray(documentValue?.blocks)) {
            for (const block of documentValue.blocks) countBlock(block);
        }
        return counts;
    }

    function renderV2Document(editor, documentValue) {
        editor.replaceChildren();
        const counts = countV2Document(documentValue);
        if (!documentValue || typeof documentValue !== 'object' || !Array.isArray(documentValue.blocks)) {
            editor.appendChild(v2Placeholder('v2 富文档暂无法预览，请重新导入 DOCX。', true));
            return counts;
        }
        let rendered = false;
        const titleBlockId = typeof documentValue.title_block_id === 'string'
            ? documentValue.title_block_id : null;
        for (const block of documentValue.blocks) {
            rendered = appendV2Block(editor, block, titleBlockId) || rendered;
        }
        if (!rendered) editor.appendChild(v2Placeholder('正文暂无可预览内容。', true));
        return counts;
    }

    function collectV2ImageCandidates(documentValue) {
        const candidates = [];
        const seen = new Set();
        const titleBlockId = typeof documentValue?.title_block_id === 'string'
            ? documentValue.title_block_id : null;
        const addImage = image => {
            if (!image || image.kind !== 'image' || typeof image.asset_id !== 'string') return;
            const source = assetUrl(image.asset_id);
            if (!source || seen.has(image.asset_id)) return;
            seen.add(image.asset_id);
            candidates.push({
                asset_id: image.asset_id,
                asset_url: source,
                alt: typeof image.alt === 'string' ? image.alt : '',
                caption: typeof image.caption === 'string' ? image.caption : '',
                width: image.width,
                height: image.height,
            });
        };
        const visitBlock = block => {
            if (!block || typeof block !== 'object' || block.block_id === titleBlockId) return;
            if (block.kind === 'paragraph' || block.kind === 'heading') {
                for (const child of block.children || []) addImage(child);
                return;
            }
            if (block.kind === 'list') {
                for (const item of block.items || []) for (const nested of item?.blocks || []) visitBlock(nested);
                return;
            }
            if (block.kind === 'table') {
                for (const row of block.rows || []) for (const cell of row?.cells || []) {
                    for (const nested of cell?.blocks || []) visitBlock(nested);
                }
            }
        };
        if (Array.isArray(documentValue?.blocks)) {
            for (const block of documentValue.blocks) visitBlock(block);
        }
        return candidates;
    }

    function contentImageCandidates() {
        if (!state.draft) return [];
        if (isV2Draft()) return collectV2ImageCandidates(state.draft.document);
        const seen = new Set();
        return state.draft.blocks
            .filter(block => block.type === 'image' && typeof block.asset_id === 'string')
            .filter(block => {
                if (seen.has(block.asset_id) || !assetUrl(block.asset_id)) return false;
                seen.add(block.asset_id);
                return true;
            })
            .map(block => ({
                asset_id: block.asset_id,
                asset_url: assetUrl(block.asset_id),
                alt: block.alt || '',
                caption: block.alt || '',
            }));
    }

    function renderCover() {
        const panel = byId('cover-panel');
        if (!panel || !state.draft) return;
        const candidates = contentImageCandidates();
        const cover = normalizedCover(state.draft.cover);
        const selectedId = cover.strategy === 'EXPLICIT'
            ? cover.asset_id
            : cover.strategy === 'FIRST_BODY_IMAGE' ? candidates[0]?.asset_id : null;
        const radioValues = {
            NONE: byId('cover-none'),
            FIRST_BODY_IMAGE: byId('cover-first-body-image'),
            EXPLICIT: byId('cover-explicit'),
        };
        for (const [strategy, radio] of Object.entries(radioValues)) {
            if (!radio) continue;
            radio.checked = cover.strategy === strategy;
            radio.disabled = strategy !== 'NONE' && candidates.length === 0;
        }
        const list = byId('cover-assets-list');
        list?.replaceChildren(...candidates.map(candidate => {
            const card = document.createElement('button');
            card.type = 'button';
            card.className = `cover-asset-card${selectedId === candidate.asset_id ? ' is-selected' : ''}`;
            card.dataset.assetId = candidate.asset_id;
            card.setAttribute('aria-pressed', String(selectedId === candidate.asset_id));
            card.setAttribute('aria-label', `指定${candidate.alt || candidate.caption || '正文图片'}为封面`);
            const image = document.createElement('img');
            image.src = assetUrl(candidate.asset_id);
            image.alt = candidate.alt || candidate.caption || '正文图片';
            image.loading = 'lazy';
            image.decoding = 'async';
            const label = document.createElement('span');
            label.textContent = candidate.caption || candidate.alt || '正文图片';
            card.append(image, label);
            card.addEventListener('click', () => setCoverStrategy('EXPLICIT', candidate.asset_id));
            return card;
        }));
        byId('cover-empty')?.classList.toggle('d-none', candidates.length > 0);
        const sourceNote = byId('cover-source-note');
        if (sourceNote) sourceNote.textContent = isV2Draft() ? '来自 Word 富文档图片' : '来自正文图片块';
        byId('cover-auto-note')?.classList.toggle('d-none', !state.coverAutoNoteVisible);
    }

    function setCoverStrategy(strategy, assetId = null, { automatic = false } = {}) {
        if (!state.draft || !['NONE', 'FIRST_BODY_IMAGE', 'EXPLICIT'].includes(strategy)) return false;
        const candidates = contentImageCandidates();
        const firstId = candidates[0]?.asset_id || null;
        const currentCover = normalizedCover(state.draft.cover);
        const currentExplicitId = currentCover.strategy === 'EXPLICIT' ? currentCover.asset_id : null;
        const resolvedId = strategy === 'EXPLICIT'
            ? assetId || currentExplicitId || firstId
            : strategy === 'FIRST_BODY_IMAGE' ? firstId : null;
        if (strategy !== 'NONE' && !resolvedId) return false;
        if (resolvedId && !candidates.some(candidate => candidate.asset_id === resolvedId)) return false;
        const previous = JSON.stringify(currentCover);
        const dismissedBefore = state.coverAutoSelectionDismissed;
        state.draft.cover = normalizedCover({ strategy, asset_id: resolvedId });
        state.coverAutoSelectionDismissed = automatic ? false : strategy === 'NONE';
        state.coverAutoNoteVisible = automatic;
        const changed = previous !== JSON.stringify(state.draft.cover)
            || dismissedBefore !== state.coverAutoSelectionDismissed;
        renderCover();
        if (changed) markDirty();
        return changed;
    }

    function v2ReadonlyMessage() {
        return 'Word 富文档受保护，当前正文只读；重新导入可替换';
    }

    function syncEditorMode() {
        const readonly = isV2Draft();
        const editor = byId('rich-editor');
        if (editor) {
            editor.contentEditable = readonly ? 'false' : 'true';
            editor.setAttribute('aria-readonly', String(readonly));
            editor.classList.toggle('is-v2-readonly', readonly);
        }
        const assetUpload = byId('asset-upload');
        if (assetUpload) assetUpload.disabled = readonly;
        const assetTrigger = byId('asset-upload-trigger');
        if (assetTrigger) {
            assetTrigger.classList.toggle('v2-readonly-control', readonly);
            assetTrigger.setAttribute('aria-disabled', String(readonly));
        }
        const clearButton = byId('clear-blocks');
        if (clearButton) {
            clearButton.disabled = readonly || !state.draft?.blocks?.length;
            clearButton.setAttribute('aria-disabled', String(readonly));
        }
        const notice = byId('v2-readonly-notice');
        notice?.classList.toggle('d-none', !readonly);
    }

    function renderBlocks() {
        const editor = byId('rich-editor');
        syncEditorMode();
        if (editor) {
            if (isV2Draft()) renderV2Document(editor, state.draft.document);
            else editor.innerHTML = blocksToHtml(state.draft.blocks);
        }
        syncBlocksMeta();
    }

    function syncBlocksMeta() {
        const blocks = state.draft ? state.draft.blocks : [];
        const counts = isV2Draft()
            ? countV2Document(state.draft.document)
            : {
                blocks: blocks.length,
                paragraphs: blocks.filter(block => block.type === 'text').length,
                headings: 0,
                images: blocks.filter(block => block.type === 'image').length,
            };
        byId('blocks-empty').classList.toggle('d-none', counts.blocks > 0);
        byId('block-count').textContent = `${counts.paragraphs + counts.headings} 段 · ${counts.images} 图`;
        const clearButton = byId('clear-blocks');
        if (clearButton) {
            clearButton.disabled = isV2Draft() || counts.blocks === 0;
            clearButton.setAttribute('aria-disabled', String(isV2Draft()));
        }
        updateDraftMeta();
    }

    function parseEditorToBlocks() {
        if (isV2Draft()) return [];
        const editor = byId('rich-editor');
        if (!editor) return [];
        const blocks = [];
        let position = 0;
        for (const node of editor.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                const text = (node.textContent || '').replace(/\n+$/g, '');
                if (text.trim()) blocks.push({ block_id: uid(), type: 'text', text, position: position++ });
                continue;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) continue;
            const img = node.tagName === 'IMG' ? node : node.querySelector('img');
            if (img && img.dataset.assetId) {
                blocks.push({
                    block_id: uid(), type: 'image',
                    asset_id: img.dataset.assetId, alt: img.alt || '文档图片',
                    position: position++,
                });
                continue;
            }
            const text = (node.innerText || '').replace(/\n+$/g, '');
            if (text.trim()) blocks.push({ block_id: uid(), type: 'text', text, position: position++ });
        }
        return blocks;
    }

    let editorParseTimer = null;
    function handleEditorInput() {
        if (isV2Draft()) {
            setMessage('content-error', v2ReadonlyMessage());
            return;
        }
        clearTimeout(editorParseTimer);
        editorParseTimer = setTimeout(() => {
            if (!state.draft) return;
            const blocks = parseEditorToBlocks();
            const current = state.draft.blocks;
            const same = current.length === blocks.length && current.every((block, index) =>
                block.type === blocks[index].type
                && (block.type === 'image'
                    ? block.asset_id === blocks[index].asset_id
                    : block.text === blocks[index].text));
            if (same) return;
            state.draft.blocks = blocks;
            syncBlocksMeta();
            markDirty();
        }, 600);
    }

    function clearAllBlocks() {
        if (isV2Draft()) {
            setMessage('content-error', v2ReadonlyMessage());
            return;
        }
        if (!state.draft || state.draft.blocks.length === 0) return;
        if (!window.confirm('确定清空正文全部内容吗？清空后可重新拖入 Word 文档或添加图文块。')) return;
        state.draft.blocks = [];
        renderBlocks();
        markDirty();
    }

    function insertImageIntoEditor(asset) {
        if (isV2Draft()) {
            setMessage('content-error', v2ReadonlyMessage());
            return;
        }
        const editor = byId('rich-editor');
        const figure = document.createElement('figure');
        figure.className = 'rich-image';
        const img = document.createElement('img');
        img.src = asset.asset_url || `/api/content-assets/${encodeURIComponent(asset.asset_id)}`;
        img.alt = '';
        img.dataset.assetId = asset.asset_id;
        img.loading = 'lazy';
        figure.appendChild(img);
        const selection = window.getSelection();
        if (selection && selection.rangeCount && editor.contains(selection.anchorNode)) {
            const range = selection.getRangeAt(0);
            range.deleteContents();
            range.insertNode(figure);
            range.setStartAfter(figure);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
        } else {
            editor.appendChild(figure);
        }
    }

    async function uploadAssets(files) {
        if (isV2Draft()) {
            setMessage('content-error', v2ReadonlyMessage());
            return;
        }
        if (!state.draft || !files.length) return;
        setMessage('content-error', '');
        setSaveState('saving', '正在上传图片');
        try {
            if (!(await createPersistedDraft())) return;
            for (const file of files) {
                const form = new FormData(); form.append('file', file);
                const response = await fetch(endpoint(root.dataset.assetsUrlTemplate, 'draft_id', state.draft.draft_id), { method: 'POST', body: form, headers: { Accept: 'application/json' } });
                const asset = await jsonResponse(response);
                insertImageIntoEditor(asset);
            }
            // 图片已插入编辑器 DOM，立即解析同步（不等防抖）
            state.draft.blocks = parseEditorToBlocks();
            syncBlocksMeta();
            await markDirty();
        } catch (error) {
            setMessage('content-error', error.message || '图片上传失败');
            setSaveState('error', '图片上传失败');
        }
    }

    async function fetchPlatforms() {
        const loading = byId('platform-grid-loading');
        const list = byId('target-switcher-list');
        try {
            const response = await fetch(root.dataset.platformsUrl, { headers: { Accept: 'application/json' } });
            const data = await jsonResponse(response);
            state.platforms = (Array.isArray(data) ? data : (data.platforms || []))
                .filter(platform => platform && typeof platform.id === 'string')
                .sort((left, right) => Number(left.sort_order || 0) - Number(right.sort_order || 0));
            renderTargetSwitcher();
        } catch (error) {
            setMessage('target-builder-error', `平台目录加载失败：${error.message || '未知错误'}`);
        } finally {
            loading?.classList.add('d-none');
            list?.classList.remove('d-none');
        }
    }

    function platformLabel(id) {
        const platform = state.platforms.find(item => item.id === id);
        return platform ? platform.display_name : id;
    }

    function deliverablePlatforms() {
        return state.platforms.filter(platform => platform.delivery_enabled);
    }

    function enabledPlatformIds() {
        return deliverablePlatforms()
            .filter(platform => state.switcherToggles[platform.id])
            .map(platform => platform.id);
    }

    function selectedSwitcherAccountCount(platformIds = enabledPlatformIds()) {
        return platformIds.reduce(
            (total, platformId) => total + (state.switcherSelected[platformId] || []).length,
            0,
        );
    }

    function allLoadedAccountsSelected(platformIds) {
        if (platformIds.some(platformId => state.switcherAccountLoadFailed[platformId])) {
            return false;
        }
        const accountIds = platformIds.flatMap(platformId =>
            (state.switcherAccounts[platformId] || []).map(account => account.account_id));
        return accountIds.length > 0 && platformIds.every(platformId => {
            const selected = state.switcherSelected[platformId] || [];
            return (state.switcherAccounts[platformId] || [])
                .every(account => selected.includes(account.account_id));
        });
    }

    function updateBulkTargetControls() {
        const deliverable = deliverablePlatforms();
        const enabledIds = enabledPlatformIds();
        const allPlatformsEnabled = deliverable.length > 0
            && deliverable.every(platform => state.switcherToggles[platform.id]);
        const platformButton = byId('toggle-all-platforms');
        const accountButton = byId('toggle-all-accounts');
        const selectedAccounts = selectedSwitcherAccountCount(enabledIds);
        const loadedCount = enabledIds.filter(platformId =>
            Array.isArray(state.switcherAccounts[platformId])
            && !state.switcherAccountLoadFailed[platformId]).length;

        if (platformButton) {
            platformButton.disabled = deliverable.length === 0 || state.bulkAccountSelecting;
            platformButton.textContent = allPlatformsEnabled
                ? '取消全选所有平台' : '全选所有平台';
        }
        if (accountButton) {
            accountButton.disabled = enabledIds.length === 0 || state.bulkAccountSelecting;
            accountButton.textContent = allLoadedAccountsSelected(enabledIds)
                ? '取消全选所有账户' : '全选所有账户';
            accountButton.title = enabledIds.length === 0
                ? '请先选择至少一个可投递平台'
                : state.bulkAccountSelecting ? '正在加载可用账号' : '';
        }
        const summary = byId('target-selection-summary');
        if (summary) {
            summary.textContent = `已选平台 ${enabledIds.length} 个 · 账户 ${selectedAccounts} 个`;
        }
        const progress = byId('account-load-progress');
        if (progress) {
            progress.textContent = enabledIds.length === 0
                ? '请先选择平台，再批量选择登录状态有效的账户'
                : state.bulkAccountSelecting
                    ? `正在加载账号 ${loadedCount}/${enabledIds.length}`
                    : `账号已加载 ${loadedCount}/${enabledIds.length}`;
        }
    }

    // ===== iOS 风格目标选择器：竖排滑块 =====

    function switcherMode(platformId) {
        return state.switcherModes[platformId] || 'DRAFT';
    }

    function switcherPlatformIcon(platform) {
        const icon = document.createElement('span');
        icon.className = 'target-platform-icon';
        const img = document.createElement('img');
        img.src = platform.logo_url || '';
        img.alt = '';
        img.setAttribute('aria-hidden', 'true');
        img.addEventListener('error', () => {
            img.remove();
            const fallback = document.createElement('span');
            fallback.className = 'platform-icon-fallback';
            fallback.textContent = (platform.display_name || platform.id).slice(0, 2);
            icon.appendChild(fallback);
        }, { once: true });
        icon.appendChild(img);
        return icon;
    }

    function renderTargetSwitcher() {
        const list = byId('target-switcher-list');
        if (!list) return;
        list.replaceChildren(...state.platforms.map(platform => switcherRow(platform)));
        const deliverable = state.platforms.filter(platform => platform.delivery_enabled).length;
        const accountOnly = state.platforms.filter(platform => !platform.delivery_enabled && platform.account_enabled).length;
        const comingSoon = state.platforms.length - deliverable - accountOnly;
        const summary = byId('platform-capability-summary');
        if (summary) summary.textContent = `${deliverable} 个可投递 · ${accountOnly} 个仅账号管理 · ${comingSoon} 个即将接入`;
        byId('targets-empty').classList.toggle('d-none', (state.draft?.targets || []).length > 0);
        updateBulkTargetControls();
    }

    function platformCapability(platform) {
        if (platform.delivery_enabled) return { label: '可投递', className: 'is-deliverable' };
        if (platform.account_enabled) return { label: '仅账号管理', className: 'is-account-only' };
        return { label: '即将接入', className: 'is-coming-soon' };
    }

    function switcherRow(platform) {
        const canDeliver = Boolean(platform.delivery_enabled);
        const on = canDeliver && Boolean(state.switcherToggles[platform.id]);
        const capability = platformCapability(platform);
        const row = document.createElement('div');
        row.className = `target-platform-row ${capability.className}${on ? ' is-on' : ''}`;
        row.dataset.platformId = platform.id;

        // 头部：图标 + 名称 + 模式滑块（草稿/公开）+ 平台开关
        const head = document.createElement('div');
        head.className = 'target-platform-head';
        head.append(switcherPlatformIcon(platform));
        const name = document.createElement('strong');
        name.className = 'target-platform-name';
        name.textContent = platform.display_name;
        head.appendChild(name);

        const capabilityBadge = document.createElement('span');
        capabilityBadge.className = 'target-capability-badge';
        capabilityBadge.textContent = capability.label;
        head.appendChild(capabilityBadge);

        const mode = document.createElement('div');
        mode.className = 'target-mode-switch';
        mode.setAttribute('role', 'group');
        mode.setAttribute('aria-label', `${platform.display_name} 投递模式`);
        const modeDraft = document.createElement('button');
        modeDraft.type = 'button';
        modeDraft.className = `mode-seg${switcherMode(platform.id) === 'DRAFT' ? ' is-active' : ''}`;
        modeDraft.textContent = '平台草稿';
        modeDraft.dataset.mode = 'DRAFT';
        modeDraft.disabled = !on;
        const modePublish = document.createElement('button');
        modePublish.type = 'button';
        modePublish.className = `mode-seg${switcherMode(platform.id) === 'PUBLISH' ? ' is-active' : ''}`;
        modePublish.textContent = '公开发布';
        modePublish.dataset.mode = 'PUBLISH';
        modePublish.disabled = !on;
        modeDraft.addEventListener('click', () => setSwitcherMode(platform.id, 'DRAFT'));
        modePublish.addEventListener('click', () => setSwitcherMode(platform.id, 'PUBLISH'));
        mode.append(modeDraft, modePublish);
        head.appendChild(mode);

        const switchWrap = document.createElement('div');
        switchWrap.className = 'form-check form-switch target-platform-switch m-0';
        const toggle = document.createElement('input');
        toggle.type = 'checkbox';
        toggle.className = 'form-check-input';
        toggle.role = 'switch';
        toggle.checked = on;
        toggle.disabled = !canDeliver;
        toggle.setAttribute('aria-label', `启用${platform.display_name}投递`);
        toggle.title = canDeliver ? `启用${platform.display_name}并选择账号` : capability.label;
        toggle.addEventListener('change', () => togglePlatform(platform.id, toggle.checked));
        switchWrap.appendChild(toggle);
        head.appendChild(switchWrap);
        if (canDeliver) {
            head.classList.add('is-clickable');
            head.addEventListener('click', event => {
                if (event.target.closest(
                    'button, input, a, label, .target-mode-switch, .target-platform-switch',
                )) return;
                togglePlatform(platform.id, !on);
            });
        }
        row.appendChild(head);

        // 展开区：账号多选
        const body = document.createElement('div');
        body.className = 'target-platform-body';
        if (!canDeliver) {
            const message = document.createElement('div');
            message.className = 'target-unavailable-message';
            message.textContent = platform.account_enabled
                ? '当前版本暂不参与投递，可先在账号页维护登录态。'
                : '平台入口已预留，适配器与账号能力尚未开放。';
            body.appendChild(message);
            if (platform.account_enabled) {
                const link = document.createElement('a');
                link.className = 'btn btn-sm btn-outline-secondary';
                link.href = `/accounts?platform=${encodeURIComponent(platform.id)}`;
                link.textContent = '前往账号管理';
                body.appendChild(link);
            }
        } else if (on) {
            if (state.switcherAccounts[platform.id] === undefined) {
                state.switcherAccounts[platform.id] = null;
                body.appendChild(switcherBodyMessage('正在读取账号…'));
                loadSwitcherAccounts(platform.id);
            } else if (state.switcherAccounts[platform.id] === null) {
                body.appendChild(switcherBodyMessage('正在读取账号…'));
            } else {
                body.append(...switcherAccountChecks(platform.id));
            }
        }
        row.appendChild(body);
        return row;
    }

    function switcherBodyMessage(text) {
        const el = document.createElement('div');
        el.className = 'target-accounts-loading';
        el.textContent = text;
        return el;
    }

    function switcherAccountChecks(platformId) {
        const accounts = state.switcherAccounts[platformId] || [];
        const selected = state.switcherSelected[platformId] || [];
        if (accounts.length === 0) {
            const unavailable = state.switcherAccountDiagnostics[platformId] || [];
            const empty = document.createElement('div');
            empty.className = 'target-accounts-empty';
            if (unavailable.length) {
                const labels = {
                    VERIFYING: '正在验证',
                    UNVERIFIED: '尚未验证',
                    LOGIN_REQUIRED: '需要重新登录',
                    EXPIRED: '登录态已过期',
                    ERROR: '验证异常',
                    BUSY: '账号正忙',
                };
                const states = Array.from(new Set(unavailable.map(account =>
                    labels[account.session_status]
                        || sharedUiText.statusLabel?.(account.session_status)
                        || '状态未知')));
                empty.textContent = `检测到 ${unavailable.length} 个账号，但当前状态为“${states.join('、')}”，尚不能投递。`;
            } else {
                empty.textContent = '该平台暂无可用账号（需要有效登录状态）。';
            }
            const refresh = document.createElement('button');
            refresh.type = 'button';
            refresh.className = 'btn btn-sm btn-outline-primary';
            refresh.textContent = '刷新账号状态';
            refresh.addEventListener('click', () => loadSwitcherAccounts(
                platformId,
                { reconcile: false },
            ));
            const link = document.createElement('a');
            link.className = 'btn btn-sm btn-outline-secondary';
            link.href = `/accounts?platform=${encodeURIComponent(platformId)}`;
            link.textContent = '前往账号管理';
            return [empty, refresh, link];
        }
        return accounts.map(account => {
            const wrap = document.createElement('label');
            wrap.className = 'form-check target-account-check';
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'form-check-input';
            box.checked = selected.includes(account.account_id);
            box.setAttribute('aria-label', `选择账号 ${account.display_name}`);
            box.addEventListener('change', () => toggleSwitcherAccount(platformId, account.account_id, box.checked));
            const text = document.createElement('span');
            text.className = 'form-check-label';
            text.textContent = `${account.display_name || '未命名账号'}${account.masked_platform_user_id ? ` · ${account.masked_platform_user_id}` : ''}`;
            wrap.append(box, text);
            return wrap;
        });
    }

    function reconcileLoadedAccountSelection(platformId, accounts, { apply = true } = {}) {
        const selected = state.switcherSelected[platformId] || [];
        const validIds = new Set(accounts.map(account => account.account_id));
        const invalidIds = selected.filter(accountId => !validIds.has(accountId));
        if (apply) {
            state.switcherSelected[platformId] = selected
                .filter(accountId => validIds.has(accountId));
        }
        if (invalidIds.length && apply) {
            setMessage(
                'target-builder-error',
                `${platformLabel(platformId)}有 ${invalidIds.length} 个历史目标账号当前登录状态无效，已从本次选择中移除；历史投递记录不受影响。`,
            );
        }
        return invalidIds;
    }

    function clearSwitcherAccountError(platformId) {
        const error = byId('target-builder-error');
        if (!error) return;
        const text = error.textContent || '';
        const label = platformLabel(platformId);
        const isAccountLoadError = text.startsWith(`加载${label}账号失败：`);
        const isInvalidSelectionError = text.startsWith(`${label}有 `)
            && text.includes('当前登录状态无效');
        if (isAccountLoadError || isInvalidSelectionError) {
            setMessage('target-builder-error', '');
        }
    }

    async function loadSwitcherAccounts(
        platformId,
        { render = true, reconcile = render } = {},
    ) {
        const sequence = (state.switcherSequences[platformId] || 0) + 1;
        state.switcherSequences[platformId] = sequence;
        state.switcherControllers[platformId]?.abort();
        const controller = new AbortController();
        state.switcherControllers[platformId] = controller;
        if (state.switcherAccounts[platformId] === undefined) {
            state.switcherAccounts[platformId] = null;
        }
        updateBulkTargetControls();
        try {
            const url = endpoint(root.dataset.accountsUrlTemplate, 'platform', platformId);
            const payload = await jsonResponse(await fetch(url, {
                signal: controller.signal,
                cache: 'no-store',
                headers: { Accept: 'application/json' },
            }));
            if (sequence !== state.switcherSequences[platformId]) return null;
            if (payload.platform && payload.platform !== platformId) throw new Error('账号响应与所选平台不匹配');
            const allAccounts = Array.isArray(payload.accounts) ? payload.accounts : [];
            state.switcherAccountDiagnostics[platformId] = allAccounts.filter(account =>
                account.status === 'ACTIVE' && account.session_status !== 'VALID');
            state.switcherAccounts[platformId] = allAccounts.filter(account =>
                account.status === 'ACTIVE' && account.session_status === 'VALID');
            state.switcherAccountLoadFailed[platformId] = false;
            clearSwitcherAccountError(platformId);
            const invalidIds = reconcileLoadedAccountSelection(
                platformId,
                state.switcherAccounts[platformId],
                { apply: reconcile },
            );
            if (render) reRenderSwitcherRow(platformId);
            if (render && reconcile && invalidIds.length) rebuildTargets();
        } catch (error) {
            if (error.name === 'AbortError' || sequence !== state.switcherSequences[platformId]) return null;
            state.switcherAccounts[platformId] = [];
            state.switcherAccountDiagnostics[platformId] = [];
            state.switcherAccountLoadFailed[platformId] = true;
            setMessage('target-builder-error', `加载${platformLabel(platformId)}账号失败：${error.message || '未知错误'}`);
            if (render) reRenderSwitcherRow(platformId);
        } finally {
            if (state.switcherControllers[platformId] === controller) delete state.switcherControllers[platformId];
            updateBulkTargetControls();
        }
        return state.switcherAccountLoadFailed[platformId]
            ? null
            : (state.switcherAccounts[platformId] || []);
    }

    async function refreshEnabledSwitcherAccounts({ force = false } = {}) {
        if (document.hidden || state.accountRefreshPromise) return state.accountRefreshPromise;
        const platformIds = enabledPlatformIds();
        if (!platformIds.length) return null;
        const now = Date.now();
        if (!force && now - state.lastAccountRefreshAt < 1000) return null;
        state.lastAccountRefreshAt = now;
        state.accountRefreshPromise = Promise.all(platformIds.map(platformId =>
            loadSwitcherAccounts(platformId, { render: false, reconcile: false })))
            .finally(() => {
                state.accountRefreshPromise = null;
                renderTargetSwitcher();
            });
        return state.accountRefreshPromise;
    }

    function reRenderSwitcherRow(platformId) {
        const list = byId('target-switcher-list');
        if (!list) return;
        const row = list.querySelector(`.target-platform-row[data-platform-id="${platformId}"]`);
        const platform = state.platforms.find(item => item.id === platformId);
        if (row && platform) row.replaceWith(switcherRow(platform));
        updateBulkTargetControls();
    }

    function toggleAllPlatforms() {
        const deliverable = deliverablePlatforms();
        if (!deliverable.length) return;
        const turnOff = deliverable.every(platform => state.switcherToggles[platform.id]);
        const nextToggles = { ...state.switcherToggles };
        const nextSelected = { ...state.switcherSelected };
        for (const platform of deliverable) {
            nextToggles[platform.id] = !turnOff;
            delete state.switcherAccounts[platform.id];
            delete state.switcherAccountDiagnostics[platform.id];
            if (turnOff) {
                state.switcherControllers[platform.id]?.abort();
                delete state.switcherControllers[platform.id];
                if (state.switcherAccounts[platform.id] === null) {
                    delete state.switcherAccounts[platform.id];
                }
                if (state.switcherAccountLoadFailed[platform.id]) {
                    delete state.switcherAccounts[platform.id];
                    delete state.switcherAccountLoadFailed[platform.id];
                }
                nextSelected[platform.id] = [];
            }
        }
        state.switcherToggles = nextToggles;
        state.switcherSelected = nextSelected;
        renderTargetSwitcher();
        rebuildTargets();
    }

    async function toggleAllAccounts() {
        const platformIds = enabledPlatformIds();
        if (!platformIds.length || state.bulkAccountSelecting) return;
        const alreadySelected = platformIds.every(platformId =>
            Array.isArray(state.switcherAccounts[platformId])
            && !state.switcherAccountLoadFailed[platformId])
            && allLoadedAccountsSelected(platformIds);
        if (alreadySelected) {
            const nextSelected = { ...state.switcherSelected };
            for (const platformId of platformIds) nextSelected[platformId] = [];
            state.switcherSelected = nextSelected;
            renderTargetSwitcher();
            rebuildTargets();
            return;
        }

        state.bulkAccountSelecting = true;
        updateBulkTargetControls();
        let allAccountsLoaded = true;
        try {
            const loaded = await Promise.all(platformIds.map(async platformId => {
                const accounts = await loadSwitcherAccounts(platformId, { render: false });
                return [platformId, accounts];
            }));
            allAccountsLoaded = loaded.every(([, accounts]) => Array.isArray(accounts));
            if (allAccountsLoaded) {
                const nextSelected = { ...state.switcherSelected };
                for (const [platformId, accounts] of loaded) {
                    nextSelected[platformId] = accounts.map(account => account.account_id);
                }
                state.switcherSelected = nextSelected;
            }
        } finally {
            state.bulkAccountSelecting = false;
        }
        renderTargetSwitcher();
        if (!allAccountsLoaded) {
            setMessage(
                'target-builder-error',
                '部分平台账号加载失败，本次全选未保存；请检查账号状态后重试。',
            );
            return;
        }
        rebuildTargets();
    }

    function togglePlatform(platformId, checked) {
        const platform = state.platforms.find(item => item.id === platformId);
        if (!platform?.delivery_enabled) return;
        state.switcherToggles[platformId] = checked;
        delete state.switcherAccounts[platformId];
        delete state.switcherAccountDiagnostics[platformId];
        if (!checked) {
            state.switcherControllers[platformId]?.abort();
            delete state.switcherControllers[platformId];
            if (state.switcherAccounts[platformId] === null) delete state.switcherAccounts[platformId];
            if (state.switcherAccountLoadFailed[platformId]) {
                delete state.switcherAccounts[platformId];
                delete state.switcherAccountLoadFailed[platformId];
            }
            state.switcherSelected[platformId] = [];
        }
        reRenderSwitcherRow(platformId);
        rebuildTargets();
    }

    function toggleSwitcherAccount(platformId, accountId, checked) {
        const selected = state.switcherSelected[platformId] || (state.switcherSelected[platformId] = []);
        if (checked) {
            if (!selected.includes(accountId)) selected.push(accountId);
        } else {
            const index = selected.indexOf(accountId);
            if (index >= 0) selected.splice(index, 1);
        }
        updateBulkTargetControls();
        rebuildTargets();
    }

    function setSwitcherMode(platformId, mode) {
        if (!state.switcherToggles[platformId]) return;
        state.switcherModes[platformId] = mode;
        reRenderSwitcherRow(platformId);
        rebuildTargets();
    }

    function rebuildTargets() {
        if (!state.draft) return;
        const targets = [];
        for (const platform of state.platforms) {
            if (!platform.delivery_enabled || !state.switcherToggles[platform.id]) continue;
            const mode = switcherMode(platform.id);
            const accounts = state.switcherAccounts[platform.id] || [];
            const accountsLoading = !Array.isArray(state.switcherAccounts[platform.id])
                || state.switcherAccountLoadFailed[platform.id];
            const existingTargets = (state.draft.targets || [])
                .filter(target => target.platform === platform.id);
            for (const accountId of state.switcherSelected[platform.id] || []) {
                const account = accounts.find(item => item.account_id === accountId);
                const existingTarget = existingTargets
                    .find(target => target.account_id === accountId);
                if (!account && accountsLoading && existingTarget) {
                    targets.push({
                        ...existingTarget,
                        mode,
                    });
                    continue;
                }
                if (!account) continue;
                targets.push({
                    target_id: existingTarget?.target_id,
                    platform: platform.id,
                    account_id: accountId,
                    mode,
                    persist_login: Boolean(account.persist_login),
                });
            }
        }
        const desired = state.targetSaveDesired || state.draft.targets || [];
        const same = desired.length === targets.length && desired.every((target, index) =>
            target.platform === targets[index].platform
            && target.account_id === targets[index].account_id
            && target.mode === targets[index].mode);
        if (!same) saveTargets(targets);
    }

    function saveTargets(targets) {
        state.targetSaveDesired = cloneValue(targets);
        state.targetSavePending = cloneValue(targets);
        if (!state.targetSavePromise) {
            state.targetSavePromise = drainTargetSaveQueue().finally(() => {
                state.targetSavePromise = null;
            });
        }
        return state.targetSavePromise;
    }

    async function drainTargetSaveQueue() {
        let saved = true;
        while (state.targetSavePending !== null) {
            const targets = state.targetSavePending;
            state.targetSavePending = null;
            saved = await persistTargets(targets);
            if (!saved) {
                state.targetSavePending = null;
                const serverTargets = state.conflictServerDraft?.targets
                    || state.draft?.targets
                    || [];
                state.targetSaveDesired = cloneValue(serverTargets);
                restoreSwitcherSelection(serverTargets);
                renderTargetSwitcher();
                setMessage(
                    'target-builder-error',
                    '投递目标未保存，已恢复到最后一次服务端状态；请处理提示后重试。',
                );
                break;
            }
        }
        return saved;
    }

    async function flushTargetSaveQueue() {
        if (state.targetSavePending !== null && !state.targetSavePromise) {
            saveTargets(state.targetSavePending);
        }
        return state.targetSavePromise ? state.targetSavePromise : true;
    }

    async function persistTargets(targets) {
        const initialDraftId = state.draft?.draft_id;
        if (!(await saveDraftNow()) || state.conflictServerDraft) return false;
        if (initialDraftId && state.draft?.draft_id !== initialDraftId) return true;
        if (!state.draft?.draft_id) {
            const expectedDraft = state.draft;
            const created = await enqueueDraftMutation(() => {
                if (state.draft !== expectedDraft) return false;
                return createPersistedDraft();
            });
            if (!created || state.conflictServerDraft) return false;
        }
        const requestDraftId = state.draft.draft_id;
        const requestedTargets = cloneValue(targets);
        return enqueueDraftMutation(async () => {
            if (state.draft?.draft_id !== requestDraftId) return true;
            const requestRevision = state.draft.revision;
            state.targetSaving = true;
            try {
                const response = await fetch(endpoint(
                    root.dataset.targetsUrlTemplate,
                    'draft_id',
                    requestDraftId,
                ), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                    body: JSON.stringify({
                        revision: requestRevision,
                        targets: requestedTargets.map(target => ({
                            target_id: target.target_id || undefined,
                            platform: target.platform,
                            account_id: target.account_id,
                            mode: target.mode,
                            persist_login: target.persist_login,
                        })),
                    }),
                });
                const payload = await response.json().catch(() => ({}));
                if (state.draft?.draft_id !== requestDraftId) return true;
                if (response.status === 409 && payload.error === 'DRAFT_REVISION_CONFLICT') {
                    state.conflictServerDraft = payload.server_draft;
                    byId('conflict-local-revision').textContent = String(requestRevision);
                    byId('conflict-server-revision').textContent = String(
                        payload.server_draft?.revision ?? '—',
                    );
                    coreModal('conflict-modal').show();
                    return false;
                }
                if (!response.ok) throw new Error(payload.message || '投递目标保存失败');
                state.draft.targets = payload.targets || [];
                state.draft.revision = payload.revision;
                state.draft.updated_at = payload.updated_at;
                invalidatePlan();
                renderTargets();
                updateDraftMeta();
                await localDraftPut(state.dirty);
                setSaveState(
                    state.dirty ? 'local' : 'synced',
                    state.dirty ? '本地已保存' : '已同步',
                );
                byId('targets-empty').classList.toggle(
                    'd-none',
                    state.draft.targets.length > 0,
                );
                return true;
            } catch (error) {
                if (state.draft?.draft_id !== requestDraftId) return true;
                setMessage('target-builder-error', error.message || '投递目标保存失败');
                return false;
            } finally {
                state.targetSaving = false;
                if (state.draft?.draft_id === requestDraftId
                    && state.dirty
                    && !state.conflictServerDraft) {
                    clearTimeout(state.saveTimer);
                    state.saveTimer = setTimeout(() => saveDraftNow(), 50);
                }
            }
        });
    }

    function updateCreatePlanButton() {
        const button = byId('create-plan');
        if (!button || state.planBusy) return;
        const targets = state.draft?.targets || [];
        const drafts = targets.filter(target => target.mode === 'DRAFT').length;
        const publishes = targets.filter(target => target.mode === 'PUBLISH').length;
        if (publishes > 0 && drafts > 0) {
            button.textContent = `保存到 ${drafts} 个草稿箱，并确认 ${publishes} 个公开发布目标`;
        } else if (publishes > 0) {
            button.textContent = `继续确认 ${publishes} 个公开发布目标`;
        } else if (drafts > 0) {
            button.textContent = `保存到 ${drafts} 个草稿箱`;
        } else {
            button.textContent = '选择投递目标后继续';
        }
    }

    function renderTargets() {
        const container = byId('targets-list');
        container.replaceChildren(...state.draft.targets.map(target => {
            const row = document.createElement('article'); row.className = 'target-row';
            const platform = document.createElement('div'); const platformStrong = document.createElement('strong'); platformStrong.textContent = platformLabel(target.platform); const platformSmall = document.createElement('small'); platformSmall.textContent = '平台'; platform.append(platformStrong, platformSmall);
            const account = document.createElement('div'); const accountStrong = document.createElement('strong'); accountStrong.textContent = target.account_display_name || '平台账号'; const accountSmall = document.createElement('small'); accountSmall.textContent = target.account_id ? `账号 ${target.account_id.slice(0, 8)}…` : '账号'; account.append(accountStrong, accountSmall);
            const mode = document.createElement('div'); const modeBadge = document.createElement('span'); modeBadge.className = `badge ${target.mode === 'PUBLISH' ? 'text-bg-danger' : 'text-bg-info'}`; modeBadge.textContent = target.mode === 'PUBLISH' ? '公开发布' : '平台草稿'; mode.appendChild(modeBadge);
            const policy = document.createElement('div'); const policyStrong = document.createElement('strong'); policyStrong.textContent = target.persist_login ? '保持登录态' : '不持久会话'; const policySmall = document.createElement('small'); policySmall.textContent = '会话策略'; policy.append(policyStrong, policySmall);
            const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-outline-danger btn-sm'; remove.textContent = '移除'; remove.addEventListener('click', () => {
                // 从滑块状态移除该账号勾选，重建 targets（与滑块同源）
                const selected = state.switcherSelected[target.platform] || (state.switcherSelected[target.platform] = []);
                const index = selected.indexOf(target.account_id);
                if (index >= 0) selected.splice(index, 1);
                reRenderSwitcherRow(target.platform);
                rebuildTargets();
            });
            row.append(platform, account, mode, policy, remove); return row;
        }));
        byId('targets-empty').classList.toggle('d-none', state.draft.targets.length > 0);
        byId('target-count').textContent = `${state.draft.targets.length} 个目标`;
        updateCreatePlanButton();
        updateDraftMeta();
    }

    function validateStudio() {
        const issues = [];
        if (!state.draft.title.trim()) issues.push({ message: '填写文章标题', focus: 'draft-title', step: 'content-section' });
        if (publicBlocks().length === 0) issues.push({ message: '正文还不能为空，输入文字或插入图片', focus: 'rich-editor', step: 'content-section' });
        if (state.draft.targets.length === 0) issues.push({ message: '至少选择一个投递目标', focus: 'target-switcher-list', step: 'targets-section' });
        return issues;
    }

    function isFormatBlockedTarget(target) {
        return target?.status === 'FORMAT_REVIEW_REQUIRED'
            || Object.prototype.hasOwnProperty.call(formatErrorLabels, target?.error_code);
    }

    function isExecutableTarget(target) {
        return Boolean(target) && !isFormatBlockedTarget(target);
    }

    function executablePlanTargets(plan = state.plan) {
        return (plan?.targets || []).filter(isExecutableTarget);
    }

    function formatTargetReason(target) {
        const label = formatErrorLabels[target?.error_code] || '平台格式能力待复核';
        const features = Array.isArray(target?.required_features) && target.required_features.length
            ? `需要能力：${target.required_features.join('、')}` : '';
        const detail = typeof target?.error_message === 'string' && target.error_message
            ? localizedErrorMessage(target?.error_code, target.error_message, '') : '';
        return `${label}，此目标暂不执行。${features}${detail ? `（${detail}）` : ''}`;
    }

    function showValidation(issues) {
        const container = byId('validation-summary'); const list = byId('validation-list');
        list.replaceChildren(...issues.map(issue => { const item = document.createElement('li'); const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-link btn-sm p-0 align-baseline'; button.textContent = issue.message; button.addEventListener('click', () => focusValidationIssue(issue)); item.appendChild(button); return item; }));
        container.classList.remove('d-none'); container.focus();
        focusValidationIssue(issues[0]);
    }

    async function createPlan() {
        if (!(await flushTargetSaveQueue()) || state.conflictServerDraft) return;
        const issues = validateStudio();
        if (issues.length) { showValidation(issues); return; }
        byId('validation-summary').classList.add('d-none'); setMessage('execution-error', '');
        if (!(await saveDraftNow()) || state.conflictServerDraft) return;
        const createButton = byId('create-plan');
        state.planBusy = true;
        createButton.setAttribute('aria-busy', 'true');
        createButton.disabled = true;
        createButton.textContent = '正在生成投递计划…';
        let executeImmediately = false;
        try {
            const response = await fetch(endpoint(root.dataset.planUrlTemplate, 'draft_id', state.draft.draft_id), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ revision: state.draft.revision }) });
            state.plan = await jsonResponse(response);
            renderPlan();
            if (state.plan.targets.some(isFormatBlockedTarget)) {
                renderPlanReview();
                coreModal('plan-review-modal').show();
            } else {
                executeImmediately = true;
            }
        } catch (error) { setMessage('execution-error', error.message || '投递计划生成失败'); }
        finally {
            state.planBusy = false;
            createButton.removeAttribute('aria-busy');
            createButton.disabled = false;
            updateCreatePlanButton();
        }
        if (executeImmediately) {
            const executable = executablePlanTargets();
            await executePlan({
                target_ids: executable.map(target => target.target_id),
                draft_batch_confirmed: true,
                confirmations: {},
            }, { messageTarget: 'execution-error' });
        }
    }

    function renderPlanReview() {
        const summary = byId('plan-review-summary');
        summary.replaceChildren(...state.plan.targets.map(target => {
            const blocked = isFormatBlockedTarget(target);
            const card = document.createElement('article'); card.className = `plan-review-card${blocked ? ' is-format-review' : ''}`;
            const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`;
            const detail = document.createElement('div'); detail.className = 'small text-body-secondary'; detail.textContent = blocked
                ? formatTargetReason(target)
                : target.mode === 'PUBLISH' ? '公开发布（还需逐条二次确认）' : '保存到平台草稿箱';
            card.append(strong, detail); return card;
        }));
        const blockedTargets = state.plan.targets.filter(isFormatBlockedTarget);
        const executableTargets = executablePlanTargets();
        const executeButton = byId('execute-plan');
        executeButton.disabled = blockedTargets.length > 0 || executableTargets.length === 0;
        executeButton.setAttribute('aria-disabled', String(executeButton.disabled));
        executeButton.textContent = blockedTargets.length > 0
            ? '当前计划不可执行' : '创建执行单';
        setMessage('plan-format-warning', blockedTargets.length
            ? blockedTargets.length === state.plan.targets.length
                ? '当前计划的全部目标均待格式复核，已阻止执行；请先调整内容或完成对应平台格式能力验收。'
                : `${blockedTargets.length} 个目标待格式复核；为避免部分误投，本计划不会创建任何执行单。`
            : '');
        setMessage('plan-review-error', '');
    }

    function planBadge(status, target = null) {
        if (isFormatBlockedTarget(target)) return 'text-bg-warning';
        if (target?.degraded && target.status === 'DRAFT_SAVED') return 'text-bg-warning';
        if (['SUCCESS', 'DRAFT_SAVED', 'PUBLISHED'].includes(status)) return 'text-bg-success';
        if (['BLOCKED', 'FAILED', 'FATAL'].includes(status)) return 'text-bg-danger';
        if (['PARTIAL_FAIL', 'CONFIRMATION_REQUIRED', 'RESULT_UNKNOWN', 'DELIVERY_INCOMPLETE', 'FORMAT_REVIEW_REQUIRED', 'DRAFT_SAVED_WITH_WARNINGS', 'PUBLISHED_WITH_WARNINGS'].includes(status)) return 'text-bg-warning';
        return 'text-bg-info';
    }

    function targetNeedsRelogin(target) {
        return reLoginErrorCodes.has(String(target?.error_code || '').toUpperCase());
    }

    function targetStatusLabel(target) {
        if (targetNeedsRelogin(target)) return '需要重新登录';
        if (target?.error_code === 'XHS_CLOUD_DRAFT_UNAVAILABLE') return '网页端无云端草稿';
        if (target.status === 'DRAFT_SAVED_WITH_WARNINGS') return '草稿已保存（需核对）';
        if (target?.degraded && target.status === 'DRAFT_SAVED') return '草稿已保存（完整性待核对）';
        return localizedStatus(target.status);
    }

    function platformDraftBoxUrl(platform) {
        // 各平台草稿箱直达地址（2026-08 盘点确认）
        return {
            xiaoheihe: 'https://www.xiaoheihe.cn/app/bbs/draft',
            zhihu: 'https://www.zhihu.com/creator/manage/creation/draft?type=article',
            weibo: 'https://card.weibo.com/article/v5/editor#/draft',
            smzdm: 'https://post.smzdm.com/tougao/',
            baijiahao: 'https://baijiahao.baidu.com/builder/rc/content',
            zol: 'https://post.zol.com.cn/v2/manage/works/draft',
        }[platform] || '';
    }

    function planTargetDetail(target) {
        if (isFormatBlockedTarget(target)) return formatTargetReason(target);
        if (targetNeedsRelogin(target)) {
            return '该账号登录态已失效，请到账号管理页重新登录后再创建新计划。';
        }
        if (target.error_code === 'XHS_CLOUD_DRAFT_UNAVAILABLE') {
            return '小红书网页长文只保存在隔离浏览器本地，不能作为账号云端草稿；该目标未执行。';
        }
        if (target.error_code === 'DRAFT_BASELINE_UNAVAILABLE') {
            return localizedErrorMessage(
                target.error_code,
                target.error_message,
                '保存前无法建立可靠草稿基线；平台写入已停止。',
            );
        }
        const warningStatus = ['DRAFT_SAVED_WITH_WARNINGS', 'PUBLISHED_WITH_WARNINGS'].includes(target.status);
        if (warningStatus && target.error_message) {
            return localizedErrorMessage(target.error_code, target.error_message, '草稿已保存，但内容需要核对。');
        }
        if (target?.degraded && ['DRAFT_SAVED', 'DRAFT_SAVED_WITH_WARNINGS'].includes(target.status)) {
            return localizedErrorMessage(
                target.error_code,
                target.error_message,
                '平台草稿箱已出现本次草稿，保存结果待核对正文和图片完整性。',
            );
        }
        if (target.status === 'DELIVERY_INCOMPLETE' && target.error_message) {
            return localizedErrorMessage(target.error_code, target.error_message, '投递未完成，请人工核对草稿箱。');
        }
        // 草稿保存证据链：即使整体失败/未知，也展示平台侧可核验证据
        if (target.verification_evidence) {
            const ev = target.verification_evidence;
            if (ev.draft_list_title_unique === true) {
                return '平台草稿箱已存在标题唯一匹配的草稿。' + (ev.summary ? `（${ev.summary}）` : '');
            }
            if (ev.summary) return ev.summary;
        }
        if (target.status === 'RESULT_UNKNOWN') {
            return '结果未知，请先到平台人工核对；系统不会自动重试。';
        }
        if (target.status === 'DELIVERY_INCOMPLETE') {
            return localizedErrorMessage(
                target.error_code,
                target.error_message,
                '投递未完成：平台侧未确认草稿保存结果，请使用只读核验或到平台草稿箱人工核对。',
            );
        }
        if (target.status === 'PARTIAL_FAIL') {
            return localizedErrorMessage(target.error_code, target.error_message, '部分内容未完整处理，请核对图片和平台结果。');
        }
        if (target.status === 'CREATING') {
            return '正在创建独立执行单，请稍候。';
        }
        return target.error_message
            ? localizedErrorMessage(target.error_code, target.error_message)
            : (target.operation_id
                ? `执行单 ${target.operation_id}`
                : target.mode === 'PUBLISH' ? '公开发布' : '平台草稿');
    }

    function planDisplayStatus(plan) {
        const targets = Array.isArray(plan?.targets) ? plan.targets : [];
        if (plan?.status === 'FATAL' && targets.length > 0
            && targets.every(target => target?.status === 'DELIVERY_INCOMPLETE')) {
            return 'DELIVERY_INCOMPLETE';
        }
        return plan?.status || '';
    }

    const verifyStatuses = ['FAILED', 'RESULT_UNKNOWN', 'DELIVERY_INCOMPLETE', 'DRAFT_SAVED_WITH_WARNINGS'];
    const verificationControls = new WeakMap();

    function canVerifyDraft(target) {
        return Boolean(target?.operation_id)
            && (verifyStatuses.includes(target.status)
                || (target.degraded && target.status === 'DRAFT_SAVED'));
    }

    function hasInFlightTarget(plan) {
        return plan.targets.some(target => ['CREATING', 'QUEUED', 'RUNNING'].includes(target.status));
    }

    function renderPlan() {
        if (!state.plan) return;
        byId('plan-result').classList.remove('d-none');
        byId('plan-id').textContent = `投递计划 ${state.plan.plan_id.slice(0, 8)}…`;
        const displayStatus = planDisplayStatus(state.plan);
        byId('plan-status').className = `badge ${planBadge(displayStatus)}`;
        byId('plan-status').textContent = localizedStatus(displayStatus);
        byId('plan-targets').replaceChildren(...state.plan.targets.map(target => {
            const row = document.createElement('article'); row.className = `plan-target${isFormatBlockedTarget(target) ? ' is-format-review' : ''}`;
            const copy = document.createElement('div'); copy.className = 'plan-target-copy'; const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`; const small = document.createElement('small'); small.textContent = planTargetDetail(target); copy.append(strong, small);
            const actions = document.createElement('div'); actions.className = 'plan-target-actions'; const badge = document.createElement('span'); badge.className = `badge ${planBadge(target.status, target)}`; badge.textContent = targetStatusLabel(target); actions.appendChild(badge);
            if (['DRAFT_SAVED', 'DRAFT_SAVED_WITH_WARNINGS'].includes(target.status)) {
                const draftBoxUrl = platformDraftBoxUrl(target.platform);
                if (draftBoxUrl) {
                    const link = document.createElement('a');
                    link.className = 'btn btn-sm btn-outline-primary';
                    link.href = draftBoxUrl;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.textContent = '查看平台草稿箱';
                    link.setAttribute('aria-label', `在新标签页打开${platformLabel(target.platform)}草稿箱`);
                    actions.appendChild(link);
                }
            }
            // 只读核验草稿按钮：结果需核对时允许人工触发；不自动验证或重试。
            if (canVerifyDraft(target)) {
                const verifyResult = document.createElement('div');
                verifyResult.className = 'plan-target-verify-result alert alert-info mt-2 d-none';
                verifyResult.setAttribute('role', 'status');
                verifyResult.setAttribute('aria-live', 'polite');
                verifyResult.setAttribute('aria-atomic', 'true');
                const verify = document.createElement('button');
                verify.className = 'btn btn-sm btn-outline-secondary';
                verify.textContent = '核验平台草稿';
                verify.setAttribute('type', 'button');
                verify.dataset.draftVerifyButton = 'true';
                verificationControls.set(target, { button: verify, result: verifyResult });
                verify.addEventListener('click', () => verifyDraft(target));
                actions.appendChild(verify);
                copy.appendChild(verifyResult);
            }
            row.append(copy, actions); return row;
        }));
        updateStudioProgress();
    }

    async function verifyDraft(target) {
        const opId = target.operation_id;
        if (!opId) return;
        const controls = verificationControls.get(target) || {};
        const btn = controls.button || null;
        const resultEl = controls.result || null;
        if (state.verifyingOperations.has(opId)) return;
        state.verifyingOperations.add(opId);
        const setResult = (text, kind = 'info') => {
            if (!resultEl) return;
            resultEl.textContent = text || '';
            resultEl.className = `plan-target-verify-result alert alert-${kind} mt-2${text ? '' : ' d-none'}`;
        };
        if (btn) {
            btn.disabled = true;
            btn.textContent = '核验中…';
            btn.setAttribute('aria-busy', 'true');
        }
        try {
            const response = await fetch(`/api/delivery-operations/${encodeURIComponent(opId)}/verify-draft`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                const code = payload.error || 'PROBE_FAILED';
                const hint = code === 'PROBE_UNSUPPORTED_PLATFORM'
                    ? '该平台尚未实现只读核验，请打开平台草稿箱人工核对。'
                    : localizedErrorMessage(
                        code,
                        payload.message,
                        '核验失败，请到平台草稿箱人工核对。',
                    );
                setResult(hint, 'warning');
                return;
            }
            if (payload.title_matched) {
                if (payload.status_updated) {
                    const unattributed = payload.operation?.error_code === 'DRAFT_ENTITY_UNATTRIBUTED';
                    setResult(
                        unattributed
                            ? '平台存在唯一同标题草稿；已改为“草稿已保存（需核对）”，但未确认由本次执行新建，系统不会重新投递。'
                            : '已确认本次云端草稿实体；执行状态已改为“草稿已保存（需核对）”，系统不会重新投递。',
                        unattributed ? 'warning' : 'success',
                    );
                    if (state.plan?.plan_id) {
                        try {
                            state.plan = await jsonResponse(await fetch(
                                endpoint(root.dataset.planDetailUrlTemplate, 'plan_id', state.plan.plan_id),
                                { headers: { Accept: 'application/json' } },
                            ));
                            renderPlan();
                        } catch (error) {
                            setResult('云端草稿已确认且执行单已更新；计划刷新失败，请刷新页面查看最新状态。', 'warning');
                        }
                    }
                    return;
                }
                setResult(`只读核验：平台草稿箱存在标题唯一匹配的草稿${payload.draft_url ? '。可在平台直接核对' : ''}；未绑定到本次执行单时不会改写状态。`, 'success');
            } else if (payload.error_code === 'PROBE_NOT_FOUND') {
                setResult('只读核验：平台草稿箱未找到该标题草稿。', 'danger');
            } else if (payload.error_code === 'PROBE_TITLE_AMBIGUOUS') {
                setResult(localizedErrorMessage(payload.error_code, payload.error_message, '草稿箱存在多个同名草稿，需人工区分。'), 'warning');
            } else {
                setResult(localizedErrorMessage(payload.error_code, payload.error_message, '只读核验完成，请查看平台草稿箱。'), 'info');
            }
        } catch (err) {
            setResult('只读核验请求失败，请稍后重试；系统不会自动重试平台操作。', 'danger');
        } finally {
            state.verifyingOperations.delete(opId);
            if (btn) {
                btn.disabled = false;
                btn.textContent = '核验平台草稿';
                btn.setAttribute('aria-busy', 'false');
            }
        }
    }

    async function executePlan(
        payload,
        { fromReview = false, messageTarget = null, manageFollowup = true } = {},
    ) {
        if (!state.plan || state.planBusy) return;
        const messageId = messageTarget
            || (fromReview ? 'plan-review-error' : 'publish-confirm-error');
        if (state.plan.targets.some(isFormatBlockedTarget)) {
            setMessage(messageId, '当前计划存在待格式复核目标，系统不会调用执行接口。');
            return false;
        }
        const executable = executablePlanTargets();
        if (!executable.length) {
            setMessage(messageId, '当前没有可执行目标：所有目标均待格式复核，系统不会调用执行接口。');
            return false;
        }
        const executableIds = new Set(executable.map(target => target.target_id));
        const requestedIds = Array.isArray(payload.target_ids) ? payload.target_ids : [...executableIds];
        const safeTargetIds = requestedIds.filter(targetId => executableIds.has(targetId));
        if (!safeTargetIds.length) {
            setMessage(messageId, '所选目标当前不可执行，系统不会调用执行接口。');
            return false;
        }
        const safePayload = { ...payload, target_ids: safeTargetIds };
        state.planBusy = true; setMessage(messageId, '');
        try {
            const response = await fetch(endpoint(root.dataset.planExecuteUrlTemplate, 'plan_id', state.plan.plan_id), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(safePayload) });
            const result = await response.json().catch(() => ({}));
            if (!response.ok && response.status !== 428) throw new Error(result.message || '投递计划执行失败');
            state.plan = result; state.pollCount = 0; renderPlan();
            if (fromReview) coreModal('plan-review-modal').hide();
            if (manageFollowup) {
                const confirmations = result.targets.filter(target =>
                    target.confirmation_required && target.confirmation_token);
                if (confirmations.length) {
                    state.pendingPublishTargets = confirmations.slice();
                    showNextPublishConfirmation();
                } else if (hasInFlightTarget(result)) {
                    schedulePlanPoll();
                }
            }
        } catch (error) { setMessage(messageId, error.message || '投递计划执行失败'); return false; }
        finally { state.planBusy = false; }
        return true;
    }

    function showNextPublishConfirmation() {
        state.activePublishTarget = state.pendingPublishTargets.shift() || null;
        if (!state.activePublishTarget) { schedulePlanPoll(); return; }
        const target = state.activePublishTarget;
        const summary = byId('publish-target-summary'); summary.replaceChildren();
        const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`;
        const detail = document.createElement('div'); detail.className = 'small mt-1'; detail.textContent = `标题：${state.draft.title}`;
        summary.append(strong, detail); byId('publish-token-expiry').textContent = target.expires_at ? `一次性确认令牌有效至 ${formatDate(target.expires_at)}` : '该确认令牌只能使用一次。';
        setMessage('publish-confirm-error', ''); coreModal('publish-confirm-modal').show();
    }

    async function confirmPublishTarget() {
        const target = state.activePublishTarget;
        if (!target) return;
        const accepted = await executePlan({
            target_ids: [target.target_id],
            draft_batch_confirmed: true,
            confirmations: { [target.target_id]: target.confirmation_token },
        }, { manageFollowup: false });
        if (!accepted) return;
        coreModal('publish-confirm-modal').hide();
        state.pendingPublishTargets = state.plan.targets.filter(targetItem =>
            targetItem.confirmation_required && targetItem.confirmation_token);
        if (state.pendingPublishTargets.length) {
            setTimeout(showNextPublishConfirmation, 250);
        } else if (hasInFlightTarget(state.plan)) {
            schedulePlanPoll();
        }
    }

    function schedulePlanPoll() {
        clearTimeout(state.pollTimer);
        if (!state.plan) return;
        if (state.pollCount >= MAX_PLAN_POLLS) {
            setMessage('execution-error', '仍有目标未进入终态，请稍后手动刷新计划；系统不会自动重试平台操作。');
            return;
        }
        state.pollTimer = setTimeout(async () => {
            state.pollCount += 1;
            try {
                state.plan = await jsonResponse(await fetch(endpoint(root.dataset.planDetailUrlTemplate, 'plan_id', state.plan.plan_id), { headers: { Accept: 'application/json' } })); renderPlan();
                if (hasInFlightTarget(state.plan)) schedulePlanPoll();
            } catch (error) { setMessage('execution-error', error.message || '无法刷新执行状态'); }
        }, 2500);
    }

    const draftStatusLabels = {
        ACTIVE: '编辑中', UNSAVED: '未保存', ARCHIVED: '已归档',
    };

    function renderDraftLibrary() {
        const list = byId('draft-library-list');
        if (!list) return;
        list.replaceChildren(...state.drafts.map(draft => {
            const item = document.createElement('article');
            item.className = 'library-item';
            const copy = document.createElement('div');
            copy.className = 'library-item-copy';
            const title = document.createElement('strong');
            title.textContent = draft.title || '未命名草稿';
            const meta = document.createElement('small');
            meta.textContent = draftSourceSummary(draft);
            const dates = document.createElement('small');
            dates.textContent = `创建：${formatDate(draft.created_at)} · 更新：${formatDate(draft.updated_at)}`;
            const status = document.createElement('small');
            status.textContent = `状态：${draftStatusLabels[draft.status] || sharedUiText.statusLabel?.(draft.status) || '状态未知'}`;
            copy.append(title, meta, dates, status);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-outline-primary btn-sm';
            button.textContent = draft.draft_id === state.draft?.draft_id ? '当前草稿' : '继续编辑';
            button.disabled = draft.draft_id === state.draft?.draft_id;
            button.addEventListener('click', async () => {
                if (!(await saveDraftNow())) return;
                button.disabled = true;
                try {
                    const full = await jsonResponse(await fetch(
                        endpoint(root.dataset.draftUrlTemplate, 'draft_id', draft.draft_id),
                        { headers: { Accept: 'application/json' } },
                    ));
                    await openDraft(full, { historyMode: 'push' });
                    coreModal('source-library-modal').hide();
                } catch (error) {
                    setMessage('draft-library-error', error.message || '历史草稿读取失败');
                    button.disabled = false;
                }
            });
            item.append(copy, button);
            return item;
        }));
        byId('draft-library-empty')?.classList.toggle('d-none', state.drafts.length > 0);
    }

    async function loadDraftLibrary() {
        if (state.draftLibraryLoading) return;
        state.draftLibraryLoading = true;
        const loading = byId('draft-library-loading');
        loading?.classList.remove('d-none');
        setMessage('draft-library-error', '');
        try {
            const payload = await jsonResponse(await fetch(`${root.dataset.draftsUrl}?limit=50&offset=0`, {
                headers: { Accept: 'application/json' },
            }));
            state.drafts = Array.isArray(payload.drafts) ? payload.drafts : [];
            renderDraftLibrary();
        } catch (error) {
            setMessage('draft-library-error', error.message || '历史草稿读取失败');
        } finally {
            state.draftLibraryLoading = false;
            loading?.classList.add('d-none');
        }
    }

    async function createBlankDraft() {
        try { if (!(await saveDraftNow())) return; const draft = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title: '', blocks: [], cover: { strategy: 'NONE', asset_id: null } }) })); await openDraft(draft, { historyMode: 'replace' }); coreModal('source-library-modal').hide(); requestAnimationFrame(() => byId('draft-title').focus()); }
        catch (error) { setMessage('studio-fatal', error.message || '无法新建草稿'); }
    }

    async function importDocx(file) {
        if (!file) return;
        setSaveState('saving', '正在导入 DOCX');
        const form = new FormData(); form.append('file', file);
        try { if (!(await saveDraftNow())) return; const draft = await jsonResponse(await fetch(root.dataset.docxUrl, { method: 'POST', body: form, headers: { Accept: 'application/json' } })); await openDraft(draft, { historyMode: 'replace' }); coreModal('source-library-modal').hide(); }
        catch (error) { setMessage('studio-fatal', error.message || 'DOCX 导入失败'); setSaveState('error', 'DOCX 导入失败'); }
    }

    async function resolveConflict(useServer) {
        if (useServer) {
            const server = state.conflictServerDraft;
            if (!server) {
                setMessage('content-error', '服务端版本不可用，请刷新草稿后重试。');
                return;
            }
            if (isV2Draft() && Number(server.content_schema_version) !== 2) {
                setMessage('content-error', '服务端返回的不是同一份 v2 富文档，已拒绝降级覆盖；请刷新后重试。');
                return;
            }
            applyDraft(server);
            await localDraftPut(false);
            setSaveState('synced', '已采用服务端版本');
            coreModal('conflict-modal').hide();
            return;
        }
        const local = isV2Draft()
            ? {
                title: state.draft.title,
                content_schema_version: 2,
                document: cloneValue(state.draft.document),
                cover: coverRequest(state.draft.cover),
            }
            : { title: state.draft.title, blocks: publicBlocks(), cover: coverRequest(state.draft.cover) };
        try { const copy = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(local) })); state.conflictServerDraft = null; await openDraft(copy, { historyMode: 'replace' }); setSaveState('synced', '本地内容已另存副本'); coreModal('conflict-modal').hide(); }
        catch (error) { setMessage('content-error', error.message || '另存草稿副本失败'); }
    }

    async function openDraftFromLocation() {
        const sequence = ++state.navigationSequence;
        const requestedId = new URLSearchParams(window.location.search).get('draft_id')?.trim() || '';
        if (requestedId) {
            const draft = await jsonResponse(await fetch(
                endpoint(root.dataset.draftUrlTemplate, 'draft_id', requestedId),
                { headers: { Accept: 'application/json' } },
            ));
            if (sequence !== state.navigationSequence) return;
            await openDraft(draft, { historyMode: 'replace' });
            return;
        }
        clearStudioMessages();
        if (sequence !== state.navigationSequence) return;
        await openDraft(blankDraft(), { historyMode: 'replace' });
    }

    function bindEvents() {
        window.addEventListener('hashchange', syncStudioStepFromLocation);
        window.addEventListener('focus', () => {
            refreshEnabledSwitcherAccounts().catch(error => {
                setMessage('target-builder-error', `刷新账号状态失败：${error.message || '未知错误'}`);
            });
        });
        window.addEventListener('pageshow', event => {
            if (!event.persisted) return;
            refreshEnabledSwitcherAccounts({ force: true }).catch(error => {
                setMessage('target-builder-error', `刷新账号状态失败：${error.message || '未知错误'}`);
            });
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) return;
            refreshEnabledSwitcherAccounts().catch(error => {
                setMessage('target-builder-error', `刷新账号状态失败：${error.message || '未知错误'}`);
            });
        });
        window.addEventListener('popstate', () => {
            openDraftFromLocation().catch(error => {
                setMessage('studio-fatal', `加载工作台失败：${error.message || '未知错误'}`);
            });
        });
        byId('draft-title').addEventListener('input', event => {
            state.draft.title = event.target.value;
            if (isV2Draft() && state.draft.document && typeof state.draft.document === 'object') {
                state.draft.document.title = event.target.value;
            }
            byId('side-draft-title').textContent = event.target.value || '未命名草稿';
            markDirty();
        });
        // 知乎式连续编辑区：输入防抖同步；粘贴图片直接上传插入光标处，文字仅保留纯文本
        byId('rich-editor').addEventListener('input', handleEditorInput);
        byId('rich-editor').addEventListener('paste', event => {
            if (isV2Draft()) {
                event.preventDefault();
                setMessage('content-error', v2ReadonlyMessage());
                return;
            }
            event.preventDefault();
            const clipboard = event.clipboardData || window.clipboardData;
            const pastedImages = Array.from(clipboard.items || [])
                .filter(item => item.type && item.type.startsWith('image/'))
                .map(item => item.getAsFile())
                .filter(Boolean);
            if (pastedImages.length) {
                uploadAssets(pastedImages);
                return;
            }
            const text = clipboard.getData('text/plain');
            document.execCommand('insertText', false, text);
        });
        byId('clear-blocks').addEventListener('click', clearAllBlocks);
        byId('asset-upload').addEventListener('change', event => { uploadAssets(Array.from(event.target.files || [])); event.target.value = ''; });
        ['cover-none', 'cover-first-body-image', 'cover-explicit'].forEach(id => {
            byId(id)?.addEventListener('change', event => setCoverStrategy(event.target.value));
        });
        // 拖拽导入：Word(.docx) → 导入并预览；图片 → 插入图片块
        const blocksDrop = byId('content-blocks');
        const dropOverlay = byId('drop-overlay');
        function showDropOverlay(visible) {
            blocksDrop.classList.toggle('is-drop-target', visible);
            dropOverlay?.classList.toggle('d-none', !visible);
        }
        ['dragenter', 'dragover'].forEach(type => blocksDrop.addEventListener(type, event => {
            if (isV2Draft()) {
                showDropOverlay(false);
                return;
            }
            if (Array.from(event.dataTransfer?.types || []).includes('Files')) {
                event.preventDefault();
                showDropOverlay(true);
            }
        }));
        ['dragleave', 'drop'].forEach(type => blocksDrop.addEventListener(type, event => {
            if (type === 'dragleave' && blocksDrop.contains(event.relatedTarget)) return;
            showDropOverlay(false);
        }));
        blocksDrop.addEventListener('drop', event => {
            event.preventDefault();
            if (isV2Draft()) {
                showDropOverlay(false);
                setMessage('content-error', v2ReadonlyMessage());
                return;
            }
            const files = Array.from(event.dataTransfer?.files || []);
            setMessage('content-error', '');
            if (!files.length) {
                // 从应用内（如企业微信客户端/网页）拖入时，浏览器可能拿不到 File 对象，
                // 只有文本/链接；无法自动读取文件内容，给出明确引导。
                const uriList = (event.dataTransfer?.getData('text/uri-list') || '');
                const html = (event.dataTransfer?.getData('text/html') || '');
                const looksLikeDoc = /\.docx?(?:\s|$)/i.test(uriList + ' ' + html);
                setMessage('content-error',
                    looksLikeDoc
                        ? '从应用内拖入的 Word 文档浏览器无法直接读取，请用「更换内容源 → 导入 DOCX」选择文件，或从文件管理器把 .docx 文件拖到此处。'
                        : '浏览器无法读取拖入的内容，请拖入本地文件，或用「更换内容源 → 导入 DOCX」。');
                return;
            }
            const docx = files.find(file =>
                file.name.toLowerCase().endsWith('.docx')
                || (file.type || '').includes('wordprocessingml'));
            if (docx) { importDocx(docx); return; }
            const images = files.filter(file => (file.type || '').startsWith('image/'));
            if (images.length) { uploadAssets(images); return; }
            setMessage('content-error', '支持拖入 Word 文档(.docx) 或图片文件。');
        });
        byId('toggle-all-platforms').addEventListener('click', toggleAllPlatforms);
        byId('toggle-all-accounts').addEventListener('click', toggleAllAccounts);
        byId('create-plan').addEventListener('click', createPlan);
        byId('save-draft-now').addEventListener('click', () => saveDraftNow());
        byId('execute-plan').addEventListener('click', () => {
            const executable = executablePlanTargets();
            if (!executable.length) {
                setMessage('plan-review-error', '当前没有可执行目标：所有目标均待格式复核。');
                return;
            }
            executePlan({
                target_ids: executable.map(target => target.target_id),
                draft_batch_confirmed: true,
                confirmations: {},
            }, { fromReview: true });
        });
        byId('confirm-publish-target').addEventListener('click', confirmPublishTarget);
        byId('new-blank-draft').addEventListener('click', createBlankDraft);
        byId('docx-import').addEventListener('change', event => { importDocx(event.target.files?.[0]); event.target.value = ''; });
        byId('source-library-modal').addEventListener('shown.coreui.modal', loadDraftLibrary);
        byId('conflict-use-server').addEventListener('click', () => resolveConflict(true));
        byId('conflict-save-copy').addEventListener('click', () => resolveConflict(false));
        window.addEventListener('beforeunload', () => { if (state.dirty) localDraftPut(true); });
    }

    
    async function init() {
        try {
            bindEvents();
            state.localDb = await openLocalDb();
            await fetchPlatforms();
            await openDraftFromLocation();
            syncStudioStepFromLocation();
        } catch (error) {
            byId('studio-loading').classList.add('d-none');
            setMessage('studio-fatal', `加载工作台失败：${error.message || '未知错误'}`);
        }
    }


    init();
})();
