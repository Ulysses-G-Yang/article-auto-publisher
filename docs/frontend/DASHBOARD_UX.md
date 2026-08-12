# ArticleOps 前端结构与验收

## 页面结构

应用继续使用原 Flask 服务和 5000 端口。发布概览、创建文章、平台账号、任务详情和数据中心共用 ArticleOps 的深色侧边栏、顶栏、页面标题、卡片、表格、表单、状态标签与响应式规则。旧页面仍使用原有 Alpine.js、Jinja 变量和 Fetch 接口。

数据中心继续使用 CoreUI 与 GridStack。顶部摘要是可拖动、可隐藏、可恢复的 `summary` 模块，其余工作流、接口契约、文章映射和采集运行模块保持可访问。

## 指标模式和卡片语义

顶部只提供两个模式：

- 运营总览：汇总所有真实文章最新快照。
- 单篇文章：默认选择排序后的最新文章，也可通过选择器切换。

两种模式固定使用四张卡：基础信息、流量表现、互动表现、传播与采集。模式保存在浏览器 `localStorage`；单篇文章的原始链接使用新标签页、安全 `rel` 属性和可访问名称。

## 可空字段处理

不存在或为 `null` 的指标显示“—”。没有快照时显示“尚未采集 / 暂无数据”，不会将缺失值转成 `0`。`created_at` 只用于文章排序的兼容退化和映射表记录，不显示为正式发布时间。数据新鲜度只由 `latest_metric.snapshot_time` 计算。

仍等待后端正式提供以下可空字段：

- `articles[].title`
- `articles[].published_at`
- `articles[].latest_metric.exposure_count`
- `articles[].latest_metric.share_count`

## 本地验收

1. 使用项目既有 Python 环境启动 Flask 服务，确认仍监听 `http://127.0.0.1:5000`。
2. 依次访问 `/`、`/upload`、`/accounts`、已有的 `/task/<id>` 与 `/data-center/`。
3. 在数据中心切换“运营总览 / 单篇文章”，切换文章，刷新页面验证模式保留。
4. 进入编辑看板，拖动模块、隐藏模块、保存布局并恢复默认。
5. 在 1440、1024、390 像素宽度检查侧边栏、表格、卡片与表单；确认无横向页面溢出，键盘焦点清晰。
6. 打开浏览器控制台，确认没有前端错误。验收空状态时不要向数据库写入模拟业务数据，也不要触发上传或真实平台发布。

自动测试：

```powershell
python -m pytest tests/frontend tests/test_article_mvp.py tests/test_regression.py -q
node --check src/article_mvp/web/static/dashboard.js
node --check web/static/js/app.js
```
