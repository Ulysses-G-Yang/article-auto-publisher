import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def function_body(script: str, name: str, next_name: str) -> str:
    match = re.search(
        rf"async function {name}\(.*?\) \{{(?P<body>.*?)\n    \}}\n\n"
        rf"    (?:async )?function {next_name}\(",
        script,
        flags=re.DOTALL,
    )
    assert match, f"missing function {name}"
    return match.group("body")


def test_ai_settings_page_is_wired_without_key_persistence_or_echo() -> None:
    base = read("web/templates/base.html")
    template = read("web/templates/ai_settings.html")
    script = read("web/static/js/ai-settings.js")

    assert 'href="/settings/ai"' in base
    assert 'id="ai-api-key"' in template
    assert 'type="password"' in template
    assert 'autocomplete="new-password"' in template
    assert 'data-models-url="/api/settings/publication-ai/models"' in template
    assert 'aria-describedby="ai-base-url-help ai-base-url-error"' in template
    assert "ai-model-help ai-model-error ai-model-status" in template
    assert 'role="combobox"' in template
    assert 'role="listbox"' in template
    assert 'id="load-ai-models"' in template
    assert "测试已保存连接" in template
    assert "现有密钥永不回显" in template
    assert "新密钥只有保存设置时才进入当前服务进程" in template
    assert "任何时候都可以直接填写模型 ID" in template
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "byId('ai-api-key').value = '';" in script
    assert "await persistCurrentForm();" in script
    assert "root.setAttribute('aria-busy', String(busy));" in script
    assert "input.setAttribute('aria-invalid'" in script
    assert "api_key_configured" in script
    assert "X-ArticleOps-AI-Settings" in script
    assert "textContent" in script
    assert "innerHTML" not in script


def test_model_catalog_is_optional_and_does_not_persist_temporary_credentials() -> None:
    script = read("web/static/js/ai-settings.js")

    load_models = function_body(script, "loadModels", "clearRuntimeKey")
    save_settings = function_body(script, "saveSettings", "loadModels")
    test_connection = function_body(script, "testConnection", "toggleKeyVisibility")

    assert "root.dataset.modelsUrl" in load_models
    assert "payload.api_key = apiKey" in load_models
    assert "persistCurrentForm" not in load_models
    assert "仍可手动填写模型 ID 并保存" in load_models
    assert "await persistCurrentForm();" in save_settings
    assert "root.dataset.modelsUrl" not in save_settings
    assert "method: 'POST'" in test_connection
    assert "persistCurrentForm" not in test_connection
    assert "已保存设置不会被撤销" in test_connection


def test_ai_settings_accessibility_and_responsive_contracts() -> None:
    template = read("web/templates/ai_settings.html")
    styles = read("web/static/css/ai-settings.css")

    assert 'aria-live="polite"' in template
    assert 'aria-busy="true"' in template
    assert "root.setAttribute('aria-busy', String(busy));" in read(
        "web/static/js/ai-settings.js"
    )
    assert "min-height: 44px" in styles
    assert ".ai-model-option:focus-visible" in styles
    assert "outline: 2px solid var(--ao-focus)" in styles
    assert "@media (max-width: 639.98px)" in styles
    assert "@media (prefers-reduced-motion: no-preference)" in styles
    assert "var(--ao-" in styles


def test_studio_requests_readonly_advice_for_current_revision_and_platforms() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="generate-publication-advice"' in template
    assert "生成平台建议" in template
    assert 'disabled title="正在读取平台目录"' in template
    assert 'data-publication-advice-url-template=' in template
    assert 'id="publication-advice-results"' in template
    assert 'id="cancel-publication-advice"' in template
    assert "取消分析" in template
    assert 'id="publication-advice-countdown"' in template
    assert 'id="publication-advice-seconds">90</strong> 秒' in template
    assert 'id="publication-advice-progress"' in template
    for stage, label in (
        ("saving", "保存当前文章"),
        ("rules", "读取平台规则"),
        ("request", "请求 AI"),
        ("validation", "校验返回结果"),
    ):
        assert f'data-advice-stage="{stage}"' in template
        assert label in template
    assert "async function generatePublicationAdvice()" in script
    assert "const platforms = enabledPlatformIds();" in script
    assert "revision: adviceRevision, platforms" in script
    assert "await saveDraftNow()" in script
    assert "panel.setAttribute('aria-busy', 'true')" in script
    assert "new AbortController()" in script
    assert "signal:" in script
    assert "90" in script
    assert "cancelPublicationAdvice" in script
    assert "生成平台建议已超时" in script
    assert "已取消生成平台建议" in script
    for status in (
        "正在保存当前文章",
        "正在读取平台规则",
        "正在请求 AI",
        "正在校验返回结果",
    ):
        assert status in script
    assert "scrollIntoView" in script
    assert "renderPublicationAdvice(advice);" in script
    assert "state.dirty" in script
    assert "advice.draft_id !== adviceDraftId" in script
    assert "advice.revision !== adviceRevision" in script
    assert "document.createElement('article')" in script
    assert "建议仅供核对，不会自动选择话题、修改草稿或执行平台操作" in template
