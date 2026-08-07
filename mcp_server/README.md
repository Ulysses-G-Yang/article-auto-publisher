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

先启动现有 Flask 服务（内部地址 `http://127.0.0.1:5000`），再在项目根目录执行：

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

如需办公内网访问，应绑定内网 IP（或受控环境使用 `0.0.0.0`），并把实际访问 Host 加入 `MCP_ALLOWED_HOSTS`，同时配置防火墙来源白名单。禁止将 8765 端口直接暴露到公网。Host 白名单支持裸域名并自动允许其端口，例如 `collector.mcp.example.com` 会允许 `collector.mcp.example:<port>`。经受控反向代理访问时，将代理源 IP 配置到 `MCP_TRUSTED_PROXY_IPS`，再把业务域名加入 `MCP_ALLOWED_HOSTS`。

## 工具清单

### 查询工具

- `list_accounts`：查询 ZOL、小黑盒登录状态和上次登录时间。
- `list_articles`：查询文章标题、关键词、字数、图片数和关联任务。
- `list_tasks`：查询发布任务状态。
- `get_task_logs`：查询单个任务日志。
- `get_queue_status`：查询 Flask 队列状态。

### 异步工具

- `start_login(platform, force)` → `get_login_result(task_id)`：打开本机登录页面，等待人工扫码。
- `publish_article(source_download_url, platforms)` → `get_publish_result(task_id)`：下载 CS_Admin 注入的 DOCX 临时文件并创建 Flask 发布任务。

### 管理工具

- `resume_task(task_id, community, topic)`：恢复暂停或需要手动选择的小黑盒任务。
- `logout_account(platform)`：清理指定平台 Cookie 并重置状态。
- `cleanup_locks`：只清理 Chrome Profile 锁文件，不杀进程、不删除 Cookie。

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
