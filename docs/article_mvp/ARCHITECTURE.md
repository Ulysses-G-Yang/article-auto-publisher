# 小黑盒单链路 MVP 架构

新数据模块位于 `src/article_mvp`，由现役 Flask 服务以 Blueprint 方式挂载到
`/data-center/`。依赖方向只能是现役入口调用新包；新包不得反向导入根目录的
`core`、`models`、`web`、MCP 或平台发布器。

尚未交付的重型老项目以及 `D:\Backup\Documents\text-restored` 均不是运行时
依赖，不得加入 `PYTHONPATH`、安装或执行。

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
```

看板不再启动第二个端口。启动现役服务后访问
`http://127.0.0.1:5000/data-center/`。页面同时展示现役发布任务的脱敏只读摘要、
新映射、归一化指标、采集运行状态和接口证据等级，不返回原始平台响应、文章
正文或登录材料。

公开发布属于人工验收门，不能加入自动测试：

```powershell
$env:ARTICLE_MVP_ALLOW_PUBLIC_PUBLISH = "true"
python -m article_mvp.tools.publish_xhh request.json --confirm-publication
```
