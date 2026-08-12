const byId = (id) => document.getElementById(id);

const LAYOUT_STORAGE_KEY = "article-mvp.dashboard-layout.v1";
const THEME_STORAGE_KEY = "article-mvp.dashboard-theme.v1";
const SIDEBAR_STORAGE_KEY = "article-mvp.sidebar-collapsed.v1";
const METRIC_MODE_STORAGE_KEY = "article-mvp.metric-mode.v1";

const DEFAULT_LAYOUT = [
  { id: "summary", x: 0, y: 0, w: 12, h: 3 },
  { id: "current-tasks", x: 0, y: 3, w: 8, h: 5 },
  { id: "contract", x: 8, y: 3, w: 4, h: 5 },
  { id: "articles", x: 0, y: 8, w: 8, h: 5 },
  { id: "runs", x: 8, y: 8, w: 4, h: 5 },
];

let dashboardGrid;
let dashboardEditing = false;
let currentTasks = [];
let dashboardPayload = null;
let metricMode = "overview";
let savedLayoutState = null;

function setText(id, value, fallback = "—") {
  const target = byId(id);
  if (target) target.textContent = value ?? fallback;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function platformLabel(platform) {
  if (platform === "xiaoheihe") return "小黑盒";
  if (platform === "zol") return "中关村在线";
  return platform || "—";
}

function displayMetric(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN");
}

function metricRow(label, value, options = {}) {
  const row = document.createElement("div");
  row.className = options.primary ? "metric-row metric-row-primary" : "metric-row";
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement(options.href ? "a" : "strong");
  content.textContent = value ?? "—";
  if (options.href) {
    content.href = options.href;
    content.target = "_blank";
    content.rel = "noopener noreferrer";
    content.setAttribute("aria-label", options.ariaLabel || `${label}（在新标签页打开）`);
  }
  row.append(name, content);
  return row;
}

function replaceMetricRows(id, rows) {
  const target = byId(id);
  target.replaceChildren(...rows);
}

function sumAvailable(articles, field) {
  const values = articles
    .map((article) => article.latest_metric?.[field])
    .filter((value) => value !== null && value !== undefined);
  return values.length ? values.reduce((total, value) => total + Number(value), 0) : null;
}

function latestSnapshotTime(articles) {
  const values = articles.map((article) => article.latest_metric?.snapshot_time).filter(Boolean);
  return values.sort((left, right) => new Date(right) - new Date(left))[0] || null;
}

function freshnessLabel(snapshotTime) {
  if (!snapshotTime) return "暂无数据";
  const time = new Date(snapshotTime);
  if (Number.isNaN(time.getTime())) return "时间未知";
  const minutes = Math.max(0, Math.round((Date.now() - time.getTime()) / 60000));
  if (minutes < 5) return "刚刚更新";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

function articleTitle(article) {
  if (article.title) return article.title;
  const task = currentTasks.find((item) => Number(item.id) === Number(article.task_id));
  return task?.article_title || task?.title_used || `文章 ${article.external_article_id || article.id}`;
}

function sortedArticles(articles) {
  return [...articles].sort((left, right) => {
    const leftTime = new Date(left.published_at || left.created_at || 0).getTime();
    const rightTime = new Date(right.published_at || right.created_at || 0).getTime();
    return rightTime - leftTime || Number(right.id || 0) - Number(left.id || 0);
  });
}

function selectedArticle() {
  const articles = sortedArticles(dashboardPayload?.articles || []);
  const selectedId = byId("article-picker").value;
  return articles.find((article) => String(article.id) === selectedId) || articles[0] || null;
}

function populateArticlePicker() {
  const picker = byId("article-picker");
  const previous = picker.value;
  const articles = sortedArticles(dashboardPayload?.articles || []);
  picker.replaceChildren();
  for (const article of articles) {
    const option = document.createElement("option");
    option.value = String(article.id);
    option.textContent = `${articleTitle(article)} · ${platformLabel(article.platform)}`;
    picker.appendChild(option);
  }
  if (articles.some((article) => String(article.id) === previous)) picker.value = previous;
  picker.disabled = articles.length === 0;
}

function renderOverviewMetrics() {
  const payload = dashboardPayload || {};
  const articles = payload.articles || [];
  const summary = payload.summary || {};
  const snapshotTime = latestSnapshotTime(articles);
  const publishedTimes = articles.map((article) => article.published_at).filter(Boolean).sort().reverse();
  replaceMetricRows("metric-basic", [
    metricRow("文章总数", displayMetric(summary.total_articles), { primary: true }),
    metricRow("已映射", displayMetric(summary.mapped_articles)),
    metricRow("最新发布时间", publishedTimes[0] ? formatTime(publishedTimes[0]) : "—"),
  ]);
  replaceMetricRows("metric-traffic", [
    metricRow("阅读 / 播放", displayMetric(sumAvailable(articles, "read_count")), { primary: true }),
    metricRow("曝光", displayMetric(sumAvailable(articles, "exposure_count"))),
  ]);
  replaceMetricRows("metric-engagement", [
    metricRow("点赞", displayMetric(sumAvailable(articles, "like_count")), { primary: true }),
    metricRow("评论", displayMetric(sumAvailable(articles, "comment_count"))),
    metricRow("收藏", displayMetric(sumAvailable(articles, "collect_count"))),
  ]);
  replaceMetricRows("metric-collection", [
    metricRow("分享", displayMetric(sumAvailable(articles, "share_count")), { primary: true }),
    metricRow("最近采集", snapshotTime ? formatTime(snapshotTime) : "尚未采集"),
    metricRow("数据新鲜度", freshnessLabel(snapshotTime)),
  ]);
  byId("metric-mode-status").textContent = articles.length
    ? (snapshotTime ? `基于 ${articles.length} 篇真实文章的最新快照` : "已有文章，尚未采集指标")
    : "尚无平台文章映射";
}

function renderArticleMetrics() {
  const article = selectedArticle();
  if (!article) {
    ["metric-basic", "metric-traffic", "metric-engagement", "metric-collection"].forEach((id) => {
      replaceMetricRows(id, [metricRow("状态", "暂无文章")]);
    });
    byId("metric-mode-status").textContent = "尚无可选择的平台文章";
    return;
  }
  const metric = article.latest_metric || {};
  const snapshotTime = metric.snapshot_time;
  replaceMetricRows("metric-basic", [
    metricRow("正式标题", articleTitle(article), { primary: true }),
    metricRow("文章 ID", article.external_article_id || "—"),
    metricRow("发布时间", article.published_at ? formatTime(article.published_at) : "—"),
    metricRow("平台", platformLabel(article.platform)),
    metricRow("原始链接", article.platform_url ? "查看原文 ↗" : "链接缺失", {
      href: article.platform_url || null,
      ariaLabel: `${articleTitle(article)}原始链接（在新标签页打开）`,
    }),
  ]);
  replaceMetricRows("metric-traffic", [
    metricRow("阅读 / 播放", displayMetric(metric.read_count), { primary: true }),
    metricRow("曝光", displayMetric(metric.exposure_count)),
  ]);
  replaceMetricRows("metric-engagement", [
    metricRow("点赞", displayMetric(metric.like_count), { primary: true }),
    metricRow("评论", displayMetric(metric.comment_count)),
    metricRow("收藏", displayMetric(metric.collect_count)),
  ]);
  replaceMetricRows("metric-collection", [
    metricRow("分享", displayMetric(metric.share_count), { primary: true }),
    metricRow("最近采集", snapshotTime ? formatTime(snapshotTime) : "尚未采集"),
    metricRow("数据新鲜度", freshnessLabel(snapshotTime)),
  ]);
  byId("metric-mode-status").textContent = snapshotTime ? "显示所选文章的真实最新快照" : "所选文章尚未采集指标";
}

function renderMetricCards() {
  byId("article-picker-wrap").classList.toggle("d-none", metricMode !== "article");
  document.querySelectorAll("[data-metric-mode]").forEach((button) => {
    const active = button.dataset.metricMode === metricMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (metricMode === "article") renderArticleMetrics();
  else renderOverviewMetrics();
}

function setMetricMode(mode) {
  metricMode = mode === "article" ? "article" : "overview";
  localStorage.setItem(METRIC_MODE_STORAGE_KEY, metricMode);
  renderMetricCards();
}

function statusNode(value) {
  const span = document.createElement("span");
  span.className = `status-label ${value || ""}`;
  span.textContent = value || "—";
  return span;
}

function appendCell(row, value, options = {}) {
  const cell = document.createElement("td");
  if (value instanceof Node) cell.appendChild(value);
  else cell.textContent = value ?? "—";
  if (options.title && value) cell.title = String(value);
  row.appendChild(cell);
}

function renderArticles(articles) {
  const body = byId("articles-body");
  body.replaceChildren();
  byId("articles-empty").classList.toggle("d-none", articles.length > 0);

  for (const article of articles) {
    const row = document.createElement("tr");
    appendCell(row, statusNode(article.status));

    const articleCell = document.createElement("span");
    if (article.platform_url) {
      const link = document.createElement("a");
      link.href = article.platform_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = article.external_article_id;
      articleCell.appendChild(link);
    } else {
      articleCell.textContent = article.external_article_id;
    }
    appendCell(row, articleCell);

    const metric = article.latest_metric;
    appendCell(row, metric?.read_count);
    appendCell(row, metric?.like_count);
    appendCell(row, metric?.comment_count);
    appendCell(row, metric?.collect_count);
    appendCell(row, metric?.revenue);
    appendCell(row, formatTime(metric?.snapshot_time));
    body.appendChild(row);
  }
}

function renderRuns(runs) {
  const body = byId("runs-body");
  body.replaceChildren();
  byId("runs-empty").classList.toggle("d-none", runs.length > 0);

  for (const run of runs) {
    const row = document.createElement("tr");
    appendCell(row, `#${run.id}`);
    appendCell(row, statusNode(run.status));
    appendCell(row, run.articles_processed);
    appendCell(row, formatTime(run.started_at));
    body.appendChild(row);
  }
}

function taskMatchesSearch(task, query) {
  if (!query) return true;
  const searchable = [
    task.id,
    task.article_title,
    task.title_used,
    task.platform,
    platformLabel(task.platform),
    task.status,
  ].join(" ").toLocaleLowerCase("zh-CN");
  return searchable.includes(query);
}

function renderCurrentTaskRows(tasks) {
  const query = byId("task-search").value.trim().toLocaleLowerCase("zh-CN");
  const visibleTasks = tasks.filter((task) => taskMatchesSearch(task, query));
  const body = byId("current-tasks-body");
  body.replaceChildren();
  byId("current-tasks-empty").classList.toggle("d-none", visibleTasks.length > 0);

  for (const task of visibleTasks) {
    const row = document.createElement("tr");
    const taskLink = document.createElement("a");
    taskLink.href = `/task/${task.id}`;
    taskLink.textContent = `#${task.id}`;
    appendCell(row, taskLink);
    appendCell(row, task.article_title, { title: true });
    appendCell(row, platformLabel(task.platform));
    appendCell(row, statusNode(task.status));
    appendCell(row, task.title_used, { title: true });
    appendCell(row, formatTime(task.created_at));
    body.appendChild(row);
  }
}

function renderCurrentWorkflow(workflow) {
  const summary = workflow?.summary || {};
  const state = byId("current-workflow-state");
  const available = Boolean(workflow?.available);
  state.textContent = available ? "同库只读" : "当前任务不可用";
  state.className = available
    ? "badge text-bg-success-subtle text-success-emphasis"
    : "badge text-bg-danger-subtle text-danger-emphasis";

  currentTasks = workflow?.tasks || [];
  renderCurrentTaskRows(currentTasks);
}

function renderContract(contract) {
  setText("publish-evidence", contract.publish_evidence);
  setText("publish-source", contract.publish_source);
  setText("collector-evidence", contract.collector_evidence);
  setText("collector-source", contract.collector_source);

  const gate = byId("collector-gate");
  if (contract.collector_enabled) {
    gate.textContent = "HTTPX ENABLED";
    gate.className = "badge text-bg-success-subtle text-success-emphasis";
  } else {
    gate.textContent = "COLLECTOR BLOCKED";
    gate.className = "badge text-bg-warning-subtle text-warning-emphasis";
  }
}

async function refreshDashboard() {
  const alert = byId("alert");
  const button = byId("refresh");
  button.disabled = true;
  button.classList.add("is-loading");
  alert.classList.add("d-none");

  try {
    const response = await fetch(document.body.dataset.dashboardApi, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.error || "加载失败");
    }

    renderCurrentWorkflow(payload.current_workflow);
    dashboardPayload = payload;
    populateArticlePicker();
    renderMetricCards();
    setText("total-articles", payload.summary.total_articles, "0");
    setText("mapped-articles", payload.summary.mapped_articles, "0");
    setText("total-snapshots", payload.summary.total_snapshots, "0");
    setText("latest-run", payload.summary.latest_run_status, "暂无运行");
    renderContract(payload.contract);
    renderArticles(payload.articles || []);
    renderRuns(payload.runs || []);
    setText(
      "last-updated",
      `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`,
    );
  } catch (error) {
    alert.textContent = `看板加载失败：${error.message}`;
    alert.classList.remove("d-none");
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
  }
}

function readLayoutState() {
  try {
    const value = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY));
    if (!value || !Array.isArray(value.widgets) || !Array.isArray(value.hidden)) {
      return null;
    }
    return value;
  } catch (_error) {
    return null;
  }
}

function moduleElement(moduleId) {
  return document.querySelector(`[data-module="${moduleId}"]`);
}

function syncModuleToggles(hiddenModules) {
  const hidden = new Set(hiddenModules);
  document.querySelectorAll("[data-module-toggle]").forEach((input) => {
    input.checked = !hidden.has(input.dataset.moduleToggle);
  });
}

function hideModule(moduleId) {
  const element = moduleElement(moduleId);
  if (!element || element.hidden) return;
  dashboardGrid.removeWidget(element, false, false);
  element.hidden = true;
}

function showModule(moduleId) {
  const element = moduleElement(moduleId);
  if (!element || !element.hidden) return;
  element.hidden = false;
  dashboardGrid.makeWidget(element);
  const savedWidget = savedLayoutState?.widgets.find((item) => item.id === moduleId);
  const defaultWidget = DEFAULT_LAYOUT.find((item) => item.id === moduleId);
  if (savedWidget || defaultWidget) {
    dashboardGrid.update(element, savedWidget || defaultWidget);
  }
}

function applyLayoutState(state) {
  if (!state) return;
  dashboardGrid.load(state.widgets, false);
  for (const moduleId of state.hidden) hideModule(moduleId);
  syncModuleToggles(state.hidden);
}

function updateResponsiveSummaryHeight() {
  if (!dashboardGrid) return;
  const summary = moduleElement("summary");
  if (!summary || summary.hidden) return;

  let height = 3;
  if (window.innerWidth < 576) height = 8;
  else if (window.innerWidth < 992) height = 5;
  else {
    height = savedLayoutState?.widgets.find((item) => item.id === "summary")?.h || 2;
  }

  if (summary.gridstackNode?.h !== height) {
    dashboardGrid.update(summary, { h: height });
  }
}

function serializeLayout() {
  const widgets = dashboardGrid.save(false, false).map((item) => ({
    id: item.id,
    x: item.x,
    y: item.y,
    w: item.w,
    h: item.h,
  }));
  const hidden = Array.from(document.querySelectorAll("[data-module][hidden]"))
    .map((element) => element.dataset.module);
  return { widgets, hidden };
}

function showToast(message) {
  setText("layout-toast-message", message);
  window.coreui.Toast.getOrCreateInstance(byId("layout-toast"), { delay: 2200 }).show();
}

function setEditMode(enabled) {
  dashboardEditing = enabled;
  document.body.classList.toggle("dashboard-editing", enabled);
  byId("edit-toolbar").classList.toggle("d-none", !enabled);
  dashboardGrid.enableMove(enabled);
  dashboardGrid.enableResize(enabled);

  const button = byId("edit-dashboard");
  button.classList.toggle("btn-primary", !enabled);
  button.classList.toggle("btn-outline-primary", enabled);
  button.querySelector("i").className = enabled ? "cil-check-circle" : "cil-pencil";
  button.querySelector("span").textContent = enabled ? "完成编辑" : "编辑看板";
}

function saveLayout() {
  savedLayoutState = serializeLayout();
  localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(savedLayoutState));
  setEditMode(false);
  showToast("看板布局已保存");
}

function restoreDefaultLayout() {
  localStorage.removeItem(LAYOUT_STORAGE_KEY);
  for (const item of DEFAULT_LAYOUT) showModule(item.id);
  dashboardGrid.load(DEFAULT_LAYOUT, false);
  savedLayoutState = { widgets: DEFAULT_LAYOUT.map((item) => ({ ...item })), hidden: [] };
  syncModuleToggles([]);
  showToast("已恢复默认布局");
}

function initGrid() {
  dashboardGrid = window.GridStack.init({
    column: 12,
    cellHeight: 88,
    margin: 8,
    animate: true,
    float: false,
    disableDrag: true,
    disableResize: true,
    draggable: { handle: ".widget-drag-handle" },
    resizable: { handles: "e, se, s, sw, w" },
    columnOpts: {
      breakpoints: [
        { w: 700, c: 1, layout: "moveScale" },
        { w: 1000, c: 6, layout: "moveScale" },
        { w: 1400, c: 12, layout: "moveScale" },
      ],
    },
  }, byId("dashboard-grid"));

  savedLayoutState = readLayoutState();
  applyLayoutState(savedLayoutState);
  updateResponsiveSummaryHeight();
}

function setTheme(theme) {
  const selected = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-coreui-theme", selected);
  byId("theme-icon").className = selected === "dark" ? "cil-sun" : "cil-moon";
  localStorage.setItem(THEME_STORAGE_KEY, selected);
}

function initTheme() {
  setTheme(localStorage.getItem(THEME_STORAGE_KEY) || "light");
}

function toggleSidebar() {
  if (window.innerWidth < 992) {
    const open = document.body.classList.toggle("sidebar-mobile-open");
    byId("sidebar-toggle").setAttribute("aria-expanded", String(open));
    return;
  }
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  localStorage.setItem(SIDEBAR_STORAGE_KEY, String(collapsed));
  window.setTimeout(() => dashboardGrid?.onResize(), 220);
}

function initSidebar() {
  if (window.innerWidth >= 992 && localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true") {
    document.body.classList.add("sidebar-collapsed");
  }
  byId("sidebar-toggle").addEventListener("click", toggleSidebar);
  byId("sidebar-close").addEventListener("click", () => {
    document.body.classList.remove("sidebar-mobile-open");
    byId("sidebar-toggle").setAttribute("aria-expanded", "false");
  });
  byId("sidebar-backdrop").addEventListener("click", () => {
    document.body.classList.remove("sidebar-mobile-open");
    byId("sidebar-toggle").setAttribute("aria-expanded", "false");
  });
  document.querySelectorAll(".sidebar a").forEach((link) => {
    link.addEventListener("click", () => {
      document.body.classList.remove("sidebar-mobile-open");
      byId("sidebar-toggle").setAttribute("aria-expanded", "false");
    });
  });
  window.addEventListener("resize", updateResponsiveSummaryHeight);
}

function bindInteractions() {
  byId("refresh").addEventListener("click", refreshDashboard);
  byId("task-search").addEventListener("input", () => renderCurrentTaskRows(currentTasks));
  byId("article-picker").addEventListener("change", renderArticleMetrics);
  document.querySelectorAll("[data-metric-mode]").forEach((button) => {
    button.addEventListener("click", () => setMetricMode(button.dataset.metricMode));
  });
  byId("edit-dashboard").addEventListener("click", () => setEditMode(!dashboardEditing));
  byId("save-layout").addEventListener("click", saveLayout);
  byId("reset-layout").addEventListener("click", restoreDefaultLayout);
  byId("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-coreui-theme");
    setTheme(current === "dark" ? "light" : "dark");
  });

  document.querySelectorAll("[data-module-toggle]").forEach((input) => {
    input.addEventListener("change", () => {
      const moduleId = input.dataset.moduleToggle;
      if (input.checked) showModule(moduleId);
      else hideModule(moduleId);
    });
  });
}

function initializeDashboard() {
  metricMode = localStorage.getItem(METRIC_MODE_STORAGE_KEY) === "article" ? "article" : "overview";
  initTheme();
  initGrid();
  initSidebar();
  bindInteractions();
  refreshDashboard();
  window.setInterval(refreshDashboard, 15000);
}

initializeDashboard();
