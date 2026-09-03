(function () {
    'use strict';

    const root = document.getElementById('ai-settings');
    if (!root) return;

    const byId = id => document.getElementById(id);
    const MAX_VISIBLE_MODELS = 100;
    const state = {
        settings: null,
        busy: false,
        models: [],
        modelRequestGeneration: 0,
    };
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

    function requestHeaders() {
        return {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-ArticleOps-AI-Settings': root.dataset.csrfToken,
        };
    }

    function setMessage(text = '', kind = 'info') {
        const message = byId('ai-settings-message');
        message.textContent = text;
        message.className = `alert alert-${kind}${text ? '' : ' d-none'}`;
    }

    function setModelStatus(text = '', kind = 'neutral') {
        const status = byId('ai-model-status');
        status.textContent = text;
        status.dataset.kind = kind;
    }

    function setBusy(busy, activeButton = null, busyLabel = '正在处理…') {
        state.busy = busy;
        root.setAttribute('aria-busy', String(busy));
        ['save-ai-settings', 'test-ai-connection', 'load-ai-models', 'clear-ai-key'].forEach(id => {
            const button = byId(id);
            button.disabled = busy || (id === 'clear-ai-key' && !state.settings?.api_key_configured)
                || (id === 'clear-ai-key' && state.settings?.api_key_source === 'environment');
        });
        if (activeButton) {
            activeButton.dataset.defaultLabel ||= activeButton.textContent.trim();
            activeButton.textContent = busy ? busyLabel : activeButton.dataset.defaultLabel;
        }
    }

    function clearModelCatalog(message = '') {
        state.modelRequestGeneration += 1;
        state.models = [];
        byId('ai-model-catalog').replaceChildren();
        byId('ai-model-catalog').setAttribute('aria-busy', 'false');
        byId('ai-model-catalog-wrap').classList.add('d-none');
        byId('ai-model-empty').classList.add('d-none');
        byId('ai-model-count').textContent = '0 个';
        byId('ai-model').setAttribute('aria-expanded', 'false');
        setModelStatus(message, message ? 'notice' : 'neutral');
    }

    function selectModel(modelId) {
        const input = byId('ai-model');
        input.value = modelId;
        input.setAttribute('aria-invalid', 'false');
        renderModelCatalog();
        setModelStatus(`已选择模型：${modelId}`, 'success');
        input.focus();
    }

    function focusAdjacentModel(currentButton, direction) {
        const buttons = Array.from(byId('ai-model-catalog').querySelectorAll('button:not([disabled])'));
        const currentIndex = buttons.indexOf(currentButton);
        if (currentIndex < 0) return;
        const nextIndex = (currentIndex + direction + buttons.length) % buttons.length;
        buttons[nextIndex]?.focus();
    }

    function createModelOption(modelId, selectedModel) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ai-model-option';
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', String(modelId === selectedModel));
        button.textContent = modelId;
        button.addEventListener('click', () => selectModel(modelId));
        button.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
                event.preventDefault();
                focusAdjacentModel(button, 1);
            } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
                event.preventDefault();
                focusAdjacentModel(button, -1);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                byId('ai-model').focus();
            }
        });
        return button;
    }

    function renderModelCatalog(query = '') {
        const catalog = byId('ai-model-catalog');
        const catalogWrap = byId('ai-model-catalog-wrap');
        const empty = byId('ai-model-empty');
        const selectedModel = byId('ai-model').value.trim();
        const normalizedQuery = query.trim().toLocaleLowerCase();
        const matches = state.models.filter(modelId => (
            !normalizedQuery || modelId.toLocaleLowerCase().includes(normalizedQuery)
        ));
        const visibleModels = matches.slice(0, MAX_VISIBLE_MODELS);

        catalog.replaceChildren();
        visibleModels.forEach(modelId => catalog.appendChild(createModelOption(modelId, selectedModel)));
        catalogWrap.classList.remove('d-none');
        empty.classList.toggle('d-none', visibleModels.length > 0);
        catalog.classList.toggle('d-none', visibleModels.length === 0);
        byId('ai-model').setAttribute('aria-expanded', String(visibleModels.length > 0));

        const suffix = matches.length > MAX_VISIBLE_MODELS ? `，仅显示前 ${MAX_VISIBLE_MODELS} 个` : '';
        byId('ai-model-count').textContent = `显示 ${visibleModels.length} / 共 ${state.models.length} 个${suffix}`;
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
        badge.textContent = ready ? '可生成建议' : settings.api_key_configured ? '已配置，尚未启用' : '缺少访问密钥';
        badge.className = `badge ${ready ? 'text-bg-success' : 'text-bg-warning'}`;
        byId('clear-ai-key').disabled = state.busy || !settings.api_key_configured
            || settings.api_key_source === 'environment';
        byId('clear-ai-key').title = settings.api_key_source === 'environment'
            ? '环境变量中的密钥不能从页面清除' : '';
    }

    function validHttpsBaseUrl(value) {
        try {
            const url = new URL(value);
            return url.protocol === 'https:' && !url.username && !url.password
                && !url.search && !url.hash;
        } catch (_) { return false; }
    }

    function validateBaseUrl() {
        const baseUrl = byId('ai-base-url');
        baseUrl.setCustomValidity(validHttpsBaseUrl(baseUrl.value.trim()) ? '' : 'INVALID_HTTPS_URL');
        baseUrl.setAttribute('aria-invalid', String(!baseUrl.validity.valid));
        if (!baseUrl.validity.valid) {
            byId('ai-settings-form').classList.add('was-validated');
            baseUrl.focus();
            return false;
        }
        return true;
    }

    function validateForm() {
        const form = byId('ai-settings-form');
        validateBaseUrl();
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
                headers: { Accept: 'application/json' },
                cache: 'no-store',
                credentials: 'same-origin',
            }));
            render(settings);
            byId('ai-settings-loading').classList.add('d-none');
            byId('ai-settings-workspace').classList.remove('d-none');
        } catch (error) {
            byId('ai-settings-loading').classList.add('d-none');
            const fatal = byId('ai-settings-fatal');
            fatal.textContent = `AI 服务配置加载失败：${error.message}`;
            fatal.classList.remove('d-none');
        } finally {
            setBusy(false);
        }
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
            method: 'PUT',
            headers: requestHeaders(),
            body: JSON.stringify(currentFormPayload()),
            cache: 'no-store',
            credentials: 'same-origin',
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
        setBusy(true, button, '正在保存…');
        try {
            await persistCurrentForm();
            setMessage('设置已保存。即使服务不提供模型列表，手动填写的模型 ID 也已生效。', 'success');
        } catch (error) {
            setMessage(`保存失败：${error.message}`, 'danger');
        } finally {
            setBusy(false, button);
        }
    }

    async function loadModels() {
        if (state.busy || !validateBaseUrl()) return;
        const button = byId('load-ai-models');
        const requestGeneration = state.modelRequestGeneration;
        const catalog = byId('ai-model-catalog');
        const payload = { base_url: byId('ai-base-url').value.trim() };
        const apiKey = byId('ai-api-key').value;
        if (apiKey) payload.api_key = apiKey;

        setMessage('');
        setModelStatus('正在向当前服务读取模型列表…', 'loading');
        catalog.setAttribute('aria-busy', 'true');
        setBusy(true, button, '正在读取…');
        try {
            const result = await jsonResponse(await fetch(root.dataset.modelsUrl, {
                method: 'POST',
                headers: requestHeaders(),
                body: JSON.stringify(payload),
                cache: 'no-store',
                credentials: 'same-origin',
            }));
            if (requestGeneration !== state.modelRequestGeneration) {
                setModelStatus('连接信息已发生变化，已丢弃旧地址返回的模型列表。', 'notice');
                return;
            }
            const rawModels = Array.isArray(result.models) ? result.models : [];
            state.models = Array.from(new Set(rawModels.filter(model => (
                typeof model === 'string' && model.trim()
            )).map(model => model.trim())));
            renderModelCatalog();
            setModelStatus(
                state.models.length
                    ? `已读取 ${state.models.length} 个模型。点击可选择，也可继续手动填写。`
                    : '服务未返回可用模型，请直接手动填写模型 ID。',
                state.models.length ? 'success' : 'notice',
            );
        } catch (error) {
            clearModelCatalog();
            setModelStatus(`模型列表读取失败：${error.message}。仍可手动填写模型 ID 并保存。`, 'warning');
        } finally {
            catalog.setAttribute('aria-busy', 'false');
            setBusy(false, button);
        }
    }

    async function clearRuntimeKey() {
        if (state.busy || !state.settings?.api_key_configured
            || state.settings.api_key_source === 'environment') return;
        const button = byId('clear-ai-key');
        setMessage('');
        setBusy(true, button, '正在清除…');
        try {
            const settings = await jsonResponse(await fetch(root.dataset.settingsUrl, {
                method: 'PUT',
                headers: requestHeaders(),
                cache: 'no-store',
                credentials: 'same-origin',
                body: JSON.stringify({
                    enabled: state.settings.enabled,
                    base_url: state.settings.base_url,
                    model: state.settings.model,
                    clear_api_key: true,
                }),
            }));
            byId('ai-api-key').value = '';
            clearModelCatalog('访问密钥已改变，需要时请重新读取模型列表。');
            render(settings);
            setMessage('页面设置的访问密钥已从当前服务进程清除。', 'success');
        } catch (error) {
            setMessage(`清除失败：${error.message}`, 'danger');
        } finally {
            setBusy(false, button);
        }
    }

    async function testConnection() {
        if (state.busy) return;
        const button = byId('test-ai-connection');
        setMessage('');
        setBusy(true, button, '正在测试…');
        try {
            const result = await jsonResponse(await fetch(root.dataset.testUrl, {
                method: 'POST',
                headers: requestHeaders(),
                body: '{}',
                cache: 'no-store',
                credentials: 'same-origin',
            }));
            setMessage(`连接成功：已保存的模型 ${result.model} 可用。`, 'success');
        } catch (error) {
            setMessage(`连接测试失败：${error.message}。已保存设置不会被撤销，你仍可继续使用手动填写的模型 ID。`, 'warning');
        } finally {
            setBusy(false, button);
        }
    }

    function toggleKeyVisibility() {
        const input = byId('ai-api-key');
        const button = byId('toggle-ai-key');
        const reveal = input.type === 'password';
        input.type = reveal ? 'text' : 'password';
        button.setAttribute('aria-pressed', String(reveal));
        button.setAttribute('aria-label', reveal ? '隐藏本次输入的访问密钥' : '显示本次输入的访问密钥');
        input.focus();
    }

    function connectionInputChanged() {
        const hadCatalog = state.models.length > 0
            || !byId('ai-model-catalog-wrap').classList.contains('d-none');
        clearModelCatalog(hadCatalog ? '连接信息已改变，请重新读取模型列表。' : '');
    }

    function modelInputChanged() {
        if (state.models.length) renderModelCatalog(byId('ai-model').value);
    }

    function handleModelInputKeydown(event) {
        if (event.key !== 'ArrowDown' || !state.models.length) return;
        const firstModel = byId('ai-model-catalog').querySelector('button:not([disabled])');
        if (!firstModel) return;
        event.preventDefault();
        firstModel.focus();
    }

    byId('ai-settings-form').addEventListener('submit', saveSettings);
    byId('load-ai-models').addEventListener('click', loadModels);
    byId('clear-ai-key').addEventListener('click', clearRuntimeKey);
    byId('test-ai-connection').addEventListener('click', testConnection);
    byId('toggle-ai-key').addEventListener('click', toggleKeyVisibility);
    byId('ai-base-url').addEventListener('input', connectionInputChanged);
    byId('ai-api-key').addEventListener('input', connectionInputChanged);
    byId('ai-model').addEventListener('input', modelInputChanged);
    byId('ai-model').addEventListener('keydown', handleModelInputKeydown);
    setBusy(true);
    loadSettings();
})();
