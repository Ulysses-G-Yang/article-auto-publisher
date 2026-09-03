from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ai_settings_page_is_wired_without_key_persistence_or_echo() -> None:
    base = read("web/templates/base.html")
    template = read("web/templates/ai_settings.html")
    script = read("web/static/js/ai-settings.js")

    assert 'href="/settings/ai"' in base
    assert 'id="ai-api-key"' in template
    assert 'type="password"' in template
    assert 'autocomplete="new-password"' in template
    assert 'aria-describedby="ai-base-url-help ai-base-url-error"' in template
    assert 'aria-describedby="ai-model-help ai-model-error"' in template
    assert "现有 Key 永不回显" in template
    assert "只保存在当前服务进程内" in template
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


def test_studio_requests_readonly_advice_for_current_revision_and_platforms() -> None:
    template = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert 'id="generate-publication-advice"' in template
    assert 'disabled title="正在读取平台目录"' in template
    assert 'data-publication-advice-url-template=' in template
    assert 'id="publication-advice-results"' in template
    assert "async function generatePublicationAdvice()" in script
    assert "const platforms = enabledPlatformIds();" in script
    assert "revision: adviceRevision, platforms" in script
    assert "await saveDraftNow()" in script
    assert "panel.setAttribute('aria-busy', 'true')" in script
    assert "renderPublicationAdvice(advice);" in script
    assert "state.dirty" in script
    assert "advice.draft_id !== adviceDraftId" in script
    assert "advice.revision !== adviceRevision" in script
    assert "document.createElement('article')" in script
    assert "建议仅供核对，不会自动选择话题、修改草稿或执行平台操作" in template
