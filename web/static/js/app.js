const SIDEBAR_STORAGE_KEY = 'articleops.sidebar-collapsed.v1';
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
}

function toggleSidebar() {
    if (window.innerWidth < 992) {
        setSidebarOpen(!document.body.classList.contains('sidebar-mobile-open'));
        return;
    }
    const collapsed = document.body.classList.toggle('sidebar-collapsed');
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(collapsed));
}

function initSidebar() {
    if (window.innerWidth >= 992 && localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true') {
        document.body.classList.add('sidebar-collapsed');
    }
    document.getElementById('sidebar-toggle')?.addEventListener('click', toggleSidebar);
    document.getElementById('sidebar-close')?.addEventListener('click', () => setSidebarOpen(false));
    document.getElementById('sidebar-backdrop')?.addEventListener('click', () => setSidebarOpen(false));
    document.querySelectorAll('#app-sidebar a').forEach(link => link.addEventListener('click', () => setSidebarOpen(false)));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') setSidebarOpen(false);
    });
}

function formatTime(isoString) {
    if (!isoString) return '-';
    return isoString.replace('T', ' ').substring(0, 19);
}

const PLATFORM_LABELS = { zol: '中关村在线', xiaoheihe: '小黑盒' };
const STATUS_LABELS = {
    queued: '排队中', processing: '处理中', completed: '已完成',
    failed: '失败', retrying: '重试中', cancelled: '已取消',
};

document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', initSidebar, { once: true })
    : initSidebar();
