# 自动化文章发布 MCP Server

这是现有 Flask 自动化文章发布工具的 MCP Adapter。MCP Server 不重复实现 DOCX 解析、队列、浏览器自动化或平台发布逻辑；所有业务操作都通过 Flask REST API 完成。

## CS_Admin 导入 JSON

在 CS_Admin → AI → MCP 管理 → 导入 JSON 中粘贴：

```json
{
  "mcpServers": {
    "文章自动发布": {
      "type": "streamable_http",
      "url": "http://10.0.0.28:8765/mcp",
      "headers": {},
      "tool_prefix": "content_article_publisher",
      "include_instructions": true,
      "timeout": 10,
      "read_timeout": 300,
      "description": "上传 docx 文章并自动发布到 ZOL 和小黑盒平台，支持账号登录管理、任务状态查询和发布日志查看"
    }
  }
}
```

导入配置只创建连接；在 CS_Admin 中仍需执行“同步工具”并配置用户/角色授权。

## 安装与启动

第二台 Windows 电脑推荐先在项目根目录执行自动初始化脚本：

```powershell
.\scripts\setup_windows.ps1
.\scripts\start_production_windows.ps1
```

脚本会自动创建 Python 3.12 环境、安装依赖、创建空运行目录并生成被 Git 忽略的生产配置；不会复制测试数据库、Cookie 或 Chrome Profile。Git/ GitHub SSH 用于克隆模式；源码压缩包模式不要求 Git，但 Conda 和 Google Chrome 仍需要预先安装。

手动启动时，先启动现有 Flask 服务（内部地址 `http://127.0.0.1:5000`），再在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python -m mcp_server.server
```

内网部署示例监听：

```text
MCP:     http://10.0.0.28:8765/mcp
Health:  http://10.0.0.28:8765/healthz
```

`5000` 是 Flask 业务服务端口，只供 MCP Adapter 在本机调用；调用方和 CS_Admin 只连接 MCP 的 `8765/mcp`。

停止服务：在运行窗口按 `Ctrl+C`。如果使用后台进程，先定位该 MCP Python 进程的 PID，再执行 `Stop-Process -Id <PID>`；不要结束 Chrome 进程。

## 配置

```text
FLASK_BASE_URL=http://127.0.0.1:5000
MCP_PORT=8765
MCP_ALLOWED_HOSTS=10.0.0.28
MCP_FILE_SERVICE_ALLOWED_HOSTS=dev.sccsai.com
```

可选配置：

```text
MCP_BIND_HOST=10.0.0.28
MCP_TASK_DB=<项目 data 目录下的 MCP 任务数据库>
MCP_TRUSTED_PROXY_IPS=<受控反向代理IP，可选>
```

账号级只读 MCP 工具另需在运行 Flask 的同一进程环境中配置：

```text
ARTICLEOPS_MCP_INTERNAL_TOKEN=<至少 32 个字符的随机值>
ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS=<允许读取的 account_id，逗号分隔>
MCP_LEGACY_MUTATIONS_ENABLED=false
```

两项缺一都会使内部账号接口以 `MCP_ACCESS_NOT_CONFIGURED` 失败。token 只在
MCP Adapter 到 Flask 的两条 `/api/internal/mcp/...` 请求中发送，不会附加到
旧 REST 接口，也不会写入日志、错误响应或工具结果；账号白名单只在 Flask 端
解释和执行。请通过安全的进程环境注入，不要写进仓库配置文件。

`MCP_LEGACY_MUTATIONS_ENABLED` 只有 `true`、`1`、`yes`、`on`（忽略大小写）
会开启，默认关闭。它是旧平台级登录、DOCX 发布任务、恢复任务、退出账号和锁
清理工具的紧急兼容开关，不属于新的账号级工作流；生产环境建议保持 `false`。

如需办公内网访问，应绑定内网 IP（或受控环境使用 `0.0.0.0`），并把实际访问 Host 加入 `MCP_ALLOWED_HOSTS`，同时配置防火墙来源白名单。禁止将 8765 端口直接暴露到公网。Host 白名单支持裸域名并自动允许其端口，例如 `collector.mcp.example.com` 会允许 `collector.mcp.example:<port>`。经受控反向代理访问时，将代理源 IP 配置到 `MCP_TRUSTED_PROXY_IPS`，再把业务域名加入 `MCP_ALLOWED_HOSTS`。

## 工具清单

### 查询工具

- `list_platform_accounts(platform, usable)`：读取白名单内指定平台的公开账号投影；这是新的账号级只读入口，不触发登录、验证、退出、清 Cookie 或投递。
- `get_account_activity(account_id, limit)`：读取白名单内单个账号的脱敏活动日志；这是新的账号级只读入口。
- `[LEGACY] list_accounts`：查询 ZOL、小黑盒登录状态和上次登录时间；旧兼容工具不属于新的账号级工作流。
- `list_articles`：查询文章标题、关键词、字数、图片数和关联任务。
- `list_tasks`：查询发布任务状态。
- `get_task_logs`：查询单个任务日志。
- `get_queue_status`：查询 Flask 队列状态。

### 异步工具

- `[LEGACY MUTATION] start_login(platform, force)` → `get_login_result(task_id)`：旧登录入口，默认关闭。
- `[LEGACY MUTATION] publish_article(source_download_url, platforms)` → `get_publish_result(task_id)`：旧 DOCX 发布入口，默认关闭。

### 管理工具

- `[LEGACY MUTATION] resume_task(task_id, community, topic)`：旧任务恢复入口，默认关闭。
- `[LEGACY MUTATION] logout_account(platform)`：旧账号退出入口，默认关闭。
- `[LEGACY MUTATION] cleanup_locks`：旧 Profile 锁清理入口，默认关闭。

所有异步工具都返回 `cs-admin-async-task/v1` 的 `async_task` 信息。MCP Server 会在 SQLite 中持久化 MCP task id 与 Flask task id 的映射，重启后仍可继续轮询。

## 文件下载边界

`publish_article` 的 `source_download_url` 只用于本次 CS_Admin 注入的 HTTP(S) 临时文件地址，不提供通用 URL 抓取工具；不接受 `file://`、账号密码 URL 或本地绝对路径。文件下载超时为 30 秒，大小上限为 50 MB，处理完成后临时文件自动删除。

返回结果不会包含 Cookie、密钥、绝对路径、完整配置或本地原始文件内容。

## 故障排查

1. `GET /healthz` 返回 `status=ok`，说明 MCP 进程和 Host 校验已启动。
2. 如果工具返回 `UNAVAILABLE`，确认 Flask 服务已在 `FLASK_BASE_URL` 启动。
3. 如果出现 `421 Invalid Host header`，把 CS_Admin 实际访问 MCP 使用的域名加入 `MCP_ALLOWED_HOSTS` 后重启 MCP Server；反向代理场景同时配置 `MCP_TRUSTED_PROXY_IPS`。
4. 如果发布任务返回 `awaiting_user_action`，按消息完成扫码或填写社区/话题，再让 CS_Admin 继续轮询或调用 `resume_task`。

## 交付文件

- [MCP_API_REFERENCE.md](../MCP_API_REFERENCE.md)：给调用、测试和 CS_Admin 管理员的接口规范。
- [registration.json](registration.json)：可直接导入 CS_Admin 的单 MCP 配置。
