import re
from pathlib import Path

from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[2]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def render_main_shell(**context: object) -> str:
    app = Flask(
        "articleops_frontend_contract",
        template_folder=str(ROOT / "web/templates"),
        static_folder=str(ROOT / "web/static"),
    )
    with app.test_request_context("/upload"):
        return render_template("base.html", **context)


def test_dashboard_has_accessible_switchable_metric_cards() -> None:
    template = read("src/article_mvp/web/templates/dashboard.html")
    script = read("src/article_mvp/web/static/dashboard.js")

    assert 'role="group" aria-label="指标卡显示模式"' in template
    assert 'data-metric-mode="overview"' in template
    assert 'data-metric-mode="article"' in template
    assert 'id="article-picker"' in template
    assert 'aria-live="polite"' in template
    assert 'id="sidebar-toggle"' in template
    assert 'aria-controls="app-sidebar"' in template
    assert re.search(r'id="sidebar-toggle"[^>]+aria-expanded="(?:true|false)"', template)
    assert "METRIC_MODE_STORAGE_KEY" in script
    assert "populateArticlePicker()" in script
    assert "renderOverviewMetrics()" in script
    assert "renderArticleMetrics()" in script
    assert 'setAttribute("aria-expanded"' in script


def test_dashboard_uses_platform_first_analytics_layout_without_fake_trends() -> None:
    template = read("src/article_mvp/web/templates/dashboard.html")
    script = read("src/article_mvp/web/static/dashboard.js")
    styles = read("src/article_mvp/web/static/dashboard.css")

    assert 'id="platform-filter-list"' in template
    assert 'aria-label="按平台筛选文章"' in template
    assert 'id="platform-bars"' in template
    assert 'id="mapping-ring"' in template
    assert "PLATFORM_FILTER_STORAGE_KEY" in script
    assert "renderPlatformFilters()" in script
    assert "renderPlatformBars()" in script
    assert "renderMappingChart()" in script
    assert "Promise.allSettled" in script
    assert "ECharts" not in template + script
    assert ".analytics-layout" in styles
    assert "min-height: 44px" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_dashboard_combines_two_independent_api_sources() -> None:
    template = read("src/article_mvp/web/templates/dashboard.html")
    script = read("src/article_mvp/web/static/dashboard.js")

    assert 'data-dashboard-api="{{ url_for(\'article_mvp_dashboard.api_dashboard\') }}"' in template
    assert 'data-legacy-summary-api="/api/legacy-summary"' in template
    assert "loadDashboardData()" in script
    assert "loadLegacySummary()" in script
    assert "Promise.allSettled" in script
    assert "renderDashboardFailure(error)" in script
    assert "renderLegacySummaryFailure()" in script
    assert script.count("if (dashboardPayload) {") >= 2
    assert "旧发布摘要暂不可用" in script
    assert "新看板加载失败" in script
    assert "payload.current_workflow" not in script


def test_nullable_metrics_do_not_fall_back_to_zero() -> None:
    script = read("src/article_mvp/web/static/dashboard.js")

    assert 'value === null || value === undefined ? "—"' in script
    assert 'snapshotTime ? formatTime(snapshotTime) : "尚未采集"' in script
    assert 'article.published_at ? formatTime(article.published_at) : "—"' in script
    assert "created_at 仅" not in script
    assert 'displayMetric(metric.exposure_count)' in script
    assert 'displayMetric(metric.share_count)' in script


def test_original_article_link_is_safe_and_accessible() -> None:
    script = read("src/article_mvp/web/static/dashboard.js")

    assert 'content.target = "_blank"' in script
    assert 'content.rel = "noopener noreferrer"' in script
    assert 'content.setAttribute("aria-label"' in script
    assert "原始链接（在新标签页打开）" in script


def test_read_only_legacy_pages_keep_their_compatible_contracts() -> None:
    index = read("web/templates/index.html")
    task = read("web/templates/task_detail.html")

    assert "fetch('/api/tasks')" in index
    assert "fetch('/api/status')" in index
    assert "/api/tasks/{{ task.id if task else 0 }}/resume" in task
    assert "JSON.stringify({community: this.community, topic: this.topic})" in task


def test_accounts_page_is_the_modern_multi_account_module_only() -> None:
    accounts = read("web/templates/accounts.html")

    # legacy 硬编码账号板块（ZOL/小黑盒 + Alpine /api/accounts 轮询）已删除。
    assert "x-data=" not in accounts
    assert "fetch('/api/accounts')" not in accounts
    assert "xiaoheihe" not in accounts
    assert "中关村在线" not in accounts
    # 现代多账号会话模块与活动抽屉保留。
    assert "multi-account-sessions" in accounts
    assert "data-delete-account-url-template" in accounts
    assert "account-activity-drawer" in accounts


def test_legacy_upload_queue_is_not_used_by_new_page() -> None:
    upload = read("web/templates/upload.html")
    script = read("web/static/js/content-studio.js")

    assert "/api/upload" not in upload + script
    assert "/api/content-drafts/import-docx" in upload
    assert "formData.append('platforms'" not in upload


def test_shared_shell_has_navigation_and_mobile_controls() -> None:
    base = read("web/templates/base.html")
    script = read("web/static/js/app.js")

    for path in ('href="/"', 'href="/upload"', 'href="/accounts"', 'href="/data-center/"'):
        assert path in base
    assert "request.path.startswith('/task/')" in base
    assert 'href="/delivery/new"' not in base
    assert 'id="sidebar-toggle"' in base
    assert 'id="sidebar-close"' in base
    assert 'id="sidebar-backdrop"' in base
    assert "sidebar-mobile-open" in script
    assert "event.key === 'Escape'" in script


def test_sidebar_permission_projection_fails_closed_when_explicit() -> None:
    legacy_html = render_main_shell()
    limited_html = render_main_shell(sidebar_permissions={"content_studio": True})
    empty_html = render_main_shell(sidebar_permissions={})

    # 未接入权限上下文的旧页面保持兼容；显式权限映射一旦存在，缺失项必须隐藏。
    assert legacy_html.count("data-permission=") == 5
    assert 'data-permission="content_studio"' in limited_html
    for permission in ("overview", "accounts", "data_center", "ai_settings"):
        assert f'data-permission="{permission}"' not in limited_html
    assert "data-permission=" not in empty_html


def test_dashboard_permission_projection_uses_the_same_fail_closed_rule() -> None:
    from article_mvp.web import create_dashboard_app

    app = create_dashboard_app()

    @app.context_processor
    def limited_sidebar() -> dict[str, object]:
        return {"sidebar_permissions": {"data_center": True}}

    response = app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-permission="data_center"' in html
    for permission in ("overview", "content_studio", "accounts", "ai_settings"):
        assert f'data-permission="{permission}"' not in html


def test_frontend_assets_use_current_ui_cache_versions() -> None:
    base = read("web/templates/base.html")
    accounts = read("web/templates/accounts.html")
    upload = read("web/templates/upload.html")
    ai_settings = read("web/templates/ai_settings.html")
    dashboard = read("src/article_mvp/web/templates/dashboard.html")

    assert "filename='css/design-tokens.css', v='20260903-shell-v3'" in base
    assert "filename='css/style.css', v='20260903-shell-v3'" in base
    assert "filename='js/app.js', v='20260902-ui-v2'" in base
    assert "filename='css/account-sessions.css', v='20260903-shell-v3'" in accounts
    assert "filename='css/content-studio.css', v='20260903-shell-v3'" in upload
    assert "filename='js/content-studio.js', v='20260903-shell-v3'" in upload
    assert "filename='css/ai-settings.css', v='20260903-shell-v3'" in ai_settings
    assert "filename='dashboard.css', v='20260903-dashboard-v3'" in dashboard
    assert "filename='dashboard.js', v='20260903-dashboard-v3'" in dashboard


def test_shared_pages_match_dashboard_shell_tokens() -> None:
    tokens = read("web/static/css/design-tokens.css")
    style = read("web/static/css/style.css")
    studio = read("web/static/css/content-studio.css")
    ai_settings = read("web/static/css/ai-settings.css")

    assert "--ao-canvas: #f3f5f9;" in tokens
    assert "--ao-sidebar-bg: #ffffff;" in tokens
    assert "--ao-sidebar-active: rgb(37 99 235 / 0.10);" in tokens
    assert "--ao-content-max: 1600px;" in tokens
    assert ".sidebar-brand:hover { color: var(--ao-primary-700); }" in style
    assert "min-height: 44px;" in style
    assert "color: var(--ao-primary-700);" in style
    assert ".task-page { max-width: var(--ao-content-max); }" in style
    assert ".studio-shell { width: 100%; max-width: var(--ao-content-max);" in studio
    assert ".safety-card { border-color: var(--ao-border); background: var(--ao-surface);" in studio
    assert "max-width: var(--ao-content-max);" in ai_settings


def test_user_facing_operation_states_are_localized_to_chinese() -> None:
    app = read("web/static/js/app.js")
    index = read("web/templates/index.html")
    upload = read("web/templates/upload.html")
    studio = read("web/static/js/content-studio.js")
    accounts = read("web/static/js/account-sessions.js")
    task = read("web/templates/task_detail.html")
    dashboard = read("src/article_mvp/web/static/dashboard.js")

    assert "DRAFT_SAVED_WITH_WARNINGS: '草稿已保存（需核对）'" in app
    assert "DRAFT_SAVED_WITH_WARNINGS: '草稿已保存（需核对）'" in studio
    assert 'x-text="op.error_code"' not in index
    assert 'data-ui-error-code' in task
    assert '>READY</span>' not in upload
    assert '核验失败（${code}）' not in studio
    assert "VALID 登录态" not in studio
    assert "当前不是 VALID" not in studio
    assert "event.event_type || event.action || '账号事件'" not in accounts
    assert "[{{ log.level }}]" not in task
    assert "HTTPX ENABLED" not in dashboard
    assert "COLLECTOR BLOCKED" not in dashboard
    assert "DATA UNAVAILABLE" not in dashboard
    assert "PARTIAL_FAIL: '部分成功 / 部分失败'" in app
    assert 'PARTIAL_FAIL: "部分成功 / 部分失败"' in dashboard
    assert 'DELETED: "已删除"' in dashboard
    assert "payload.message || payload.error" not in dashboard
    assert "filename='js/content-studio.js'" in upload
