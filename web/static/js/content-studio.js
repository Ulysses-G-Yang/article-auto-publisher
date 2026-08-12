(function () {
    'use strict';

    const root = document.getElementById('content-studio');
    if (!root) return;

    const byId = id => document.getElementById(id);
    const platformLabels = { xiaoheihe: '小黑盒', zol: '中关村在线' };
    const sourceLabels = { BLANK: '空白草稿', DOCX: 'DOCX 导入', LEGACY_ARTICLE: '历史文章副本', SYSTEM_SEED: '系统草稿' };
    const planStatusLabels = {
        READY: '待执行', QUEUED: '已排队', RUNNING: '执行中', SUCCESS: '已完成',
        PARTIAL_FAIL: '部分失败', FATAL: '执行失败', CONFIRMATION_REQUIRED: '待公开确认',
        DRAFT_SAVED: '平台草稿已保存', PUBLISHED: '已公开发布', BLOCKED: '已拦截', FAILED: '失败',
    };
    const state = {
        draft: null,
        drafts: [],
        accounts: [],
        selectedPlatform: null,
        selectedAccountId: null,
        accountRequestController: null,
        accountRequestSequence: 0,
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
        draggedBlockId: null,
        localDb: null,
    };

    function endpoint(template, key, value) {
        return template.replace(`{${key}}`, encodeURIComponent(value));
    }

    function uid() {
        return globalThis.crypto?.randomUUID?.() || `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
    }

    function openLocalDb() {
        if (!window.indexedDB) return Promise.resolve(null);
        return new Promise(resolve => {
            const request = indexedDB.open('articleops-content-studio', 1);
            request.onupgradeneeded = () => {
                const database = request.result;
                if (!database.objectStoreNames.contains('drafts')) database.createObjectStore('drafts', { keyPath: 'draft_id' });
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => resolve(null);
        });
    }

    function localDraftGet(draftId) {
        if (!state.localDb) return Promise.resolve(null);
        return new Promise(resolve => {
            const request = state.localDb.transaction('drafts', 'readonly').objectStore('drafts').get(draftId);
            request.onsuccess = () => resolve(request.result || null);
            request.onerror = () => resolve(null);
        });
    }

    function localDraftPut(dirty = state.dirty) {
        if (!state.localDb || !state.draft) return Promise.resolve();
        const snapshot = {
            draft_id: state.draft.draft_id,
            revision: state.draft.revision,
            title: state.draft.title,
            blocks: state.draft.blocks,
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
        byId('plan-result').classList.add('d-none');
    }

    async function markDirty() {
        if (!state.draft) return;
        state.dirty = true;
        invalidatePlan();
        setSaveState('local', '本地已保存');
        await localDraftPut(true);
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
    }

    async function saveDraftNow() {
        clearTimeout(state.saveTimer);
        if (!state.draft || !state.dirty || state.saving || state.conflictServerDraft) return !state.dirty;
        state.saving = true;
        setSaveState('saving', '正在同步');
        const baseRevision = state.draft.revision;
        try {
            const requestTitle = state.draft.title;
            const requestBlocks = publicBlocks();
            const response = await fetch(endpoint(root.dataset.draftUrlTemplate, 'draft_id', state.draft.draft_id), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ revision: baseRevision, title: requestTitle, blocks: requestBlocks }),
            });
            const payload = await response.json().catch(() => ({}));
            if (response.status === 409 && payload.error === 'DRAFT_REVISION_CONFLICT') {
                state.conflictServerDraft = payload.server_draft;
                byId('conflict-local-revision').textContent = String(baseRevision);
                byId('conflict-server-revision').textContent = String(payload.server_draft?.revision ?? '—');
                setSaveState('error', '同步冲突');
                coreModal('conflict-modal').show();
                return false;
            }
            if (!response.ok) throw new Error(payload.message || '草稿同步失败');
            state.draft.revision = payload.revision;
            state.draft.updated_at = payload.updated_at;
            state.draft.source_type = payload.source_type;
            const changedDuringRequest = state.draft.title !== requestTitle
                || JSON.stringify(publicBlocks()) !== JSON.stringify(requestBlocks);
            state.dirty = changedDuringRequest;
            await localDraftPut(changedDuringRequest);
            updateDraftMeta();
            setSaveState(changedDuringRequest ? 'local' : 'synced', changedDuringRequest ? '本地已保存' : '已同步');
            if (changedDuringRequest) {
                clearTimeout(state.saveTimer);
                state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
            }
            return !changedDuringRequest;
        } catch (error) {
            setSaveState('error', '同步失败');
            setMessage('content-error', `${error.message}；本地恢复副本已保留。`);
            return false;
        } finally {
            state.saving = false;
        }
    }

    function applyDraft(payload, { render = true } = {}) {
        state.draft = {
            ...payload,
            title: payload.title || '',
            blocks: Array.isArray(payload.blocks) ? payload.blocks.map((block, position) => ({ ...block, position })) : [],
            targets: Array.isArray(payload.targets) ? payload.targets : [],
        };
        state.dirty = false;
        state.conflictServerDraft = null;
        invalidatePlan();
        if (render) {
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            renderTargets();
            updateDraftMeta();
        }
        const url = new URL(window.location.href);
        url.searchParams.set('draft_id', state.draft.draft_id);
        history.replaceState({}, '', url);
    }

    async function openDraft(payload) {
        applyDraft(payload);
        const local = await localDraftGet(payload.draft_id);
        if (local?.dirty && local.revision === payload.revision) {
            state.draft.title = local.title || '';
            state.draft.blocks = Array.isArray(local.blocks) ? local.blocks : [];
            state.dirty = true;
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            updateDraftMeta();
            setSaveState('local', '已恢复本地未同步内容');
            clearTimeout(state.saveTimer);
            state.saveTimer = setTimeout(() => saveDraftNow(), 1000);
        } else {
            await localDraftPut(false);
            setSaveState('synced', '已同步');
        }
        byId('studio-loading').classList.add('d-none');
        byId('studio-workspace').classList.remove('d-none');
    }

    function updateDraftMeta() {
        if (!state.draft) return;
        const source = sourceLabels[state.draft.source_type] || state.draft.source_type || '草稿';
        byId('draft-source-badge').textContent = source;
        byId('draft-revision').textContent = `修订 ${state.draft.revision}`;
        byId('draft-updated-at').textContent = state.draft.updated_at ? `更新于 ${formatDate(state.draft.updated_at)}` : '';
        byId('side-draft-title').textContent = state.draft.title || '未命名草稿';
        byId('side-draft-source').textContent = source;
        byId('side-block-count').textContent = String(state.draft.blocks.length);
        byId('side-target-count').textContent = String(state.draft.targets.length);
        byId('side-revision').textContent = String(state.draft.revision);
    }

    function formatDate(value) {
        if (!value) return '—';
        const date = new Date(value);
        return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
    }

    function iconButton(iconClass, label, action, blockId, disabled = false) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-ghost-secondary btn-sm';
        button.dataset.action = action;
        button.dataset.blockId = blockId;
        button.disabled = disabled;
        button.setAttribute('aria-label', label);
        const icon = document.createElement('i');
        icon.className = iconClass;
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(icon);
        return button;
    }

    function blockElement(block, index) {
        const article = document.createElement('article');
        article.className = 'content-block';
        article.dataset.blockId = block.block_id;
        article.draggable = true;
        article.addEventListener('dragstart', () => { state.draggedBlockId = block.block_id; article.classList.add('is-dragging'); });
        article.addEventListener('dragend', () => { state.draggedBlockId = null; article.classList.remove('is-dragging'); document.querySelectorAll('.drag-over').forEach(item => item.classList.remove('drag-over')); });
        article.addEventListener('dragover', event => { event.preventDefault(); if (state.draggedBlockId !== block.block_id) article.classList.add('drag-over'); });
        article.addEventListener('dragleave', () => article.classList.remove('drag-over'));
        article.addEventListener('drop', event => { event.preventDefault(); reorderByDrop(state.draggedBlockId, block.block_id); });

        const handle = document.createElement('button');
        handle.type = 'button'; handle.className = 'block-handle'; handle.setAttribute('aria-label', '拖动调整图文块顺序');
        const handleIcon = document.createElement('i'); handleIcon.className = 'cil-cursor-move'; handleIcon.setAttribute('aria-hidden', 'true'); handle.appendChild(handleIcon);
        const content = document.createElement('div'); content.className = 'block-content';
        if (block.type === 'image') {
            const preview = document.createElement('div'); preview.className = 'block-image-preview';
            const image = document.createElement('img'); image.src = block.asset_url || `/api/content-assets/${encodeURIComponent(block.asset_id)}`; image.alt = block.alt || '文章图片'; image.loading = 'lazy';
            const meta = document.createElement('div'); meta.className = 'block-image-meta';
            const type = document.createElement('span'); type.textContent = '图片块';
            const alt = document.createElement('input'); alt.type = 'text'; alt.className = 'form-control form-control-sm'; alt.placeholder = '图片替代文本（可选）'; alt.value = block.alt || ''; alt.dataset.action = 'image-alt'; alt.dataset.blockId = block.block_id; alt.setAttribute('aria-label', '图片替代文本');
            meta.append(type, alt); preview.append(image, meta); content.appendChild(preview);
        } else {
            const label = document.createElement('label'); label.className = 'visually-hidden'; label.htmlFor = `block-${block.block_id}`; label.textContent = `正文文字块 ${index + 1}`;
            const textarea = document.createElement('textarea'); textarea.id = `block-${block.block_id}`; textarea.className = 'form-control'; textarea.placeholder = '输入正文内容…'; textarea.value = block.text || ''; textarea.dataset.action = 'text-input'; textarea.dataset.blockId = block.block_id;
            content.append(label, textarea);
        }
        const actions = document.createElement('div'); actions.className = 'block-actions';
        actions.append(
            iconButton('cil-arrow-top', '上移该图文块', 'move-up', block.block_id, index === 0),
            iconButton('cil-arrow-bottom', '下移该图文块', 'move-down', block.block_id, index === state.draft.blocks.length - 1),
            iconButton('cil-trash', '删除该图文块', 'delete-block', block.block_id),
        );
        article.append(handle, content, actions);
        return article;
    }

    function renderBlocks() {
        const container = byId('content-blocks');
        container.replaceChildren(...state.draft.blocks.map(blockElement));
        byId('blocks-empty').classList.toggle('d-none', state.draft.blocks.length > 0);
        byId('block-count').textContent = `${state.draft.blocks.length} 个块`;
        updateDraftMeta();
    }

    function addTextBlock(text = '') {
        state.draft.blocks.push({ block_id: uid(), type: 'text', text, position: state.draft.blocks.length });
        renderBlocks();
        markDirty();
        requestAnimationFrame(() => byId(`block-${state.draft.blocks.at(-1).block_id}`)?.focus());
    }

    function moveBlock(blockId, delta) {
        const index = state.draft.blocks.findIndex(block => block.block_id === blockId);
        const target = index + delta;
        if (index < 0 || target < 0 || target >= state.draft.blocks.length) return;
        [state.draft.blocks[index], state.draft.blocks[target]] = [state.draft.blocks[target], state.draft.blocks[index]];
        state.draft.blocks.forEach((block, position) => { block.position = position; });
        renderBlocks(); markDirty();
    }

    function reorderByDrop(sourceId, targetId) {
        if (!sourceId || sourceId === targetId) return;
        const sourceIndex = state.draft.blocks.findIndex(block => block.block_id === sourceId);
        const targetIndex = state.draft.blocks.findIndex(block => block.block_id === targetId);
        if (sourceIndex < 0 || targetIndex < 0) return;
        const [moved] = state.draft.blocks.splice(sourceIndex, 1);
        state.draft.blocks.splice(targetIndex, 0, moved);
        state.draft.blocks.forEach((block, position) => { block.position = position; });
        renderBlocks(); markDirty();
    }

    async function uploadAssets(files) {
        if (!state.draft || !files.length) return;
        setMessage('content-error', '');
        setSaveState('saving', '正在上传图片');
        try {
            for (const file of files) {
                const form = new FormData(); form.append('file', file);
                const response = await fetch(endpoint(root.dataset.assetsUrlTemplate, 'draft_id', state.draft.draft_id), { method: 'POST', body: form, headers: { Accept: 'application/json' } });
                const asset = await jsonResponse(response);
                state.draft.blocks.push({ block_id: uid(), type: 'image', asset_id: asset.asset_id, asset_url: asset.asset_url, alt: '', position: state.draft.blocks.length });
            }
            renderBlocks(); await markDirty();
        } catch (error) {
            setMessage('content-error', error.message || '图片上传失败');
            setSaveState('error', '图片上传失败');
        }
    }

    async function loadAccounts(platform) {
        const sequence = ++state.accountRequestSequence;
        state.accountRequestController?.abort();
        state.accountRequestController = new AbortController();
        state.accounts = []; state.selectedAccountId = null;
        const select = byId('target-account');
        select.disabled = true; select.replaceChildren(new Option(`正在读取${platformLabels[platform]}账号…`, ''));
        byId('target-persist-login').disabled = true;
        setMessage('target-builder-error', '');
        try {
            const url = endpoint(root.dataset.accountsUrlTemplate, 'platform', platform);
            const payload = await jsonResponse(await fetch(url, { signal: state.accountRequestController.signal, headers: { Accept: 'application/json' } }));
            if (sequence !== state.accountRequestSequence || platform !== state.selectedPlatform) return;
            if (payload.platform && payload.platform !== platform) throw new Error('账号响应与所选平台不匹配');
            state.accounts = (Array.isArray(payload.accounts) ? payload.accounts : []).filter(account => account.session_status === 'VALID');
            select.replaceChildren(new Option(state.accounts.length ? '请选择账号（不会自动选中）' : '当前平台没有可用账号', ''));
            state.accounts.forEach(account => {
                const masked = account.masked_platform_user_id ? ` · ${account.masked_platform_user_id}` : '';
                select.appendChild(new Option(`${account.display_name || '未命名账号'}${masked}`, account.account_id));
            });
            select.disabled = state.accounts.length === 0;
        } catch (error) {
            if (error.name === 'AbortError' || sequence !== state.accountRequestSequence) return;
            select.replaceChildren(new Option('账号加载失败', ''));
            setMessage('target-builder-error', error.message || '账号加载失败');
        }
    }

    function selectedAccount() {
        return state.accounts.find(account => account.account_id === state.selectedAccountId) || null;
    }

    async function updateSessionPolicy(checked) {
        const account = selectedAccount();
        const input = byId('target-persist-login');
        if (!account) return;
        const previous = Boolean(account.persist_login);
        input.disabled = true;
        try {
            const url = endpoint(root.dataset.sessionPolicyUrlTemplate, 'account_id', account.account_id);
            const updated = await jsonResponse(await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ persist_login: checked }) }));
            if (updated.account_id !== account.account_id) throw new Error('会话策略响应与所选账号不匹配');
            Object.assign(account, updated); input.checked = Boolean(account.persist_login);
        } catch (error) {
            input.checked = previous; setMessage('target-builder-error', error.message || '会话策略更新失败');
        } finally { input.disabled = false; }
    }

    async function saveTargets(targets) {
        if (!(await saveDraftNow()) || state.conflictServerDraft) return false;
        try {
            const response = await fetch(endpoint(root.dataset.targetsUrlTemplate, 'draft_id', state.draft.draft_id), {
                method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ revision: state.draft.revision, targets: targets.map(target => ({ target_id: target.target_id || undefined, platform: target.platform, account_id: target.account_id, mode: target.mode, persist_login: target.persist_login })) }),
            });
            const payload = await response.json().catch(() => ({}));
            if (response.status === 409 && payload.error === 'DRAFT_REVISION_CONFLICT') {
                state.conflictServerDraft = payload.server_draft; byId('conflict-local-revision').textContent = String(state.draft.revision); byId('conflict-server-revision').textContent = String(payload.server_draft?.revision ?? '—'); coreModal('conflict-modal').show(); return false;
            }
            if (!response.ok) throw new Error(payload.message || '投递目标保存失败');
            state.draft.targets = payload.targets || [];
            state.draft.revision = payload.revision;
            state.draft.updated_at = payload.updated_at;
            invalidatePlan(); renderTargets(); updateDraftMeta(); await localDraftPut(false); setSaveState('synced', '已同步'); return true;
        } catch (error) { setMessage('target-builder-error', error.message || '投递目标保存失败'); return false; }
    }

    async function addTarget() {
        const account = selectedAccount();
        const mode = document.querySelector('input[name="target-mode"]:checked')?.value || 'DRAFT';
        if (!state.selectedPlatform) { setMessage('target-builder-error', '请先选择平台。'); byId('target-platform-xiaoheihe').focus(); return; }
        if (!account) { setMessage('target-builder-error', '请选择一个状态有效的账号。'); byId('target-account').focus(); return; }
        if (state.draft.targets.some(target => target.account_id === account.account_id)) { setMessage('target-builder-error', '同一草稿不能重复添加同一账号。'); return; }
        setMessage('target-builder-error', '');
        await saveTargets([...state.draft.targets, { platform: state.selectedPlatform, account_id: account.account_id, mode, persist_login: Boolean(account.persist_login) }]);
    }

    function renderTargets() {
        const container = byId('targets-list');
        container.replaceChildren(...state.draft.targets.map(target => {
            const row = document.createElement('article'); row.className = 'target-row';
            const platform = document.createElement('div'); const platformStrong = document.createElement('strong'); platformStrong.textContent = platformLabels[target.platform] || target.platform; const platformSmall = document.createElement('small'); platformSmall.textContent = '平台'; platform.append(platformStrong, platformSmall);
            const account = document.createElement('div'); const accountStrong = document.createElement('strong'); accountStrong.textContent = target.account_display_name || '平台账号'; const accountSmall = document.createElement('small'); accountSmall.textContent = target.account_id ? `账号 ${target.account_id.slice(0, 8)}…` : '账号'; account.append(accountStrong, accountSmall);
            const mode = document.createElement('div'); const modeBadge = document.createElement('span'); modeBadge.className = `badge ${target.mode === 'PUBLISH' ? 'text-bg-danger' : 'text-bg-info'}`; modeBadge.textContent = target.mode === 'PUBLISH' ? '公开发布' : '平台草稿'; mode.appendChild(modeBadge);
            const policy = document.createElement('div'); const policyStrong = document.createElement('strong'); policyStrong.textContent = target.persist_login ? '保持登录态' : '不持久会话'; const policySmall = document.createElement('small'); policySmall.textContent = '会话策略'; policy.append(policyStrong, policySmall);
            const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-outline-danger btn-sm'; remove.textContent = '移除'; remove.addEventListener('click', () => saveTargets(state.draft.targets.filter(item => item.account_id !== target.account_id)));
            row.append(platform, account, mode, policy, remove); return row;
        }));
        byId('targets-empty').classList.toggle('d-none', state.draft.targets.length > 0);
        byId('target-count').textContent = `${state.draft.targets.length} 个目标`;
        updateDraftMeta();
    }

    function validateStudio() {
        const issues = [];
        if (!state.draft.title.trim()) issues.push({ message: '填写文章标题', focus: 'draft-title' });
        if (publicBlocks().length === 0) issues.push({ message: '至少添加一个非空文字块或图片块', focus: 'add-text-block' });
        if (state.draft.targets.length === 0) issues.push({ message: '至少添加一个投递目标', focus: 'target-platform-xiaoheihe' });
        return issues;
    }

    function showValidation(issues) {
        const container = byId('validation-summary'); const list = byId('validation-list');
        list.replaceChildren(...issues.map(issue => { const item = document.createElement('li'); const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-link btn-sm p-0 align-baseline'; button.textContent = issue.message; button.addEventListener('click', () => byId(issue.focus)?.focus()); item.appendChild(button); return item; }));
        container.classList.remove('d-none'); container.focus();
        byId(issues[0].focus)?.focus({ preventScroll: false });
    }

    async function createPlan() {
        const issues = validateStudio();
        if (issues.length) { showValidation(issues); return; }
        byId('validation-summary').classList.add('d-none'); setMessage('execution-error', '');
        if (!(await saveDraftNow()) || state.conflictServerDraft) return;
        state.planBusy = true; byId('create-plan').setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(endpoint(root.dataset.planUrlTemplate, 'draft_id', state.draft.draft_id), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ revision: state.draft.revision }) });
            state.plan = await jsonResponse(response); renderPlan(); renderPlanReview(); coreModal('plan-review-modal').show();
        } catch (error) { setMessage('execution-error', error.message || '投递计划生成失败'); }
        finally { state.planBusy = false; byId('create-plan').removeAttribute('aria-busy'); }
    }

    function renderPlanReview() {
        const summary = byId('plan-review-summary');
        summary.replaceChildren(...state.plan.targets.map(target => {
            const card = document.createElement('article'); card.className = 'plan-review-card';
            const strong = document.createElement('strong'); strong.textContent = `${platformLabels[target.platform] || target.platform} · ${target.account_display_name || '平台账号'}`;
            const detail = document.createElement('div'); detail.className = 'small text-body-secondary'; detail.textContent = target.mode === 'PUBLISH' ? '公开发布（还需逐条二次确认）' : '保存到平台草稿箱';
            card.append(strong, detail); return card;
        }));
        const hasDraft = state.plan.targets.some(target => target.mode === 'DRAFT');
        byId('draft-confirmation-row').classList.toggle('d-none', !hasDraft);
        byId('draft-batch-confirmed').checked = false;
        setMessage('plan-review-error', '');
    }

    function planBadge(status) {
        if (['SUCCESS', 'DRAFT_SAVED', 'PUBLISHED'].includes(status)) return 'text-bg-success';
        if (['BLOCKED', 'FAILED', 'FATAL'].includes(status)) return 'text-bg-danger';
        if (['PARTIAL_FAIL', 'CONFIRMATION_REQUIRED'].includes(status)) return 'text-bg-warning';
        return 'text-bg-info';
    }

    function renderPlan() {
        if (!state.plan) return;
        byId('plan-result').classList.remove('d-none');
        byId('plan-id').textContent = `投递计划 ${state.plan.plan_id.slice(0, 8)}…`;
        byId('plan-status').className = `badge ${planBadge(state.plan.status)}`;
        byId('plan-status').textContent = planStatusLabels[state.plan.status] || state.plan.status;
        byId('plan-targets').replaceChildren(...state.plan.targets.map(target => {
            const row = document.createElement('article'); row.className = 'plan-target';
            const copy = document.createElement('div'); copy.className = 'plan-target-copy'; const strong = document.createElement('strong'); strong.textContent = `${platformLabels[target.platform] || target.platform} · ${target.account_display_name || '平台账号'}`; const small = document.createElement('small'); small.textContent = target.error_message || (target.operation_id ? `执行单 ${target.operation_id}` : target.mode === 'PUBLISH' ? '公开发布' : '平台草稿'); copy.append(strong, small);
            const actions = document.createElement('div'); actions.className = 'plan-target-actions'; const badge = document.createElement('span'); badge.className = `badge ${planBadge(target.status)}`; badge.textContent = planStatusLabels[target.status] || target.status; actions.appendChild(badge);
            row.append(copy, actions); return row;
        }));
    }

    async function executePlan(payload, { fromReview = false } = {}) {
        if (!state.plan || state.planBusy) return;
        state.planBusy = true; setMessage(fromReview ? 'plan-review-error' : 'publish-confirm-error', '');
        try {
            const response = await fetch(endpoint(root.dataset.planExecuteUrlTemplate, 'plan_id', state.plan.plan_id), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(payload) });
            const result = await response.json().catch(() => ({}));
            if (!response.ok && response.status !== 428) throw new Error(result.message || '投递计划执行失败');
            state.plan = result; renderPlan();
            if (fromReview) coreModal('plan-review-modal').hide();
            const confirmations = result.targets.filter(target => target.confirmation_required && target.confirmation_token);
            if (confirmations.length) {
                state.pendingPublishTargets = confirmations.slice(); showNextPublishConfirmation();
            } else if (result.targets.some(target => ['QUEUED', 'RUNNING'].includes(target.status))) {
                schedulePlanPoll();
            }
        } catch (error) { setMessage(fromReview ? 'plan-review-error' : 'publish-confirm-error', error.message || '投递计划执行失败'); }
        finally { state.planBusy = false; }
    }

    function showNextPublishConfirmation() {
        state.activePublishTarget = state.pendingPublishTargets.shift() || null;
        if (!state.activePublishTarget) { schedulePlanPoll(); return; }
        const target = state.activePublishTarget;
        const summary = byId('publish-target-summary'); summary.replaceChildren();
        const strong = document.createElement('strong'); strong.textContent = `${platformLabels[target.platform] || target.platform} · ${target.account_display_name || '平台账号'}`;
        const detail = document.createElement('div'); detail.className = 'small mt-1'; detail.textContent = `标题：${state.draft.title}`;
        summary.append(strong, detail); byId('publish-token-expiry').textContent = target.expires_at ? `一次性确认令牌有效至 ${formatDate(target.expires_at)}` : '该确认令牌只能使用一次。';
        setMessage('publish-confirm-error', ''); coreModal('publish-confirm-modal').show();
    }

    async function confirmPublishTarget() {
        const target = state.activePublishTarget;
        if (!target) return;
        await executePlan({ target_ids: [target.target_id], draft_batch_confirmed: true, confirmations: { [target.target_id]: target.confirmation_token } });
        coreModal('publish-confirm-modal').hide();
        if (state.pendingPublishTargets.length) setTimeout(showNextPublishConfirmation, 250);
    }

    function schedulePlanPoll() {
        clearTimeout(state.pollTimer);
        if (!state.plan || state.pollCount >= 12) return;
        state.pollTimer = setTimeout(async () => {
            state.pollCount += 1;
            try {
                state.plan = await jsonResponse(await fetch(endpoint(root.dataset.planDetailUrlTemplate, 'plan_id', state.plan.plan_id), { headers: { Accept: 'application/json' } })); renderPlan();
                if (state.plan.targets.some(target => ['QUEUED', 'RUNNING'].includes(target.status))) schedulePlanPoll();
            } catch (error) { setMessage('execution-error', error.message || '无法刷新执行状态'); }
        }, 2500);
    }

    function renderDraftLibrary() {
        byId('draft-library-list').replaceChildren(...state.drafts.map(draft => {
            const item = document.createElement('article'); item.className = 'library-item'; const copy = document.createElement('div'); copy.className = 'library-item-copy'; const title = document.createElement('strong'); title.textContent = draft.title || '未命名草稿'; const meta = document.createElement('small'); meta.textContent = `${sourceLabels[draft.source_type] || draft.source_type} · 修订 ${draft.revision} · ${formatDate(draft.updated_at)}`; copy.append(title, meta); const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-primary btn-sm'; button.textContent = draft.draft_id === state.draft?.draft_id ? '当前草稿' : '继续编辑'; button.disabled = draft.draft_id === state.draft?.draft_id; button.addEventListener('click', async () => { await saveDraftNow(); const full = await jsonResponse(await fetch(endpoint(root.dataset.draftUrlTemplate, 'draft_id', draft.draft_id))); await openDraft(full); coreModal('source-library-modal').hide(); }); item.append(copy, button); return item;
        }));
        byId('draft-library-empty').classList.toggle('d-none', state.drafts.length > 0);
    }

    async function refreshDrafts() {
        const payload = await jsonResponse(await fetch(`${root.dataset.draftsUrl}?limit=50&offset=0`, { headers: { Accept: 'application/json' } }));
        state.drafts = Array.isArray(payload.drafts) ? payload.drafts : [];
        renderDraftLibrary(); return state.drafts;
    }

    async function loadLegacyArticles() {
        if (byId('legacy-library-list').dataset.loaded === 'true') return;
        byId('legacy-library-loading').classList.remove('d-none'); setMessage('legacy-library-error', '');
        try {
            const payload = await jsonResponse(await fetch(`${root.dataset.legacyUrl}?limit=50&offset=0`, { headers: { Accept: 'application/json' } }));
            const articles = Array.isArray(payload.articles) ? payload.articles : [];
            byId('legacy-library-list').replaceChildren(...articles.map(article => {
                const articleId = article.article_id ?? article.id;
                const item = document.createElement('article'); item.className = 'library-item'; const copy = document.createElement('div'); copy.className = 'library-item-copy'; const title = document.createElement('strong'); title.textContent = article.title || article.filename || `历史文章 #${articleId}`; const meta = document.createElement('small'); meta.textContent = `${article.filename || '历史文章'} · ${formatDate(article.created_at)}`; copy.append(title, meta); const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-primary btn-sm'; button.textContent = '继续编辑'; button.addEventListener('click', () => copyLegacyArticle(articleId, button)); item.append(copy, button); return item;
            }));
            byId('legacy-library-list').dataset.loaded = 'true';
        } catch (error) { setMessage('legacy-library-error', error.message || '历史文章读取失败'); }
        finally { byId('legacy-library-loading').classList.add('d-none'); }
    }

    async function copyLegacyArticle(articleId, button) {
        button.disabled = true; button.textContent = '正在复制…';
        try { const draft = await jsonResponse(await fetch(endpoint(root.dataset.fromLegacyUrlTemplate, 'article_id', articleId), { method: 'POST', headers: { Accept: 'application/json' } })); await refreshDrafts(); await openDraft(draft); coreModal('source-library-modal').hide(); }
        catch (error) { setMessage('legacy-library-error', error.message || '历史文章复制失败'); }
        finally { button.disabled = false; button.textContent = '继续编辑'; }
    }

    async function createBlankDraft() {
        try { await saveDraftNow(); const draft = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title: '', blocks: [] }) })); await refreshDrafts(); await openDraft(draft); coreModal('source-library-modal').hide(); requestAnimationFrame(() => byId('draft-title').focus()); }
        catch (error) { setMessage('studio-fatal', error.message || '无法新建草稿'); }
    }

    async function importDocx(file) {
        if (!file) return;
        setSaveState('saving', '正在导入 DOCX');
        const form = new FormData(); form.append('file', file);
        try { await saveDraftNow(); const draft = await jsonResponse(await fetch(root.dataset.docxUrl, { method: 'POST', body: form, headers: { Accept: 'application/json' } })); await refreshDrafts(); await openDraft(draft); coreModal('source-library-modal').hide(); }
        catch (error) { setMessage('studio-fatal', error.message || 'DOCX 导入失败'); setSaveState('error', 'DOCX 导入失败'); }
    }

    async function resolveConflict(useServer) {
        if (useServer) {
            const server = state.conflictServerDraft; applyDraft(server); await localDraftPut(false); setSaveState('synced', '已采用服务端版本'); coreModal('conflict-modal').hide(); return;
        }
        const local = { title: state.draft.title, blocks: publicBlocks() };
        try { const copy = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(local) })); state.conflictServerDraft = null; await refreshDrafts(); await openDraft(copy); setSaveState('synced', '本地内容已另存副本'); coreModal('conflict-modal').hide(); }
        catch (error) { setMessage('content-error', error.message || '另存草稿副本失败'); }
    }

    function bindEvents() {
        byId('draft-title').addEventListener('input', event => { state.draft.title = event.target.value; byId('side-draft-title').textContent = event.target.value || '未命名草稿'; markDirty(); });
        byId('add-text-block').addEventListener('click', () => addTextBlock());
        byId('blocks-empty').addEventListener('click', event => { if (event.target.closest('[data-action="empty-add-text"]')) addTextBlock(); });
        byId('content-blocks').addEventListener('input', event => { const block = state.draft.blocks.find(item => item.block_id === event.target.dataset.blockId); if (!block) return; if (event.target.dataset.action === 'text-input') block.text = event.target.value; if (event.target.dataset.action === 'image-alt') block.alt = event.target.value; markDirty(); });
        byId('content-blocks').addEventListener('click', event => { const button = event.target.closest('button[data-action]'); if (!button) return; if (button.dataset.action === 'move-up') moveBlock(button.dataset.blockId, -1); if (button.dataset.action === 'move-down') moveBlock(button.dataset.blockId, 1); if (button.dataset.action === 'delete-block') { state.draft.blocks = state.draft.blocks.filter(block => block.block_id !== button.dataset.blockId); renderBlocks(); markDirty(); } });
        byId('asset-upload').addEventListener('change', event => { uploadAssets(Array.from(event.target.files || [])); event.target.value = ''; });
        document.querySelectorAll('input[name="target-platform"]').forEach(input => input.addEventListener('change', event => { state.selectedPlatform = event.target.value; state.selectedAccountId = null; loadAccounts(state.selectedPlatform); }));
        byId('target-account').addEventListener('change', event => { state.selectedAccountId = event.target.value || null; const account = selectedAccount(); const control = byId('target-persist-login'); control.disabled = !account; control.checked = Boolean(account?.persist_login); });
        byId('target-persist-login').addEventListener('change', event => updateSessionPolicy(event.target.checked));
        byId('add-target').addEventListener('click', addTarget);
        byId('create-plan').addEventListener('click', createPlan);
        byId('execute-plan').addEventListener('click', () => { const hasDraft = state.plan.targets.some(target => target.mode === 'DRAFT'); if (hasDraft && !byId('draft-batch-confirmed').checked) { setMessage('plan-review-error', '请先勾选平台草稿批量摘要确认。'); byId('draft-batch-confirmed').focus(); return; } executePlan({ draft_batch_confirmed: !hasDraft || byId('draft-batch-confirmed').checked, confirmations: {} }, { fromReview: true }); });
        byId('confirm-publish-target').addEventListener('click', confirmPublishTarget);
        byId('new-blank-draft').addEventListener('click', createBlankDraft);
        byId('docx-import').addEventListener('change', event => { importDocx(event.target.files?.[0]); event.target.value = ''; });
        byId('legacy-tab').addEventListener('shown.coreui.tab', loadLegacyArticles);
        byId('conflict-use-server').addEventListener('click', () => resolveConflict(true));
        byId('conflict-save-copy').addEventListener('click', () => resolveConflict(false));
        window.addEventListener('beforeunload', () => { if (state.dirty) localDraftPut(true); });
    }

    async function init() {
        bindEvents(); state.localDb = await openLocalDb();
        try {
            const drafts = await refreshDrafts();
            const requestedId = new URLSearchParams(window.location.search).get('draft_id');
            let draft = requestedId ? await jsonResponse(await fetch(endpoint(root.dataset.draftUrlTemplate, 'draft_id', requestedId), { headers: { Accept: 'application/json' } })) : drafts[0];
            if (!draft) draft = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title: '', blocks: [] }) }));
            await openDraft(draft);
        } catch (error) {
            byId('studio-loading').classList.add('d-none'); setMessage('studio-fatal', `${error.message || '创作工作台加载失败'}。请确认 Content Studio 后端已启用。`); setSaveState('error', '加载失败');
        }
    }

    init();
})();
