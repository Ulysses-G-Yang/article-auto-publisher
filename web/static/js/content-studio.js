(function () {
    'use strict';

    const root = document.getElementById('content-studio');
    if (!root) return;

    const byId = id => document.getElementById(id);
    const sourceLabels = { BLANK: '空白草稿', DOCX: 'DOCX 导入', LEGACY_ARTICLE: '历史文章副本', SYSTEM_SEED: '系统草稿' };
    const planStatusLabels = {
        READY: '待执行', CREATING: '正在创建执行单', QUEUED: '已排队', RUNNING: '执行中', SUCCESS: '已完成',
        PARTIAL_FAIL: '部分失败', FATAL: '执行失败', CONFIRMATION_REQUIRED: '待公开确认',
        DRAFT_SAVED: '平台草稿已保存', PUBLISHED: '已公开发布', BLOCKED: '已拦截', FAILED: '失败',
        RESULT_UNKNOWN: '结果未知，需人工核对',
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
        switcherSelected: {},
        switcherModes: {},
        switcherController: null,
        switcherSequence: 0,
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
        if (!state.localDb || !state.draft) return Promise.resolve();
        const snapshot = {
            draft_id: state.draft.draft_id,
            revision: state.draft.revision,
            title: state.draft.title,
            content_schema_version: Number(state.draft.content_schema_version) === 2 ? 2 : 1,
            document: isV2Draft() ? cloneValue(state.draft.document) : null,
            blocks: isV2Draft() ? [] : cloneValue(state.draft.blocks),
            cover: cloneValue(normalizedCover(state.draft.cover)),
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
            const requestIsV2 = isV2Draft();
            const requestDocument = requestIsV2 ? cloneValue(state.draft.document) : null;
            const requestBlocks = requestIsV2 ? null : publicBlocks();
            const requestCover = coverRequest(state.draft.cover);
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
            const requestSnapshot = contentSnapshot(state.draft);
            const response = await fetch(endpoint(root.dataset.draftUrlTemplate, 'draft_id', state.draft.draft_id), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(requestBody),
            });
            const payload = await response.json().catch(() => ({}));
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
            state.draft.revision = payload.revision;
            state.draft.updated_at = payload.updated_at;
            state.draft.source_type = payload.source_type;
            const changedDuringRequest = contentSnapshot(state.draft) !== requestSnapshot;
            if (!changedDuringRequest && requestIsV2 && Number(payload.content_schema_version) === 2) {
                state.draft.content_schema_version = 2;
                state.draft.document = cloneValue(payload.document);
                state.draft.cover = normalizedCover(payload.cover);
            } else if (!changedDuringRequest && !requestIsV2) {
                state.draft.cover = normalizedCover(payload.cover);
            }
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
        const schemaVersion = Number(payload.content_schema_version) === 2 ? 2 : 1;
        state.draft = {
            ...payload,
            title: payload.title || '',
            blocks: Array.isArray(payload.blocks) ? payload.blocks.map((block, position) => ({ ...block, position })) : [],
            content_schema_version: schemaVersion,
            document: schemaVersion === 2 ? cloneValue(payload.document) : null,
            cover: normalizedCover(payload.cover),
            targets: Array.isArray(payload.targets) ? payload.targets : [],
        };
        // 回填滑块状态：已有目标 → 平台开关/账号勾选/模式
        state.switcherToggles = {};
        state.switcherSelected = {};
        state.switcherModes = {};
        for (const target of state.draft.targets) {
            state.switcherToggles[target.platform] = true;
            state.switcherModes[target.platform] = target.mode;
            const selected = state.switcherSelected[target.platform] || (state.switcherSelected[target.platform] = []);
            if (!selected.includes(target.account_id)) selected.push(target.account_id);
        }
        state.dirty = false;
        state.conflictServerDraft = null;
        invalidatePlan();
        if (render) {
            byId('draft-title').value = state.draft.title;
            renderBlocks();
            renderTargetSwitcher();
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
        const serverIsV2 = isV2Draft(state.draft);
        const localMatchesSchema = serverIsV2
            ? Number(local?.content_schema_version) === 2
            : Number(local?.content_schema_version || 1) !== 2;
        const localSameRevision = Boolean(local?.dirty && local.revision === payload.revision);
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
        const indicatorText = byId('save-indicator-text');
        if (indicatorText) {
             const sourceBadge = byId('draft-source-badge');
             if (sourceBadge) sourceBadge.textContent = source;
             const revElem = byId('draft-revision');
             if (revElem) revElem.textContent = `修订 ${state.draft.revision}`;
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
            byId('side-revision').textContent = String(state.draft.revision);
        }
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
        list.replaceChildren(...state.platforms
            .filter(platform => platform.delivery_enabled)
            .map(platform => switcherRow(platform)));
        byId('targets-empty').classList.toggle('d-none', (state.draft?.targets || []).length > 0);
    }

    function switcherRow(platform) {
        const on = Boolean(state.switcherToggles[platform.id]);
        const row = document.createElement('div');
        row.className = `target-platform-row${on ? ' is-on' : ''}`;
        row.dataset.platformId = platform.id;

        // 头部：图标 + 名称 + 模式滑块（草稿/公开）+ 平台开关
        const head = document.createElement('div');
        head.className = 'target-platform-head';
        head.append(switcherPlatformIcon(platform));
        const name = document.createElement('strong');
        name.className = 'target-platform-name';
        name.textContent = platform.display_name;
        head.appendChild(name);

        const mode = document.createElement('div');
        mode.className = 'target-mode-switch';
        mode.setAttribute('role', 'group');
        mode.setAttribute('aria-label', `${platform.display_name} 投递模式`);
        const modeDraft = document.createElement('button');
        modeDraft.type = 'button';
        modeDraft.className = `mode-seg${switcherMode(platform.id) === 'DRAFT' ? ' is-active' : ''}`;
        modeDraft.textContent = '平台草稿';
        modeDraft.dataset.mode = 'DRAFT';
        const modePublish = document.createElement('button');
        modePublish.type = 'button';
        modePublish.className = `mode-seg${switcherMode(platform.id) === 'PUBLISH' ? ' is-active' : ''}`;
        modePublish.textContent = '公开发布';
        modePublish.dataset.mode = 'PUBLISH';
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
        toggle.setAttribute('aria-label', `启用${platform.display_name}投递`);
        toggle.addEventListener('change', () => togglePlatform(platform.id, toggle.checked));
        switchWrap.appendChild(toggle);
        head.appendChild(switchWrap);
        row.appendChild(head);

        // 展开区：账号多选
        const body = document.createElement('div');
        body.className = 'target-platform-body';
        if (on) {
            if (state.switcherAccounts[platform.id] === undefined) {
                body.appendChild(switcherBodyMessage('正在读取账号…'));
                loadSwitcherAccounts(platform.id);
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
            const empty = document.createElement('div');
            empty.className = 'target-accounts-empty';
            empty.textContent = '该平台暂无可用账号（需要 VALID 登录态）。';
            return [empty];
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

    async function loadSwitcherAccounts(platformId) {
        const sequence = ++state.switcherSequence;
        state.switcherController?.abort();
        state.switcherController = new AbortController();
        try {
            const url = endpoint(root.dataset.accountsUrlTemplate, 'platform', platformId);
            const payload = await jsonResponse(await fetch(url, { signal: state.switcherController.signal, headers: { Accept: 'application/json' } }));
            if (sequence !== state.switcherSequence) return;
            if (payload.platform && payload.platform !== platformId) throw new Error('账号响应与所选平台不匹配');
            state.switcherAccounts[platformId] = (Array.isArray(payload.accounts) ? payload.accounts : []).filter(account => account.session_status === 'VALID');
            state.switcherSelected[platformId] = state.switcherSelected[platformId] || [];
            reRenderSwitcherRow(platformId);
        } catch (error) {
            if (error.name === 'AbortError' || sequence !== state.switcherSequence) return;
            state.switcherAccounts[platformId] = [];
            setMessage('target-builder-error', `加载${platformLabel(platformId)}账号失败：${error.message || '未知错误'}`);
            reRenderSwitcherRow(platformId);
        }
    }

    function reRenderSwitcherRow(platformId) {
        const list = byId('target-switcher-list');
        if (!list) return;
        const row = list.querySelector(`.target-platform-row[data-platform-id="${platformId}"]`);
        const platform = state.platforms.find(item => item.id === platformId);
        if (row && platform) row.replaceWith(switcherRow(platform));
    }

    function togglePlatform(platformId, checked) {
        state.switcherToggles[platformId] = checked;
        if (checked) {
            if (state.switcherAccounts[platformId] === undefined) loadSwitcherAccounts(platformId);
        } else {
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
        rebuildTargets();
    }

    function setSwitcherMode(platformId, mode) {
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
            for (const accountId of state.switcherSelected[platform.id] || []) {
                const account = accounts.find(item => item.account_id === accountId);
                if (!account) continue;
                targets.push({ platform: platform.id, account_id: accountId, mode, persist_login: Boolean(account.persist_login) });
            }
        }
        const current = state.draft.targets || [];
        const same = current.length === targets.length && current.every((target, index) =>
            target.platform === targets[index].platform
            && target.account_id === targets[index].account_id
            && target.mode === targets[index].mode);
        if (!same) saveTargets(targets);
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
            invalidatePlan(); renderTargets(); updateDraftMeta(); await localDraftPut(false); setSaveState('synced', '已同步');
            byId('targets-empty').classList.toggle('d-none', state.draft.targets.length > 0);
            return true;
        } catch (error) { setMessage('target-builder-error', error.message || '投递目标保存失败'); return false; }
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
        updateDraftMeta();
    }

    function validateStudio() {
        const issues = [];
        if (!state.draft.title.trim()) issues.push({ message: '填写文章标题', focus: 'draft-title' });
        if (publicBlocks().length === 0) issues.push({ message: '正文还不能为空，输入文字或插入图片', focus: 'rich-editor' });
        if (state.draft.targets.length === 0) issues.push({ message: '至少选择一个投递目标', focus: 'target-switcher-list' });
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
            const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`;
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
        if (['PARTIAL_FAIL', 'CONFIRMATION_REQUIRED', 'RESULT_UNKNOWN'].includes(status)) return 'text-bg-warning';
        return 'text-bg-info';
    }

    function platformDraftBoxUrl(platform) {
        // 各平台草稿箱直达地址（2026-08 盘点确认）
        return {
            xiaoheihe: 'https://www.xiaoheihe.cn/creator/draft',
            zhihu: 'https://www.zhihu.com/creator/manage/creation/drafts',
            weibo: 'https://card.weibo.com/article/v5/editor#/draft',
            smzdm: 'https://post.smzdm.com/tougao/',
            baijiahao: 'https://baijiahao.baidu.com/builder/rc/manage',
            xiaohongshu: 'https://creator.xiaohongshu.com/publish/publish',
            zol: 'https://post.zol.com.cn/v2/home',
        }[platform] || '';
    }

    function planTargetDetail(target) {
        if (target.status === 'RESULT_UNKNOWN') {
            return '结果未知，请先到平台人工核对；系统不会自动重试。';
        }
        if (target.status === 'PARTIAL_FAIL') {
            return target.error_message || '部分内容未完整处理，请核对图片和平台结果。';
        }
        if (target.status === 'CREATING') {
            return '正在创建独立执行单，请稍候。';
        }
        return target.error_message || (target.operation_id
            ? `执行单 ${target.operation_id}`
            : target.mode === 'PUBLISH' ? '公开发布' : '平台草稿');
    }

    function hasInFlightTarget(plan) {
        return plan.targets.some(target => ['CREATING', 'QUEUED', 'RUNNING'].includes(target.status));
    }

    function renderPlan() {
        if (!state.plan) return;
        byId('plan-result').classList.remove('d-none');
        byId('plan-id').textContent = `投递计划 ${state.plan.plan_id.slice(0, 8)}…`;
        byId('plan-status').className = `badge ${planBadge(state.plan.status)}`;
        byId('plan-status').textContent = planStatusLabels[state.plan.status] || state.plan.status;
        byId('plan-targets').replaceChildren(...state.plan.targets.map(target => {
            const row = document.createElement('article'); row.className = 'plan-target';
            const copy = document.createElement('div'); copy.className = 'plan-target-copy'; const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`; const small = document.createElement('small'); small.textContent = planTargetDetail(target); copy.append(strong, small);
            const actions = document.createElement('div'); actions.className = 'plan-target-actions'; const badge = document.createElement('span'); badge.className = `badge ${planBadge(target.status)}`; badge.textContent = planStatusLabels[target.status] || target.status; actions.appendChild(badge);
            if (target.status === 'DRAFT_SAVED') {
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
            } else if (hasInFlightTarget(result)) {
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
        const strong = document.createElement('strong'); strong.textContent = `${platformLabel(target.platform)} · ${target.account_display_name || '平台账号'}`;
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
                if (hasInFlightTarget(state.plan)) schedulePlanPoll();
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
        try { const copy = await jsonResponse(await fetch(root.dataset.draftsUrl, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(local) })); state.conflictServerDraft = null; await refreshDrafts(); await openDraft(copy); setSaveState('synced', '本地内容已另存副本'); coreModal('conflict-modal').hide(); }
        catch (error) { setMessage('content-error', error.message || '另存草稿副本失败'); }
    }

    function bindEvents() {
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
        byId('create-plan').addEventListener('click', createPlan);
        byId('save-draft-now').addEventListener('click', () => saveDraftNow());
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
        try {
            bindEvents();
            state.localDb = await openLocalDb();
            await fetchPlatforms();
            const drafts = await refreshDrafts();
            const params = new URLSearchParams(window.location.search);
            const requestedId = params.get('draft_id');
            let draft = requestedId
                ? await jsonResponse(await fetch(endpoint(root.dataset.draftUrlTemplate, 'draft_id', requestedId), { headers: { Accept: 'application/json' } }))
                : drafts[0];

            if (!draft) {
                draft = await jsonResponse(await fetch(root.dataset.draftsUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                    body: JSON.stringify({ title: '', blocks: [] }),
                }));
                await refreshDrafts();
            }

            await openDraft(draft);
        } catch (error) {
            byId('studio-loading').classList.add('d-none');
            setMessage('studio-fatal', `加载工作台失败：${error.message || '未知错误'}`);
        }
    }


    init();
})();
