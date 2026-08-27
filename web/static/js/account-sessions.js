(function () {
    'use strict';

    const root = document.getElementById('multi-account-sessions');
    if (!root) return;

    const MAX_TARGETED_POLLS = 12;
    const POLL_INTERVAL_MS = 2500;
    const SMZDM_PLATFORM = 'smzdm';
    const PUBLIC_ACCOUNT_FIELDS = [
        'account_id', 'display_name', 'masked_platform_user_id', 'status',
        'session_status', 'persist_login', 'last_verified_at',
        'login_in_progress', 'platform_login_in_progress', 'login_error_code',
    ];
    const state = {
        platforms: [],
        platform: null,
        accounts: [],
        requestSequence: 0,
        requestController: null,
        targetedPolls: new Map(),
        pollingAccountId: null,
        pollingGeneration: 0,
        loginInFlight: false,
        loginTargetAccountId: null,
        showArchived: false,
    };
    const byId = id => document.getElementById(id);
    const sessionLabels = {
        VALID: '有效', UNVERIFIED: '未验证', LOGIN_REQUIRED: '需要登录', ERROR: '异常',
        EXPIRED: '已过期', VERIFYING: '验证中', BUSY: '使用中',
        ACTIVE: '使用中', ARCHIVED: '已归档',
    };
    const statusClasses = {
        VALID: 'text-bg-success', VERIFYING: 'text-bg-info', BUSY: 'text-bg-warning',
        ERROR: 'text-bg-danger', LOGIN_REQUIRED: 'text-bg-danger', EXPIRED: 'text-bg-secondary',
        UNVERIFIED: 'text-bg-secondary', ACTIVE: 'text-bg-primary', ARCHIVED: 'text-bg-secondary', DISABLED: 'text-bg-secondary',
    };

    const loginErrorMessages = {
        RATE_LIMITED: '什么值得买提示“操作过于频繁”。系统已停止本次状态查询，不会自动重试或刷新平台；请关闭整个 Chrome 窗口，稍后再人工登录。',
        LOGIN_REQUIRED: '登录未完成，请在原生 Chrome 窗口中人工完成验证，完成后关闭整个 Chrome 窗口。',
        BROWSER_CONTEXT_CLOSED: '原生 Chrome 窗口已关闭，登录尚未完成。',
        LOGIN_IN_PROGRESS: '什么值得买人工登录窗口正在使用中，请勿重复点击。',
        SMZDM_NATIVE_CHROME_NOT_FOUND: '未找到可用的原生 Chrome。请先安装或修复 Chrome，再重新发起什么值得买登录；系统不会改用自动化浏览器。',
        LOGIN_WINDOW_STILL_OPEN: '原生 Chrome 登录窗口仍未关闭。请完成登录后关闭整个 Chrome 窗口，本页会继续查询本机账号状态。',
        SMZDM_PROFILE_NOT_RELEASED: '该账号的浏览器 Profile 仍被 Chrome 占用。请关闭该账号的整个 Chrome 窗口，等待几秒后再验证，不要重复点击登录。',
        SMZDM_NATIVE_LOGIN_FAILED: '原生 Chrome 登录窗口启动失败。请确认 Chrome 可以正常打开、该账号旧窗口已经关闭，然后重试一次。',
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

    function updateAccountContext(accountCount = null) {
        const platform = state.platforms.find(item => item.id === state.platform);
        const platformSummary = byId('session-platform-summary');
        const accountSummary = byId('session-account-summary');
        if (platformSummary) {
            platformSummary.textContent = platform ? `${platform.display_name}账号` : '尚未选择平台';
        }
        if (accountSummary) {
            if (!platform) {
                accountSummary.textContent = '选择平台后才会读取账号信息';
            } else if (accountCount === null) {
                accountSummary.textContent = '正在读取该平台的账号信息';
            } else if (accountCount === 0) {
                accountSummary.textContent = '暂无可用账号，可在下方创建隔离登录态';
            } else {
                accountSummary.textContent = `已读取 ${accountCount} 个账号，请查看状态后选择操作`;
            }
        }
    }

    async function jsonResponse(response) {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(payload.message || '请求失败，请稍后重试。');
            error.code = payload.error || '';
            throw error;
        }
        return payload;
    }

    function smzdmSelected() {
        return state.platform === SMZDM_PLATFORM;
    }

    function updateSmzdmLoginGuidance() {
        const guidance = byId('smzdm-login-guidance');
        if (!guidance) return;
        guidance.classList.toggle('d-none', !smzdmSelected());
    }

    function smzdmManualLoginMessage(accountLabelText) {
        const target = accountLabelText ? ` ${accountLabelText}` : '';
        return `已在原生 Chrome 中打开${target}的人工登录窗口。请人工完成验证，完成后关闭整个 Chrome 窗口；系统不会代拖滑块，也不会自动刷新平台。本页每 2.5 秒仅查询本机账号状态。`;
    }

    function loginErrorMessage(code) {
        return loginErrorMessages[code] || (code ? `登录失败（${code}）。` : '登录状态未完成。');
    }

    function setLoginInFlight(value, accountId = null) {
        const active = Boolean(value) && smzdmSelected();
        state.loginInFlight = active;
        state.loginTargetAccountId = active ? accountId : null;
        byId('add-platform-account').disabled = active;
    }

    function isCurrentContext(platform, generation) {
        return state.platform === platform && state.pollingGeneration === generation;
    }

    function loginMetadataAvailable(account) {
        return typeof account?.login_in_progress === 'boolean';
    }

    function syncSmzdmActivity() {
        if (!smzdmSelected()) return;
        const active = state.accounts.find(account => account.login_in_progress === true);
        if (active) {
            const targetChanged = state.loginTargetAccountId !== active.account_id;
            if (!state.loginInFlight || targetChanged) setLoginInFlight(true, active.account_id);
            if (state.pollingAccountId !== active.account_id) startTargetedPolling(active.account_id);
            return;
        }
        // A current poller owns terminal handling.  Do not race it by clearing
        // state here; the poller will use the same response and generation.
        if (state.pollingAccountId !== null) return;
        if (state.loginInFlight) {
            const target = state.accounts.find(
                account => account.account_id === state.loginTargetAccountId,
            );
            const metadataAvailable = state.accounts.some(loginMetadataAvailable);
            // Old services omit the optional fields.  If no target was returned,
            // release the local button lock; otherwise the bounded poller owns it.
            if (metadataAvailable || !state.loginTargetAccountId) {
                setLoginInFlight(false);
                if (target) reportLoginOutcome(target);
            }
        }
    }

    function loginRequestErrorMessage(error, fallback) {
        if (smzdmSelected()) return loginErrorMessage(error?.code) || fallback;
        return error?.message || fallback;
    }

    function reportLoginOutcome(account) {
        if (!account) {
            setMessage('session-accounts-error', '登录账号已不存在，未自动重试。');
            return;
        }
        if (account.login_error_code) {
            setMessage('session-accounts-error', loginErrorMessage(account.login_error_code));
            return;
        }
        if (account.session_status === 'VALID') {
            setMessage('session-login-status', '登录态验证成功。');
            return;
        }
        if (['LOGIN_REQUIRED', 'ERROR', 'EXPIRED'].includes(account.session_status)) {
            setMessage('session-accounts-error', loginErrorMessage(account.session_status));
            return;
        }
        setMessage('session-login-status', '登录状态已更新，请查看账号卡片。');
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

    function accountStateDescription(account, archived) {
        if (archived) return '已归档，历史记录仍保留';
        const descriptions = {
            VALID: '登录态有效，可用于投递',
            LOGIN_REQUIRED: '需要重新登录后才能投递',
            EXPIRED: '登录态已过期，请重新验证',
            ERROR: '验证出现异常，可查看活动日志',
            VERIFYING: '正在验证登录态，请稍候',
            BUSY: '账号正在处理中，请稍候',
            UNVERIFIED: '尚未验证当前登录态',
        };
        return descriptions[account.session_status] || '状态待确认';
    }

    function accountCard(account) {
        const card = document.createElement('article');
        card.className = 'session-account-card';
        const archived = account.status === 'ARCHIVED';
        card.classList.toggle('is-archived', archived);
        card.dataset.accountId = account.account_id;
        card.dataset.sessionStatus = account.session_status || 'UNKNOWN';
        const top = document.createElement('div'); top.className = 'session-account-top';
        const identity = document.createElement('div'); identity.className = 'session-account-identity';
        const name = document.createElement('strong'); name.textContent = account.display_name || '未命名账号';
        const masked = document.createElement('span'); masked.textContent = account.masked_platform_user_id || '平台 ID 已隐藏';
        const stateDescription = document.createElement('span');
        stateDescription.className = 'session-account-state';
        stateDescription.textContent = accountStateDescription(account, archived);
        identity.append(name, masked, stateDescription);
        const badges = document.createElement('div'); badges.className = 'session-account-badges';
        badges.append(badge(account.status), badge(account.session_status));
        top.append(identity, badges);

        const meta = document.createElement('div'); meta.className = 'session-account-meta';
        const verified = document.createElement('span');
        verified.textContent = account.last_verified_at ? `最近验证：${account.last_verified_at}` : '尚未验证登录态';
        if (account.login_error_code) {
            const loginError = document.createElement('span');
            loginError.className = 'text-danger';
            loginError.textContent = loginErrorMessage(account.login_error_code);
            verified.append(' ', loginError);
        }
        const policy = document.createElement('div'); policy.className = 'form-check form-switch m-0';
        const policyInput = document.createElement('input');
        policyInput.className = 'form-check-input'; policyInput.type = 'checkbox'; policyInput.role = 'switch';
        policyInput.id = `session-persist-${account.account_id}`; policyInput.checked = Boolean(account.persist_login);
        policyInput.disabled = archived;
        policyInput.setAttribute('aria-label', `${account.display_name || '账号'}保持登录态`);
        policyInput.addEventListener('change', () => updatePolicy(account, policyInput));
        policy.append(policyInput); meta.append(verified, policy);

        const actions = document.createElement('div'); actions.className = 'session-account-actions';
        if (!archived) {
            const loginBusy = smzdmSelected() && (state.loginInFlight || account.login_in_progress);
            if (loginBusy) {
                actions.append(button('登录处理中', 'btn btn-outline-secondary btn-sm', () => {}));
            } else if (['UNVERIFIED', 'ERROR', 'EXPIRED'].includes(account.session_status)) {
                actions.append(button('验证现有登录态', 'btn btn-outline-primary btn-sm', () => verifyAccount(account)));
                if (smzdmSelected()) {
                    actions.append(button('重新登录', 'btn btn-primary btn-sm', () => loginAccount(account)));
                }
            }
            if (account.session_status === 'LOGIN_REQUIRED') {
                actions.append(button('重新登录', 'btn btn-primary btn-sm', () => loginAccount(account)));
            }
            if (account.session_status === 'VALID') {
                actions.append(button('退出该账号', 'btn btn-outline-danger btn-sm', () => logoutAccount(account)));
            }
        }
        actions.append(button('活动日志', 'btn btn-outline-secondary btn-sm', () => openActivity(account)));
        if (archived) {
            actions.append(button('恢复账号', 'btn btn-outline-primary btn-sm', () => restoreAccount(account)));
            actions.append(button('退出并清除登录态', 'btn btn-outline-danger btn-sm', () => clearLoginState(account)));
            actions.append(button('永久删除', 'btn btn-outline-danger btn-sm', () => removeAccount(account)));
        } else {
            actions.append(button('归档账号', 'btn btn-outline-secondary btn-sm', () => archiveAccount(account)));
        }
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
            const requestedPlatform = new URLSearchParams(window.location.search).get('platform');
            const requested = state.platforms.find(item => item.id === requestedPlatform && item.account_enabled);
            if (requested) {
                const input = byId(`session-platform-${requested.id}`);
                if (input) input.click();
                else selectPlatform(requested.id);
            }
        } catch (error) {
            setMessage('session-platforms-error', error.message || '平台目录加载失败。');
        } finally {
            byId('session-platforms-loading').classList.add('d-none');
        }
    }

    function renderAccounts() {
        const list = byId('session-account-list');
        const archivedCount = state.accounts.filter(account => account.status === 'ARCHIVED').length;
        const visibleAccounts = state.accounts.filter(account => state.showArchived || account.status !== 'ARCHIVED');
        updateAccountContext(visibleAccounts.length);
        list.replaceChildren(...visibleAccounts.map(accountCard));
        list.classList.toggle('d-none', visibleAccounts.length === 0);
        byId('session-accounts-empty').classList.toggle('d-none', visibleAccounts.length > 0);
        byId('archived-account-count').textContent = archivedCount ? `已归档 ${archivedCount} 个` : '';
    }

    async function loadAccounts(options = {}) {
        if (!state.platform) return;
        updateAccountContext(null);
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
            syncSmzdmActivity();
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
        updateAccountContext(null);
        state.pollingGeneration += 1;
        state.targetedPolls.forEach(timer => clearTimeout(timer));
        state.targetedPolls.clear();
        state.pollingAccountId = null;
        setLoginInFlight(false);
        renderAccounts();
        byId('refresh-account-sessions').disabled = false;
        byId('add-platform-account').disabled = false;
        byId('add-platform-account').textContent = `添加${selectedPlatform.display_name}账号`;
        setMessage('session-login-status', '');
        updateSmzdmLoginGuidance();
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
        if (smzdmSelected() && (state.loginInFlight || account.login_in_progress)) {
            setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
            return;
        }
        const platformAtStart = state.platform;
        const generationAtStart = state.pollingGeneration;
        setMessage('session-accounts-error', '');
        if (smzdmSelected()) setLoginInFlight(true, account.account_id);
        try {
            const url = endpoint(root.dataset.verifyUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            setMessage('session-login-status', `正在验证 ${accountLabel(account)} 的现有登录态；不会自动打开扫码。`);
            startTargetedPolling(account.account_id);
        } catch (error) {
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            if (platformAtStart === SMZDM_PLATFORM && error.code === 'LOGIN_IN_PROGRESS') {
                setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
                await loadAccounts({ silent: true });
            } else {
                if (platformAtStart === SMZDM_PLATFORM) setLoginInFlight(false);
                setMessage('session-accounts-error', loginRequestErrorMessage(error, '无法启动登录态验证。'));
            }
        }
    }

    async function loginAccount(account) {
        if (smzdmSelected() && (state.loginInFlight || account.login_in_progress)) {
            setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
            return;
        }
        const platformAtStart = state.platform;
        const generationAtStart = state.pollingGeneration;
        setMessage('session-accounts-error', '');
        if (smzdmSelected()) setLoginInFlight(true, account.account_id);
        try {
            const url = endpoint(root.dataset.accountLoginUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            setMessage('session-login-status', platformAtStart === SMZDM_PLATFORM
                ? smzdmManualLoginMessage(accountLabel(account))
                : `正在为 ${accountLabel(account)} 打开人工登录窗口；将复用该账号的隔离 Profile。`);
            startTargetedPolling(account.account_id);
        } catch (error) {
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            if (platformAtStart === SMZDM_PLATFORM && error.code === 'LOGIN_IN_PROGRESS') {
                setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
                await loadAccounts({ silent: true });
            } else {
                if (platformAtStart === SMZDM_PLATFORM) setLoginInFlight(false);
                setMessage('session-accounts-error', loginRequestErrorMessage(error, '无法启动账号重新登录。'));
            }
        }
    }

    function startTargetedPolling(accountId) {
        const platform = state.platform;
        if (!platform || !accountId) return;
        if (state.pollingAccountId === accountId) return;
        const generation = ++state.pollingGeneration;
        state.targetedPolls.forEach(timer => clearTimeout(timer));
        state.targetedPolls.clear();
        state.pollingAccountId = accountId;
        let attempts = 0;
        const poll = async () => {
            if (generation !== state.pollingGeneration || state.platform !== platform) return;
            state.targetedPolls.delete(accountId);
            attempts += 1;
            await loadAccounts({ silent: true });
            if (generation !== state.pollingGeneration || state.platform !== platform) return;
            const account = state.accounts.find(item => item.account_id === accountId);
            const terminal = !account || (platform === SMZDM_PLATFORM
                ? (loginMetadataAvailable(account)
                    ? account.login_in_progress === false
                    : (!['VERIFYING', 'BUSY'].includes(account.session_status)
                        || attempts >= MAX_TARGETED_POLLS))
                : !['VERIFYING', 'BUSY', 'UNVERIFIED'].includes(account.session_status));
            if (terminal) {
                state.targetedPolls.delete(accountId);
                state.pollingAccountId = null;
                if (platform === SMZDM_PLATFORM) {
                    setLoginInFlight(false);
                    reportLoginOutcome(account);
                } else {
                    setMessage('session-login-status', '账号状态已更新。');
                }
                return;
            }
            if (platform !== SMZDM_PLATFORM && attempts >= MAX_TARGETED_POLLS) {
                state.targetedPolls.delete(accountId);
                state.pollingAccountId = null;
                setMessage('session-login-status', '验证轮询已停止，请稍后手动刷新。');
                return;
            }
            if (platform === SMZDM_PLATFORM && !loginMetadataAvailable(account)
                && attempts >= MAX_TARGETED_POLLS) {
                state.targetedPolls.delete(accountId);
                state.pollingAccountId = null;
                setLoginInFlight(false);
                setMessage('session-login-status', '登录状态轮询已停止，请稍后手动刷新。');
                return;
            }
            state.targetedPolls.set(accountId, setTimeout(poll, POLL_INTERVAL_MS));
        };
        state.targetedPolls.set(accountId, setTimeout(poll, POLL_INTERVAL_MS));
    }

    async function addPlatformAccount() {
        if (!state.platform) return;
        if (smzdmSelected() && state.loginInFlight) {
            setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
            return;
        }
        const platformAtStart = state.platform;
        const generationAtStart = state.pollingGeneration;
        setMessage('session-accounts-error', '');
        if (smzdmSelected()) setLoginInFlight(true);
        try {
            const url = endpoint(root.dataset.loginUrlTemplate, 'platform', state.platform);
            const payload = await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            setMessage('session-login-status', platformAtStart === SMZDM_PLATFORM
                ? smzdmManualLoginMessage('新账号')
                : `已创建${platformLabel(platformAtStart)}隔离浏览器 Profile，请在打开的人工登录窗口中完成交互登录。`);
            const targetAccountId = payload.account_id || payload.account?.account_id;
            if (targetAccountId) startTargetedPolling(targetAccountId);
        } catch (error) {
            if (!isCurrentContext(platformAtStart, generationAtStart)) return;
            if (platformAtStart === SMZDM_PLATFORM && error.code === 'LOGIN_IN_PROGRESS') {
                setMessage('session-login-status', '什么值得买人工登录窗口正在使用中，请勿重复点击。');
                await loadAccounts({ silent: true });
            } else {
                if (platformAtStart === SMZDM_PLATFORM) setLoginInFlight(false);
                setMessage('session-accounts-error', loginRequestErrorMessage(error, '无法创建隔离账号登录。'));
            }
        }
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

    async function archiveAccount(account) {
        if (!window.confirm(`确定归档 ${accountLabel(account)} 吗？\n\n归档后会从默认列表和投递选择器隐藏，但账号身份、投递记录和活动日志都会保留。`)) return;
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.archiveAccountUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `已归档账号「${accountLabel(account)}」，历史记录仍保留。`);
            await loadAccounts();
        } catch (error) { setMessage('session-accounts-error', error.message || '账号归档失败。'); }
    }

    async function restoreAccount(account) {
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.restoreAccountUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, { method: 'POST', headers: { Accept: 'application/json' } }));
            setMessage('session-login-status', `已恢复账号「${accountLabel(account)}」。`);
            await loadAccounts();
        } catch (error) { setMessage('session-accounts-error', error.message || '账号恢复失败。'); }
    }

    async function clearLoginState(account) {
        const label = accountLabel(account);
        if (!window.confirm(`确定退出并清除账号「${label}」的登录态吗？\n\n这会清空该账号的浏览器登录数据和隔离 Profile，但账号身份、投递记录和活动日志仍会保留。之后需要重新登录。`)) return;
        setMessage('session-accounts-error', '');
        try {
            const url = endpoint(root.dataset.clearLoginStateUrlTemplate, 'account_id', account.account_id);
            await jsonResponse(await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ confirmation: 'CLEAR_LOGIN_STATE' }),
            }));
            setMessage('session-login-status', `已清除账号「${label}」的登录态；历史记录未删除。`);
            await loadAccounts();
        } catch (error) { setMessage('session-accounts-error', error.message || '登录态清理失败。'); }
    }

    async function removeAccount(account) {
        const label = accountLabel(account);
        if (!window.confirm(`确定永久删除账号「${label}」吗？\n\n只有从未产生投递记录的账号才能删除；拥有投递历史的账号会被拒绝删除。此操作不可撤销。`)) return;
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
    byId('show-archived-accounts').addEventListener('change', event => {
        state.showArchived = event.target.checked === true;
        renderAccounts();
    });
})();
