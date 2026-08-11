# 小黑盒单链路 MVP 架构

新入口位于 `src/article_mvp`。根目录原有 Flask、MCP、发布器以及
`D:\Backup\Documents\text-restored` 均不是新包的运行时依赖。

阶段一数据流：

```text
PublishRequest
  -> XiaoheihePublisher
  -> POST 响应捕获
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

## 运行入口

```powershell
$env:PYTHONPATH = "src"
python -m article_mvp.tools.init_db
python -m article_mvp.tools.probe_xhh
python -m article_mvp.tools.run_dashboard
```

看板默认监听 `http://127.0.0.1:5100`，只展示映射、归一化指标、
采集运行状态和接口证据等级，不返回原始平台响应或登录材料。

公开发布属于人工验收门，不能加入自动测试：

```powershell
$env:ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH = "true"
python -m article_mvp.tools.publish_xhh request.json --confirm-publication
```
