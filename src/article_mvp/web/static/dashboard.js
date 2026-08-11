const byId = (id) => document.getElementById(id);

function text(id, value, fallback = "—") {
  byId(id).textContent = value ?? fallback;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function statusNode(value) {
  const span = document.createElement("span");
  span.className = `status ${value || ""}`;
  span.textContent = value || "—";
  return span;
}

function cell(row, value) {
  const td = document.createElement("td");
  if (value instanceof Node) td.appendChild(value);
  else td.textContent = value ?? "—";
  row.appendChild(td);
}

function renderArticles(articles) {
  const body = byId("articles-body");
  body.replaceChildren();
  byId("articles-empty").classList.toggle("hidden", articles.length > 0);
  for (const article of articles) {
    const row = document.createElement("tr");
    cell(row, statusNode(article.status));
    const articleCell = document.createElement("span");
    if (article.platform_url) {
      const link = document.createElement("a");
      link.href = article.platform_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = article.external_article_id;
      articleCell.appendChild(link);
    } else articleCell.textContent = article.external_article_id;
    cell(row, articleCell);
    const metric = article.latest_metric;
    cell(row, metric?.read_count);
    cell(row, metric?.like_count);
    cell(row, metric?.comment_count);
    cell(row, metric?.collect_count);
    cell(row, metric?.revenue);
    cell(row, formatTime(metric?.snapshot_time));
    body.appendChild(row);
  }
}

function renderRuns(runs) {
  const body = byId("runs-body");
  body.replaceChildren();
  byId("runs-empty").classList.toggle("hidden", runs.length > 0);
  for (const run of runs) {
    const row = document.createElement("tr");
    cell(row, run.id);
    cell(row, statusNode(run.status));
    cell(row, run.articles_processed);
    cell(row, run.error_type);
    cell(row, formatTime(run.started_at));
    cell(row, formatTime(run.finished_at));
    body.appendChild(row);
  }
}

async function refresh() {
  const alert = byId("alert");
  const button = byId("refresh");
  button.disabled = true;
  alert.classList.add("hidden");
  try {
    const response = await fetch("/api/dashboard", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || payload.error || "加载失败");
    text("total-articles", payload.summary.total_articles);
    text("mapped-articles", payload.summary.mapped_articles);
    text("total-snapshots", payload.summary.total_snapshots);
    text("latest-run", payload.summary.latest_run_status);
    text("publish-evidence", payload.contract.publish_evidence);
    text("publish-source", payload.contract.publish_source);
    text("collector-evidence", payload.contract.collector_evidence);
    text("collector-source", payload.contract.collector_source);
    const gate = byId("collector-gate");
    gate.textContent = payload.contract.collector_enabled ? "HTTPX ENABLED" : "COLLECTOR BLOCKED";
    gate.classList.toggle("ok", payload.contract.collector_enabled);
    renderArticles(payload.articles);
    renderRuns(payload.runs);
    text("last-updated", `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
  } catch (error) {
    alert.textContent = `看板加载失败：${error.message}`;
    alert.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

byId("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, 15000);
