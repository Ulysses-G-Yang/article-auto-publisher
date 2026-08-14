(function () {
    'use strict';

    const root = document.getElementById('multi-account-sessions');
    if (!root) return;

    const MAX_TARGETED_POLLS = 12;
    const POLL_INTERVAL_MS = 2500;
    const PUBLIC_ACCOUNT_FIELDS = [
        'account_id', 'display_name', 'masked_platform_user_id', 'status',
        'session_status', 'persist_login', 'last_verified_at',
    ];
    const state = {
        platforms: [],
        platform: null,
        accounts: [],
        requestSequence: 0,
        requestController: null,
        targetedPolls: new Map(),
    };
    const byId = id => document.getElementById(id);
    const sessionLabels = {
        VALID: '有效', UNVERIFIED: '未验证', LOGIN_REQUIRED: '需要登录', ERROR: '异常',
        EXPIRED: '已过期', VERIFYING: '验证中', BUSY: '使用中',
    };
    const statusClasses = {
        VALID: 'text-bg-success', VERIFYING: 'text-bg-info', BUSY: 'text-bg-warning',
        ERROR: 'text-bg-danger', LOGIN_REQUIRED: 'text-bg-danger', EXPIRED: 'text-bg-secondary',
        UNVERIFIED: 'text-bg-secondary', ACTIVE: 'text-bg-primary', DISABLED: 'text-bg-secondary',
    };

    function endpoint(template, key, value) {
        return template.replace(`{${key}}`, encodeURIComponent(value));
    }

    function setMessage(id, message) {
        const element = byId(id);
        element.textContent = message || '';
        element.classList.toggle('d-none', !message);
    }

    function publicAccount(account) {
        return Object.fromEntries(PUBLIC_ACCOUNT_FIELDS.map(field => [field, account?.[field] ?? null]));
    }

    async function jsonResponse(response) {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.message || '请求失败，请稍后重试。');
        return payload;
    }

    function accountLabel(account) {
        return account.masked_platform_user_id
            ? `${account.display_name || '未命名账号'}（${account.masked_platform_user_id}）`
            : account.display_name || '未命名账号';
    }

    function button(text, className, onClick) {
        const element = document.createElement('button');
        element.type = 'button';
        element.className = className;
        element.textContent = text;
        element.addEventListener('click', onClick);
        return element;
    }

    function badge(value) {
        const element = document.createElement('span');
        element.className = `badge ${statusClasses[value] || 'text-bg-secondary'}`;
        element.textContent = sessionLabels[value] || value || '状态未知';
        return element;
    }

    function accountCard(account) {
        const card = document.createElement('article');
        card.className = 'session-account-card';
        card.dataset.accountId = account.account_id;
        const top = document.createElement('div'); top.className = 'session-account-top';
        const identity = document.createElement('div'); identity.className = 'session-account-identity';
        const name = document.createElement('strong'); name.textContent = account.display_name || '未命名账号';
        const masked = document.createElement('span'); masked.textContent = account.masked_platform_user_id || '平台 ID 已隐藏';
        identity.append(name, masked);
        const badges = document.createElement('div'); badges.className = 'session-account-badges';
        badges.append(badge(account.status), badge(account.session_status));
        top.append(identity, badges);

        const meta = document.createElement('div'); meta.className = 'session-account-meta';
        const verified = document.createElement('span');
        verified.textContent = account.last_verified_at ? `最近验证：${account.last_verified_at}` : '尚未验证登录态';
        const policy = document.createElement('div'); policy.className = 'form-check form-switch m-0';
        const policyInput = document.createElement('input');
        policyInput.className = 'form-check-input'; policyInput.type = 'checkbox'; policyInput.role = 'switch';
        policyInput.id = `session-persist-${account.account_id}`; policyInput.checked = Boolean(account.persist_login);
        policyInput.setAttribute('aria-label', `${account.display_name || '账号'}保持登录态`);
        policyInput.addEventListener('change', () => updatePolicy(account, policyInput));
        policy.append(policyInput); meta.append(verified, policy);

        const actions = document.createElement('div'); actions.className = 'session-account-actions';
        if (['UNVERIFIED', 'ERROR', 'EXPIRED'].includes(account.session_status)) {
            actions.append(button('验证现有登录态', 'btn btn-outline-primary btn-sm', () => verifyAccount(account)));
        }
        if (account.session_status === 'LOGIN_REQUIRED') {
            actions.append(button('重新登录', 'btn btn-primary btn-sm', () => loginAccount(account)));
        }
        if (account.session_status === 'VALID') {
            actions.append(button('退出该账号', 'btn btn-outline-danger btn-sm', () => logoutAccount(account)));
        }
        actions.append(button('活动日志', 'btn btn-outline-secondary btn-sm', () => openActivity(account)));
        actions.append(button('删除账号', 'btn btn-outline-danger btn-sm', () => removeAccount(account)));
        card.append(top, meta, actions);
        return card;
    }

    function platformLabel(platformId) {
        return state.platforms.find(item => item.id === platformId)?.display_name || platformId;
    }

    function safeLogoUrl(value) {
        const url = typeof value === 'string' ? value : '';
        return /^\/static\/img\/platforms\/[a-z0-9_-]+\.svg$/.test(url) ? url : '';
    }

    function platformOption(platform) {
        const input = document.createElement('input');
        input.className = 'btn-check';
        input.type = 'radio';
        input.name = 'session-platform';
        input.id = `session-platform-${platform.id}`;
        input.value = platform.id;
        input.autocomplete = 'off';
        input.disabled = !platform.account_enabled;
        input.addEventListener('change', () => selectPlatform(platform.id));

        const label = document.createElement('label');
        label.className = 'session-platform-card';
        label.htmlFor = input.id;
        label.dataset.platformId = platform.id;
        const logo = document.createElement('img');
        logo.className = 'session-platform-logo';
        logo.src = safeLogoUrl(platform.logo_url);
        logo.hidden = !logo.src;
        logo.alt = '';
        logo.setAttribute('aria-hidden', 'true');
        const copy = document.createElement('span');
        copy.className = 'session-platform-copy';
        const name = document.createElement('strong');
        name.textContent = platform.display_name;
        const capability = document.createElement('span');
        capability.textContent = platform.account_enabled
            ? (platform.delivery_enabled ? '账号与投递' : '仅账号管理')
            : '即将接入';
        copy.append(name, capability);
        label.append(logo, copy);
        return [input, label];
    }

    function renderPlatforms() {
        byId('session-platforms').replaceChildren(...state.platforms.flatMap(platformOption));
    }

    async function loadPlatforms() {
        byId('session-platforms-loading').classList.remove('d-none');
        setMessage('session-platforms-error', '');
        try {
            const payload = await jsonResponse(await fetch(root.dataset.platformsUrl, {
                headers: { Accept: 'application/json' },
            }));
            const platforms = Array.isArray(payload.platforms) ? payload.platforms : [];
            state.platforms = platforms
                .filter(platform => platform && typeof platform.id === 'string' && /^[a-z0-9_-]+$/.test(platform.id))
                .map(platform => ({
                    id: platform.id,
                    display_name: String(platform.display_name || platform.id),
                    logo_url: platform.logo_url,
                    account_enabled: platform.account_enabled === true,
                    delivery_enabled: platform.delivery_enabled === true,
                    sort_order: Number(platform.sort_order || 0),
                }))
                .sort((left, right) => left.sort_order - right.sort_order);
            if (state.platforms.length === 0) throw new Error('平台目录为空。');
            renderPlatforms();
        } catch (error) {
            setMessage('session-platforms-error', error.message || '平台目录加载失败。');
        } finally {
            byId('session-platforms-loading').classList.add('d-none');
        }
    }

    function renderAccounts() {
        const list = byId('session-account-list');
        list.replaceChildren(...state.accounts.map(accountCard));
        list.classList.toggle('d-none', state.accounts.length === 0);
        byId('session-accounts-empty').classList.toggle('d-none', state.accounts.length > 0);
    }

    async function loadAccounts(options = {}) {
        if (!state.platform) return;
        const sequence = ++state.requestSequence;
        state.requestController?.abort();
        state.requestController = new AbortController();
        if (!options.silent) {
            byId('session-accounts-placeholder').classList.add('d-none');
            byId('session-accounts-loading').classList.remove('d-none');
        }
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.accountsUrlTemplate, 'platform', state.platform);
            const response = await fetch(url, { headers: { Accept: 'application/json' }, signal: state.requestController.signal });
            const payload = await jsonResponse(response);
            if (sequence !== state.requestSequence || payload.platform !== state.platform) return;
            state.accounts = (Array.isArray(payload.accounts) ? payload.accounts : []).map(publicAccount);
            renderAccounts();
        } catch (error) {
            if (error.name === 'AbortError' || sequence !== state.requestSequence) return;
            setMessage('session-accounts-error', error.message || '账号加载失败。');
        } finally {
            if (sequence === state.requestSequence) byId('session-accounts-loading').classList.add('d-none');
        }
    }

    function selectPlatform(platform) {
        const selectedPlatform = state.platforms.find(item => item.id === platform);
        if (!selectedPlatform?.account_enabled) return;
        state.platform = platform;
        state.accounts = [];
        state.targetedPolls.forEach(timer => clearTimeout(timer));
        state.targetedPolls.clear();
        renderAccounts();
        byId('refresh-account-sessions').disabled = false;
        byId('add-platform-account').disabled = false;
        byId('add-platform-account').textContent = `添加${selectedPlatform.display_name}账号`;
        setMessage('session-login-status', '');
        loadAccounts();
    }

    async function updatePolicy(account, input) {
        const previous = Boolean(account.persist_login);
        input.disabled = true;
        try {
            const url = endpoint(root.dataset.policyUrlTemplate, 'account_id', account.account_id);
            const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ persist_login: input.checked }) });
            const updated = publicAccount(await jsonResponse(response));
            if (updated.account_id !== account.account_id) throw new Error('账号响应不匹配。');
            Object.assign(account, updated); input.checked = Boolean(account.persist_login);
        } catch (error) {
            input.checked = previous;
            setMessage('session-accounts-error', error.message || '会话策略更新失败。');
        } finally { input.disabled = false; }
    }

    async function verifyAccount(account) {
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.verifyUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `正在验证 ${accountLabel(account)} 的现有登录态；不会自动打开扫码。`);
            startTargetedPolling(account.account_id);
        } catch (error) { setMessage('session-accounts-error', error.message || '无法启动登录态验证。'); }
    }

    async function loginAccount(account) {
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.accountLoginUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `正在为 ${accountLabel(account)} 打开扫码登录窗口；将复用该账号的隔离 Profile。`);
            startTargetedPolling(account.account_id);
        } catch (error) { setMessage('session-accounts-error', error.message || '无法启动账号重新登录。'); }
    }

    function startTargetedPolling(accountId) {
        state.targetedPolls.forEach(timer => clearTimeout(timer));
        state.targetedPolls.clear();
        let attempts = 0;
        const poll = async () => {
            attempts += 1;
            await loadAccounts({ silent: true });
            const account = state.accounts.find(item => item.account_id === accountId);
            if (!account || !['VERIFYING', 'BUSY', 'UNVERIFIED'].includes(account.session_status) || attempts >= MAX_TARGETED_POLLS) {
                state.targetedPolls.delete(accountId);
                setMessage('session-login-status', attempts >= MAX_TARGETED_POLLS ? '验证轮询已停止，请稍后手动刷新。' : '账号状态已更新。');
                return;
            }
            state.targetedPolls.set(accountId, setTimeout(poll, POLL_INTERVAL_MS));
        };
        state.targetedPolls.set(accountId, setTimeout(poll, POLL_INTERVAL_MS));
    }

    async function addPlatformAccount() {
        if (!state.platform) return;
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.loginUrlTemplate, 'platform', state.platform);
            const payload = await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `已创建${platformLabel(state.platform)}隔离浏览器 Profile，请在打开的窗口中完成交互登录。`);
            const targetAccountId = payload.account_id || payload.account?.account_id;
            if (targetAccountId) startTargetedPolling(targetAccountId);
        } catch (error) { setMessage('session-accounts-error', error.message || '无法创建隔离账号登录。'); }
    }

    async function logoutAccount(account) {
        if (!window.confirm(`确定退出 ${accountLabel(account)} 吗？这只影响该账号会话。`)) return;
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.logoutUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            await loadAccounts();
        } catch (error) { setMessage('session-accounts-error', error.message || '账号退出失败。'); }
    }

    async function removeAccount(account) {
        const label = accountLabel(account);
        if (!window.confirm(`确定永久删除账号「${label}」吗？\n\n将删除该账号的隔离浏览器 Profile 及其活动记录；拥有投递历史的账号会被拒绝删除。此操作不可撤销。`)) return;
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.deleteAccountUrlTemplate, 'account_id', account.account_id);
            const payload = await jsonResponse(await fetch(url, { method: 'DELETE', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `已删除账号「${label}」。`);
            await loadAccounts();
        } catch (error) {
            setMessage('session-accounts-error', error.message || '账号删除失败。');
        }
    }

    function activityItem(event) {
        const item = document.createElement('li'); item.className = 'activity-item';
        const dot = document.createElement('span'); dot.className = 'activity-dot'; dot.setAttribute('aria-hidden', 'true');
        const copy = document.createElement('div'); copy.className = 'activity-copy';
        const title = document.createElement('strong'); title.textContent = event.event_type || event.action || '账号事件';
        const message = document.createElement('p'); message.textContent = event.message || event.status || '状态已更新';
        const time = document.createElement('time'); time.textContent = event.created_at || event.timestamp || '时间未知';
        copy.append(title, message, time); item.append(dot, copy); return item;
    }

    async function openActivity(account) {
        bootstrap.Offcanvas.getOrCreateInstance(byId('account-activity-drawer')).show();
        byId('account-activity-account').textContent = accountLabel(account);
        byId('account-activity-list').replaceChildren();
        byId('account-activity-loading').classList.remove('d-none');
        byId('account-activity-empty').classList.add('d-none');
        setMessage('account-activity-error', '');
        try {
            const url = endpoint(root.dataset.activityUrlTemplate, 'account_id', account.account_id);
            const payload = await jsonResponse(await fetch(url, { headers: { Accept: 'application/json' } }));
            const events = Array.isArray(payload) ? payload : (payload.activities || payload.events || []);
            byId('account-activity-list').replaceChildren(...events.map(activityItem));
            byId('account-activity-empty').classList.toggle('d-none', events.length > 0);
        } catch (error) { setMessage('account-activity-error', error.message || '活动日志加载失败。'); }
        finally { byId('account-activity-loading').classList.add('d-none'); }
    }

    loadPlatforms();
    byId('refresh-account-sessions').addEventListener('click', () => loadAccounts());
    byId('add-platform-account').addEventListener('click', addPlatformAccount);
})();
