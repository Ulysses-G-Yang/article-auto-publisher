# 小黑盒单链路 MVP 架构

新数据模块位于 `src/article_mvp`。现役 Flask 服务只挂载其只读查询 API，
不再提供内置数据中心页面。依赖方向只能是现役入口调用新包；新包不得反向导入根目录的
`core`、`models`、`web`、MCP 或平台发布器。

尚未交付的重型老项目以及 `D:\Backup\Documents\text-restored` 均不是运行时
依赖，不得加入 `PYTHONPATH`、安装或执行。

阶段一的新链路数据流：

```text
PublishRequest
  -> XiaoheihePublisher
  -> POST 响应捕获
  -> ArticlePublished（不携带 ORM Session）
  -> PublishedEventService（幂等持久化）
  -> PlatformArticle
  -> XiaoheiheCollector (HTTPX)
  -> MetricSnapshot + CollectionRun
```

## 安全边界

- 公开发布同时要求 `ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH=true` 和 CLI 参数
  `--confirm-publication`。
- 发布响应无法确认时禁止自动重发。
- Collector 只接受证据等级为 `verified` 的 Endpoint。
- Cookie 只在 Playwright 与 HTTPX 之间以内存对象传递。
- Network 探测只保存 URL 参数名、Header/Cookie 名称和脱敏 JSON 结构。
- 旧系统继续独占 `data/app.db`；新模块默认使用
  `data/article_mvp/article_mvp.db`，运行时禁止交叉查询。
- 小黑盒 Profile、探测记录和文件锁全部位于 `data/article_mvp/` 下。

## 只读 API 边界

现役 Flask 服务仍使用 5000 端口，并保留以下只读接口供独立数据看板接入：

- `/data-center/api/dashboard`：只查询 article_mvp 独立数据库。
- `/data-center/healthz`：只读数据 API 健康检查。

内置 `/data-center/` 页面已下线，旧书签会返回主站；`/api/legacy-summary`
继续作为脱敏只读兼容接口保留。新模块不导入旧系统的 `models`、`web`、`core`
或平台适配器，也不通过 bridge 读取 `app.db`。

## 一次性安全迁移

首次切换独立数据库时，先初始化目标库，再执行只复制迁移。工具只读源库、
按主键幂等复制三张表；目标存在同主键但内容不同时整次事务失败，不删除源数据。

```powershell
$env:PYTHONPATH = "src"
python -m article_mvp.tools.migrate_legacy_tables `
  --source data/app.db `
  --destination data/article_mvp/article_mvp.db
```

迁移范围仅为 `platform_articles`、`metric_snapshots` 和 `collection_runs`。

## 运行入口

```powershell
$env:PYTHONPATH = "src"
python -m article_mvp.tools.init_db
python -m article_mvp.tools.probe_xhh
```

独立数据看板可以读取 `/data-center/api/dashboard` 中的映射、归一化指标、
采集运行状态和接口证据等级。接口不返回原始平台响应、文章正文或登录材料。

公开发布属于人工验收门，不能加入自动测试：

```powershell
$env:ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH = "true"
python -m article_mvp.tools.publish_xhh request.json --confirm-publication
```
