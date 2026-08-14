(function () {
    'use strict';

    const root = document.getElementById('delivery-app');
    if (!root) return;

    const state = {
        platforms: [],
        platform: null,
        accounts: [],
        selectedAccountId: null,
        mode: 'DRAFT',
        accountRequestSequence: 0,
        accountRequestController: null,
        sessionPolicyBusy: false,
        deliveryBusy: false,
        confirmationToken: null,
    };

    const byId = id => document.getElementById(id);
    const sessionLabels = {
        VALID: '有效', EXPIRED: '已过期', VERIFYING: '验证中', BUSY: '使用中', LOGIN_REQUIRED: '需要登录',
    };
    const sessionClasses = {
        VALID: 'text-bg-success', EXPIRED: 'text-bg-secondary', VERIFYING: 'text-bg-info',
        BUSY: 'text-bg-warning', LOGIN_REQUIRED: 'text-bg-danger',
    };

    function endpoint(template, key, value) {
        return template.replace(`{${key}}`, encodeURIComponent(value));
    }

    function platformLabel(platform) {
        const item = state.platforms.find(entry => entry.id === platform);
        return item ? item.display_name : platform;
    }

    function setError(id, message) {
        const element = byId(id);
        element.textContent = message || '';
        element.classList.toggle('d-none', !message);
    }

    async function responseJson(response) {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(payload.message || '请求失败，请稍后重试。');
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function selectedAccount() {
        return state.accounts.find(account => account.account_id === state.selectedAccountId) || null;
    }

    function safeAccountLabel(account) {
        if (!account) return '未选择';
        return account.masked_platform_user_id
            ? `${account.display_name}（${account.masked_platform_user_id}）`
            : account.display_name;
    }

    function updateSummary() {
        byId('summary-platform').textContent = state.platform ? platformLabel(state.platform) : '未选择';
        byId('summary-account').textContent = safeAccountLabel(selectedAccount());
        byId('summary-mode').textContent = state.mode === 'DRAFT' ? '保存到平台草稿箱' : '公开发布（需二次确认）';
        byId('submit-delivery').disabled = !state.platform || !state.selectedAccountId || state.deliveryBusy;
    }

    async function loadPlatforms() {
        const track = byId('platform-track');
        const loading = byId('platform-loading');
        try {
            const response = await fetch(root.dataset.platformsUrl, { headers: { Accept: 'application/json' } });
            const payload = await responseJson(response);
            const platforms = Array.isArray(payload) ? payload : (payload.platforms || []);
            state.platforms = platforms
                .filter(item => item && typeof item.id === 'string' && item.delivery_enabled === true)
                .map(item => ({
                    id: item.id,
                    display_name: item.display_name || item.id,
                    logo_url: typeof item.logo_url === 'string' ? item.logo_url : '',
                    sort_order: Number(item.sort_order || 0),
                }))
                .sort((left, right) => left.sort_order - right.sort_order);
            renderPlatformOptions();
        } catch (error) {
            setError('platform-error', `平台目录加载失败：${error.message || '未知错误'}`);
        } finally {
            loading?.classList.add('d-none');
            track?.classList.remove('d-none');
        }
    }

    function renderPlatformOptions() {
        const track = byId('platform-track');
        if (!track) return;
        track.replaceChildren(...state.platforms.map((platform, index) => {
            const input = document.createElement('input');
            input.className = 'btn-check';
            input.type = 'radio';
            input.name = 'delivery-platform';
            input.id = `platform-${platform.id}`;
            input.value = platform.id;
            input.autocomplete = 'off';
            input.addEventListener('change', () => selectPlatform(platform.id));

            const label = document.createElement('label');
            label.className = 'btn platform-option';
            label.htmlFor = input.id;
            const icon = document.createElement('i');
            icon.className = 'cil-paper-plane';
            icon.setAttribute('aria-hidden', 'true');
            const copy = document.createElement('span');
            const name = document.createElement('strong');
            name.textContent = platform.display_name;
            const note = document.createElement('small');
            note.textContent = '可投递';
            copy.append(name, note);
            label.append(icon, copy);
            return [input, label];
        }));
        if (state.platforms.length === 0) {
            setError('platform-error', '当前没有已开放投递的平台，请先在“平台账号”页完成账号接入与投递验收。');
        }
    }

    function resetAccounts() {
        state.accounts = [];
        state.selectedAccountId = null;
        byId('account-track').replaceChildren();
        byId('account-selector').classList.add('d-none');
        byId('account-empty').classList.add('d-none');
        byId('session-policy-panel').classList.add('d-none');
        setError('account-error', '');
        setError('session-policy-error', '');
        updateSummary();
    }

    function accountChoice(account, index) {
        const wrapper = document.createElement('div');
        wrapper.className = 'account-choice';
        const input = document.createElement('input');
        input.className = 'btn-check';
        input.type = 'radio';
        input.name = 'delivery-account';
        input.id = `delivery-account-${index}`;
        input.value = account.account_id;
        input.autocomplete = 'off';
        input.disabled = account.session_status !== 'VALID';
        input.addEventListener('change', () => selectAccount(account.account_id));

        const label = document.createElement('label');
        label.className = 'btn';
        label.htmlFor = input.id;
        const copy = document.createElement('span');
        copy.className = 'account-choice-copy';
        const name = document.createElement('strong');
        name.textContent = account.display_name || '未命名账号';
        const masked = document.createElement('small');
        masked.textContent = account.masked_platform_user_id || '平台 ID 已隐藏';
        copy.append(name, masked);
        const badge = document.createElement('span');
        badge.className = `badge session-badge ${sessionClasses[account.session_status] || 'text-bg-secondary'}`;
        badge.textContent = sessionLabels[account.session_status] || account.session_status || '状态未知';
        label.append(copy, badge);
        wrapper.append(input, label);
        return wrapper;
    }

    function renderAccounts() {
        const track = byId('account-track');
        track.replaceChildren();
        state.accounts.forEach((account, index) => track.appendChild(accountChoice(account, index)));
        const add = document.createElement('a');
        add.className = 'account-add';
        add.href = '/accounts';
        add.innerHTML = '<i class="cil-plus" aria-hidden="true"></i><span>添加账号</span>';
        track.appendChild(add);
        byId('account-selector').classList.remove('d-none');
        byId('account-empty').classList.toggle('d-none', state.accounts.length > 0);
        byId('account-help').textContent = state.accounts.length
            ? '请选择一个状态有效的账号；系统不会自动选择。'
            : '当前平台没有返回账号。';
    }

    async function loadAccounts(platform) {
        const requestSequence = ++state.accountRequestSequence;
        state.accountRequestController?.abort();
        state.accountRequestController = new AbortController();
        resetAccounts();
        byId('account-placeholder').classList.add('d-none');
        byId('account-loading').classList.remove('d-none');
        byId('account-help').textContent = `正在读取${platformLabel(platform)}可用账号…`;
        try {
            const url = endpoint(root.dataset.accountsUrlTemplate, 'platform', platform);
            const response = await fetch(url, { headers: { Accept: 'application/json' }, signal: state.accountRequestController.signal });
            const payload = await responseJson(response);
            if (requestSequence !== state.accountRequestSequence || platform !== state.platform) return;
            if (payload.platform && payload.platform !== platform) throw new Error('平台与账号响应不匹配，请刷新后重试。');
            state.accounts = Array.isArray(payload.accounts) ? payload.accounts : [];
            renderAccounts();
        } catch (error) {
            if (error.name === 'AbortError' || requestSequence !== state.accountRequestSequence) return;
            setError('account-error', error.message || '账号加载失败，请稍后重试。');
            byId('account-help').textContent = '账号加载失败';
        } finally {
            if (requestSequence === state.accountRequestSequence) byId('account-loading').classList.add('d-none');
        }
    }

    function selectPlatform(platform) {
        if (!state.platforms.some(item => item.id === platform)) {
            setError('platform-error', '该平台未开放投递，请重新选择。');
            return;
        }
        setError('platform-error', '');
        state.platform = platform;
        state.confirmationToken = null;
        loadAccounts(platform);
        updateSummary();
    }

    function selectAccount(accountId) {
        const account = state.accounts.find(item => item.account_id === accountId);
        if (!account || account.session_status !== 'VALID') return;
        state.selectedAccountId = accountId;
        state.confirmationToken = null;
        byId('persist-login').checked = Boolean(account.persist_login);
        byId('session-policy-panel').classList.remove('d-none');
        setError('session-policy-error', '');
        updateSummary();
    }

    async function updateSessionPolicy(persistLogin) {
        const account = selectedAccount();
        if (!account || state.sessionPolicyBusy) return;
        const control = byId('persist-login');
        const previous = Boolean(account.persist_login);
        state.sessionPolicyBusy = true;
        control.disabled = true;
        setError('session-policy-error', '');
        try {
            const url = endpoint(root.dataset.sessionPolicyUrlTemplate, 'account_id', account.account_id);
            const response = await fetch(url, {
                method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ persist_login: persistLogin }),
            });
            const updated = await responseJson(response);
            if (updated.account_id !== account.account_id) throw new Error('账号响应不匹配，请刷新后重试。');
            Object.assign(account, updated);
            control.checked = Boolean(account.persist_login);
        } catch (error) {
            control.checked = previous;
            setError('session-policy-error', error.message || '会话策略更新失败。');
        } finally {
            state.sessionPolicyBusy = false;
            control.disabled = false;
        }
    }

    function articlePayload(confirmationToken = null) {
        return {
            article: { title: byId('article-title').value.trim(), body: byId('article-body').value.trim() },
            platform: state.platform,
            account_id: state.selectedAccountId,
            mode: state.mode,
            confirmation_token: confirmationToken,
        };
    }

    function validateDelivery() {
        if (!state.platform) return '请先选择投递平台。';
        const account = selectedAccount();
        if (!account) return '请选择投递账号。';
        if (account.session_status !== 'VALID') return '所选账号当前不可用，请重新选择。';
        if (!byId('article-title').value.trim()) return '请填写文章标题。';
        if (!byId('article-body').value.trim()) return '请填写完整正文。';
        return null;
    }

    function summaryRows(container, includeRisk = false) {
        const account = selectedAccount();
        const values = [
            ['平台', platformLabel(state.platform)], ['账号', safeAccountLabel(account)],
            ['标题', byId('article-title').value.trim()], ['模式', state.mode === 'DRAFT' ? '保存到平台草稿箱' : '公开发布'],
        ];
        container.replaceChildren(...values.map(([term, value]) => {
            const row = document.createElement('div'); const dt = document.createElement('dt'); const dd = document.createElement('dd');
            dt.textContent = term; dd.textContent = value; row.append(dt, dd); return row;
        }));
        container.classList.toggle('confirmation-risk', includeRisk);
    }

    function showDraftConfirmation() {
        const error = validateDelivery();
        if (error) { setError('delivery-error', error); return; }
        setError('delivery-error', '');
        summaryRows(byId('draft-confirmation-summary'));
        bootstrap.Modal.getOrCreateInstance(byId('draft-confirmation')).show();
    }

    function showPublishConfirmation(payload) {
        state.confirmationToken = payload.confirmation_token;
        summaryRows(byId('publish-confirmation-summary'), true);
        byId('confirmation-expiry').textContent = payload.expires_at ? `确认令牌有效期至：${payload.expires_at}` : '确认令牌为一次性使用。';
        bootstrap.Modal.getOrCreateInstance(byId('publish-confirmation')).show();
    }

    function renderOperation(payload) {
        byId('operation-status').textContent = payload.status || 'QUEUED';
        byId('operation-id').textContent = payload.operation_id || '—';
        byId('operation-mode').textContent = payload.mode || state.mode;
        const link = byId('account-activity-link');
        link.href = endpoint(root.dataset.activityUrlTemplate, 'account_id', state.selectedAccountId);
        byId('operation-result').classList.remove('d-none');
    }

    async function submitDelivery(confirmationToken = null) {
        if (state.deliveryBusy) return;
        const validationError = validateDelivery();
        if (validationError) { setError('delivery-error', validationError); return; }
        state.deliveryBusy = true;
        updateSummary();
        setError('delivery-error', '');
        try {
            const response = await fetch(root.dataset.deliveryUrl, {
                method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(articlePayload(confirmationToken)),
            });
            const payload = await response.json().catch(() => ({}));
            if (response.status === 428 && payload.error === 'PUBLISH_CONFIRMATION_REQUIRED') {
                showPublishConfirmation(payload);
                return;
            }
            if (!response.ok) throw new Error(payload.message || '执行单创建失败，请稍后重试。');
            if (payload.platform !== state.platform || payload.account?.account_id !== state.selectedAccountId) {
                throw new Error('执行单与所选平台或账号不匹配，请停止操作并刷新页面。');
            }
            state.confirmationToken = null;
            renderOperation(payload);
        } catch (error) {
            setError('delivery-error', error.message || '执行单创建失败。');
        } finally {
            state.deliveryBusy = false;
            updateSummary();
        }
    }

    document.querySelectorAll('input[name="delivery-mode"]').forEach(input => input.addEventListener('change', event => { state.mode = event.target.value; state.confirmationToken = null; updateSummary(); }));
    byId('persist-login').addEventListener('change', event => updateSessionPolicy(event.target.checked));
    byId('submit-delivery').addEventListener('click', () => state.mode === 'DRAFT' ? showDraftConfirmation() : submitDelivery(null));
    byId('confirm-draft').addEventListener('click', () => { bootstrap.Modal.getOrCreateInstance(byId('draft-confirmation')).hide(); submitDelivery(null); });
    byId('confirm-publish').addEventListener('click', () => { bootstrap.Modal.getOrCreateInstance(byId('publish-confirmation')).hide(); submitDelivery(state.confirmationToken); });
    loadPlatforms();
    updateSummary();
})();
