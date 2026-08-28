const SIDEBAR_STORAGE_KEY = 'articleops.sidebar-collapsed.v1';

function readShellPreference(key) {
    try {
        return window.localStorage.getItem(key);
    } catch (_) {
        return null;
    }
}

function writeShellPreference(key, value) {
    try {
        window.localStorage.setItem(key, value);
    } catch (_) {
        // 隐私模式或禁用存储时，导航仍应正常工作，只是不持久化偏好。
    }
}
// CoreUI's Bootstrap-compatible components are the sole runtime implementation.
// Keep the historical `bootstrap.*` calls working while legacy pages migrate.
const bootstrap = window.coreui;
window.bootstrap = window.coreui;

const api = {
    async get(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    },
    async post(url, data) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    },
};

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container') || createToastContainer();
    const toast = document.createElement('div');
    const bgClass = { info: 'bg-info', success: 'bg-success', error: 'bg-danger', warn: 'bg-warning' }[type] || 'bg-info';
    toast.className = `toast align-items-center text-white ${bgClass} border-0`;
    toast.setAttribute('role', 'alert');
    const row = document.createElement('div');
    row.className = 'd-flex';
    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white me-2 m-auto';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', '关闭提示');
    row.append(body, close);
    toast.appendChild(row);
    container.appendChild(toast);
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();
    toast.addEventListener('hidden.bs.toast', () => toast.remove());
}

function createToastContainer() {
    const container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
    document.body.appendChild(container);
    return container;
}

function setSidebarOpen(open) {
    document.body.classList.toggle('sidebar-mobile-open', open);
    const button = document.getElementById('sidebar-toggle');
    if (button) button.setAttribute('aria-expanded', String(open));
    if (!open) document.getElementById('sidebar-toggle')?.focus({ preventScroll: true });
}

function toggleSidebar() {
    if (window.innerWidth < 992) {
        setSidebarOpen(!document.body.classList.contains('sidebar-mobile-open'));
        return;
    }
    const collapsed = document.body.classList.toggle('sidebar-collapsed');
    writeShellPreference(SIDEBAR_STORAGE_KEY, String(collapsed));
}

function initSidebar() {
    const sidebar = document.getElementById('app-sidebar');
    if (!sidebar || sidebar.dataset.initialized === 'true') return;
    sidebar.dataset.initialized = 'true';
    if (window.innerWidth >= 992 && readShellPreference(SIDEBAR_STORAGE_KEY) === 'true') {
        document.body.classList.add('sidebar-collapsed');
    }
    document.getElementById('sidebar-toggle')?.addEventListener('click', toggleSidebar);
    document.getElementById('sidebar-close')?.addEventListener('click', () => setSidebarOpen(false));
    document.getElementById('sidebar-backdrop')?.addEventListener('click', () => setSidebarOpen(false));
    document.querySelectorAll('#app-sidebar a').forEach(link => link.addEventListener('click', () => {
        if (window.innerWidth < 992) setSidebarOpen(false);
    }));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') setSidebarOpen(false);
    });
    window.addEventListener('resize', () => {
        if (window.innerWidth >= 992) document.body.classList.remove('sidebar-mobile-open');
    });
}

function formatTime(isoString) {
    if (!isoString) return '-';
    return isoString.replace('T', ' ').substring(0, 19);
}

const PLATFORM_LABELS = { zol: '中关村在线', xiaoheihe: '小黑盒' };
const STATUS_LABELS = {
    queued: '排队中', processing: '处理中', completed: '已完成',
    completed_with_warnings: '草稿已保存（需核对）', failed: '失败',
    retrying: '重试中', cancelled: '已取消', paused: '已暂停',
    needs_selection: '待补充平台信息',
    READY: '待执行', CREATING: '正在创建执行单', QUEUED: '已排队',
    RUNNING: '执行中', SUCCESS: '全部成功', PARTIAL_FAIL: '部分成功 / 部分失败',
    FATAL: '全部失败', CONFIRMATION_REQUIRED: '等待公开发布确认',
    DRAFT_SAVED: '草稿已保存',
    DRAFT_SAVED_WITH_WARNINGS: '草稿已保存（需核对）',
    PUBLISHED: '已公开发布', PUBLISHED_WITH_WARNINGS: '已发布（需核对）',
    RESULT_UNKNOWN: '结果未知，需人工核对',
    DELIVERY_INCOMPLETE: '投递未完成', FORMAT_REVIEW_REQUIRED: '待格式处理',
    AWAITING_CONFIRMATION: '等待公开发布确认', BLOCKED: '已拦截',
    FAILED: '失败', VALID: '有效', UNVERIFIED: '未验证',
    LOGIN_REQUIRED: '需要登录', ERROR: '验证异常', EXPIRED: '已过期',
    VERIFYING: '验证中', BUSY: '使用中', ACTIVE: '使用中', ARCHIVED: '已归档',
};

const ERROR_CODE_LABELS = {
    DRAFT_RESULT_UNKNOWN: '草稿保存结果未知',
    DELIVERY_RESULT_UNKNOWN: '投递结果未知',
    DELIVERY_INCOMPLETE: '投递未完成',
    DRAFT_BASELINE_UNAVAILABLE: '无法确认保存前草稿状态',
    DRAFT_BASELINE_FAILED: '保存前草稿状态校验失败',
    LOGIN_REQUIRED: '需要重新登录',
    SESSION_EXPIRED: '登录状态已过期',
    ACCOUNT_SESSION_EXPIRED: '账号登录状态已过期',
    ACCOUNT_IDENTITY_UNVERIFIED: '无法确认平台账号身份',
    ACCOUNT_IDENTITY_MISMATCH: '当前登录账号与已绑定账号不一致',
    CONTENT_VALIDATION_ERROR: '文章内容校验未通过',
    CONTENT_FORMAT_UNSUPPORTED: '文章格式需要自动调整',
    PLATFORM_MEDIA_INCOMPLETE: '图片未完整写入平台',
    PLATFORM_COVER_UNSUPPORTED: '当前平台暂不支持自动设置封面',
    BROWSER_CONTEXT_CLOSED: '平台浏览器窗口已关闭',
    SELECTOR_ERROR: '平台页面结构发生变化',
    RATE_LIMITED: '平台操作过于频繁，请稍后再试',
    PROFILE_IN_USE: '账号登录窗口正在使用中',
    CHALLENGE: '平台要求完成人工安全验证',
    XHS_CLOUD_DRAFT_UNAVAILABLE: '网页端没有可验证的云端草稿',
};

function statusLabel(value) {
    return STATUS_LABELS[value] || (value ? '状态待确认' : '状态未知');
}

function errorCodeLabel(value) {
    if (!value) return '';
    if (ERROR_CODE_LABELS[value]) return ERROR_CODE_LABELS[value];
    const code = String(value).toUpperCase();
    if (code.includes('LOGIN') || code.includes('SESSION')) return '登录状态异常';
    if (code.includes('BASELINE')) return '保存前状态无法确认';
    if (code.includes('IMAGE') || code.includes('MEDIA') || code.includes('COVER')) {
        return '图片或封面处理未完成';
    }
    if (code.includes('RESULT_UNKNOWN') || code.includes('UNKNOWN')) return '结果未知，需人工核对';
    if (code.includes('CONTENT') || code.includes('VALIDATION') || code.includes('FORMAT')) {
        return '文章内容校验未通过';
    }
    return '处理异常，请查看执行日志';
}

function userFacingErrorMessage(code, message, fallback = '操作未完成，请查看执行记录。') {
    const raw = typeof message === 'string' ? message.trim() : '';
    const withoutCode = raw.replace(/^[A-Z][A-Z0-9_]+\s*[:：]\s*/, '').trim();
    if (withoutCode && /[\u3400-\u9fff]/u.test(withoutCode)) return withoutCode;
    return errorCodeLabel(code) || fallback;
}

window.ArticleOpsUi = Object.freeze({
    statusLabel,
    errorCodeLabel,
    userFacingErrorMessage,
});

function localizeUiText() {
    document.querySelectorAll('[data-ui-error-code]').forEach(element => {
        element.textContent = errorCodeLabel(element.dataset.uiErrorCode);
    });
    document.querySelectorAll('[data-ui-status]').forEach(element => {
        element.textContent = statusLabel(element.dataset.uiStatus);
    });
    document.querySelectorAll('[data-ui-error-message]').forEach(element => {
        element.textContent = userFacingErrorMessage(
            element.dataset.uiMessageCode,
            element.textContent,
        );
    });
}

function initAppShell() {
    initSidebar();
    localizeUiText();
}

document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', initAppShell, { once: true })
    : initAppShell();
