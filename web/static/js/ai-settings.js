(function () {
    'use strict';

    const root = document.getElementById('ai-settings');
    if (!root) return;

    const byId = id => document.getElementById(id);
    const state = { settings: null, busy: false };
    const keySourceLabels = {
        runtime: '当前页面设置',
        environment: '环境变量',
        missing: '未配置',
    };

    async function jsonResponse(response) {
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = {}; }
        if (!response.ok) {
            throw new Error(payload.message || '请求未完成，请稍后重试');
        }
        return payload;
    }

    function setMessage(text = '', kind = 'info') {
        const message = byId('ai-settings-message');
        message.textContent = text;
        message.className = `alert alert-${kind}${text ? '' : ' d-none'}`;
    }

    function setBusy(busy, activeButton = null) {
        state.busy = busy;
        root.setAttribute('aria-busy', String(busy));
        ['save-ai-settings', 'test-ai-connection', 'clear-ai-key'].forEach(id => {
            const button = byId(id);
            button.disabled = busy || (id === 'clear-ai-key' && !state.settings?.api_key_configured)
                || (id === 'clear-ai-key' && state.settings?.api_key_source === 'environment');
        });
        if (activeButton) {
            activeButton.dataset.defaultLabel ||= activeButton.textContent.trim();
            activeButton.textContent = busy ? '正在处理…' : activeButton.dataset.defaultLabel;
        }
    }

    function render(settings) {
        state.settings = settings;
        byId('ai-enabled').checked = Boolean(settings.enabled);
        byId('ai-base-url').value = settings.base_url || '';
        byId('ai-model').value = settings.model || '';
        byId('ai-enabled-status').textContent = settings.enabled ? '已启用' : '已停用';
        byId('ai-key-status').textContent = settings.api_key_configured ? '已配置' : '未配置';
        byId('ai-key-source').textContent = keySourceLabels[settings.api_key_source] || '未知';

        const badge = byId('ai-config-badge');
        const ready = Boolean(settings.enabled && settings.api_key_configured);
        badge.textContent = ready ? '可生成建议' : settings.api_key_configured ? '已配置，尚未启用' : '缺少 API Key';
        badge.className = `badge ${ready ? 'text-bg-success' : 'text-bg-warning'}`;
        byId('clear-ai-key').disabled = state.busy || !settings.api_key_configured
            || settings.api_key_source === 'environment';
        byId('clear-ai-key').title = settings.api_key_source === 'environment'
            ? '环境变量中的 Key 不能从页面清除' : '';
    }

    function validHttpsBaseUrl(value) {
        try {
            const url = new URL(value);
            return url.protocol === 'https:' && !url.username && !url.password
                && !url.search && !url.hash;
        } catch (_) { return false; }
    }

    function validateForm() {
        const form = byId('ai-settings-form');
        const baseUrl = byId('ai-base-url');
        baseUrl.setCustomValidity(validHttpsBaseUrl(baseUrl.value.trim()) ? '' : 'INVALID_HTTPS_URL');
        form.querySelectorAll('input[required]').forEach(input => {
            input.setAttribute('aria-invalid', String(!input.validity.valid));
        });
        if (!form.checkValidity()) {
            form.classList.add('was-validated');
            form.querySelector(':invalid')?.focus();
            return false;
        }
        form.classList.remove('was-validated');
        form.querySelectorAll('input[required]').forEach(input => {
            input.setAttribute('aria-invalid', 'false');
        });
        return true;
    }

    async function loadSettings() {
        try {
            const settings = await jsonResponse(await fetch(root.dataset.settingsUrl, {
                headers: { Accept: 'application/json' }, cache: 'no-store',
                credentials: 'same-origin',
            }));
            render(settings);
            byId('ai-settings-loading').classList.add('d-none');
            byId('ai-settings-workspace').classList.remove('d-none');
        } catch (error) {
            byId('ai-settings-loading').classList.add('d-none');
            const fatal = byId('ai-settings-fatal');
            fatal.textContent = `AI 配置加载失败：${error.message}`;
            fatal.classList.remove('d-none');
        }
    }

    function requestHeaders() {
        return {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-ArticleOps-AI-Settings': root.dataset.csrfToken,
        };
    }

    function currentFormPayload() {
        const payload = {
            enabled: byId('ai-enabled').checked,
            base_url: byId('ai-base-url').value.trim(),
            model: byId('ai-model').value.trim(),
            clear_api_key: false,
        };
        const apiKey = byId('ai-api-key').value;
        if (apiKey) payload.api_key = apiKey;
        return payload;
    }

    async function persistCurrentForm() {
        const settings = await jsonResponse(await fetch(root.dataset.settingsUrl, {
            method: 'PUT', headers: requestHeaders(),
            body: JSON.stringify(currentFormPayload()),
            cache: 'no-store', credentials: 'same-origin',
        }));
        byId('ai-api-key').value = '';
        render(settings);
        return settings;
    }

    async function saveSettings(event) {
        event.preventDefault();
        if (state.busy || !validateForm()) return;
        const button = byId('save-ai-settings');
        setMessage('');
        setBusy(true, button);
        try {
            await persistCurrentForm();
            setMessage('设置已应用到当前服务进程。', 'success');
        } catch (error) { setMessage(`保存失败：${error.message}`, 'danger'); }
        finally { setBusy(false, button); }
    }

    async function clearRuntimeKey() {
        if (state.busy || !state.settings?.api_key_configured
            || state.settings.api_key_source === 'environment') return;
        const button = byId('clear-ai-key');
        setMessage('');
        setBusy(true, button);
        try {
            const settings = await jsonResponse(await fetch(root.dataset.settingsUrl, {
                method: 'PUT', headers: requestHeaders(), cache: 'no-store',
                credentials: 'same-origin',
                body: JSON.stringify({
                    enabled: state.settings.enabled,
                    base_url: state.settings.base_url,
                    model: state.settings.model,
                    clear_api_key: true,
                }),
            }));
            byId('ai-api-key').value = '';
            render(settings);
            setMessage('页面输入的 API Key 已从当前服务进程清除。', 'success');
        } catch (error) { setMessage(`清除失败：${error.message}`, 'danger'); }
        finally { setBusy(false, button); }
    }

    async function testConnection() {
        if (state.busy || !validateForm()) return;
        const button = byId('test-ai-connection');
        setMessage('');
        setBusy(true, button);
        try {
            await persistCurrentForm();
            const result = await jsonResponse(await fetch(root.dataset.testUrl, {
                method: 'POST', headers: requestHeaders(), body: '{}',
                cache: 'no-store', credentials: 'same-origin',
            }));
            setMessage(`连接成功：模型 ${result.model} 可用。`, 'success');
        } catch (error) { setMessage(`连接测试失败：${error.message}`, 'danger'); }
        finally { setBusy(false, button); }
    }

    function toggleKeyVisibility() {
        const input = byId('ai-api-key');
        const button = byId('toggle-ai-key');
        const reveal = input.type === 'password';
        input.type = reveal ? 'text' : 'password';
        button.setAttribute('aria-pressed', String(reveal));
        button.setAttribute('aria-label', reveal ? '隐藏本次输入的 API Key' : '显示本次输入的 API Key');
        input.focus();
    }

    byId('ai-settings-form').addEventListener('submit', saveSettings);
    byId('clear-ai-key').addEventListener('click', clearRuntimeKey);
    byId('test-ai-connection').addEventListener('click', testConnection);
    byId('toggle-ai-key').addEventListener('click', toggleKeyVisibility);
    loadSettings();
})();
