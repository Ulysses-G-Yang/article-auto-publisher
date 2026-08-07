# 自动化文章发布 MCP API 接口文档

> 文档版本：`1.0.0`  
> Server ID：`content.article-publisher`  
> 工具数量：`12`  
> 适用平台：ZOL、小黑盒

## 1. 接入信息

| 项目 | 值 |
| --- | --- |
| 协议 | MCP Streamable HTTP |
| MCP URL（当前内网） | `http://10.0.0.28:8765/mcp` |
| 健康检查 | `http://10.0.0.28:8765/healthz` |
| 网络范围 | 仅办公内网/可信局域网 |
| 内部 Flask 地址 | `http://127.0.0.1:5000` |

5000 是 MCP Adapter 调用的内部 Flask 业务服务端口，不交给 CS_Admin 或外部测试人员。外部调用只使用 8765 的 MCP URL。若本机内网 IP 变化，只需替换 URL 中的 IP，并同步更新 `MCP_ALLOWED_HOSTS`。

健康检查：

```powershell
Invoke-RestMethod -Uri 'http://10.0.0.28:8765/healthz' -TimeoutSec 5
```

预期结果：

```json
{
  "status": "ok",
  "server_id": "content.article-publisher",
  "version": "1.0.0"
}
```

## 2. CS_Admin 导入 JSON

将 [mcp_server/registration.json](mcp_server/registration.json) 的内容导入 CS_Admin。当前文件指向本机内网地址 `10.0.0.28:8765`；换网络后先改 IP，再导入。

```json
{
  "mcpServers": {
    "文章自动发布": {
      "type": "streamable_http",
      "url": "http://10.0.0.28:8765/mcp",
      "tool_prefix": "content_article_publisher",
      "include_instructions": true,
      "timeout": 10,
      "read_timeout": 300,
      "description": "上传 docx 文章并自动发布到 ZOL 和小黑盒平台，支持账号登录管理、任务状态查询和发布日志查看"
    }
  }
}
```

导入后还需要在 CS_Admin 中执行“同步工具”，再按角色授权。MCP 服务不接收 CS_Admin 用户登录 Token，也不返回 Cookie、密钥或本地绝对路径。

## 3. 客户端调用方式

建议使用官方 MCP SDK 完成初始化、`tools/list` 和 `tools/call`，不要把 `/mcp` 当成普通 REST 接口：

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://10.0.0.28:8765/mcp"

async def main():
    async with streamable_http_client(MCP_URL) as (read, write, _session_id):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_accounts", {})
            if result.isError:
                raise RuntimeError(result.content)
            print(info.serverInfo, len(tools.tools), result.structuredContent)

asyncio.run(main())
```

## 4. 通用返回与错误

每次工具调用成功或失败都包含：

- `server_id`：固定为 `content.article-publisher`。
- `request_id`：本次调用的 32 位十六进制追踪标识。
- 业务字段：列表通常包含 `count`、`empty`、`items`；启动类包含 `task_id` 和 `async_task`。

错误结果的 `error.code` 使用以下枚举：

| 错误码 | 含义 | 建议处理 |
| --- | --- | --- |
| `INVALID_ARGUMENT` | 参数或业务前置条件不合法 | 修正参数，不要原样重试 |
| `NOT_FOUND` | 文章、任务或资源不存在 | 先重新查询列表 |
| `RESOURCE_RESTRICTED` | 文件白名单、权限或资源范围受限 | 检查 CS_Admin 文件服务和授权 |
| `UNAVAILABLE` | Flask 或文件服务不可用 | 检查服务后稍后重试 |
| `TIMEOUT` | 请求或文件传输超时 | 查询异步任务，避免重复创建 |
| `INTERNAL_ERROR` | MCP 内部异常 | 用 `request_id` 查脱敏日志 |

## 5. 异步任务约定

登录和文章发布不会在一次工具调用中等待浏览器结束：

1. 调用 `start_login` 或 `publish_article`，获得 MCP `task_id`。
2. 按返回的 `async_task.poll_tool` 调用 `get_login_result` 或 `get_publish_result`。
3. 登录任务每 3 秒轮询，发布任务每 10 秒轮询。
4. `status=completed` 读取 `result`；`status=failed` 读取脱敏错误；`status=awaiting_user_action` 按消息完成扫码或社区/话题选择。
5. MCP task_id 与 Flask task_id 持久化到 SQLite，MCP 重启后可以继续查询。

发布任务状态包括：`pending`、`running`、`awaiting_user_action`、`completed`、`failed`。小黑盒缺少社区或话题时不会误报成功，必须调用 `resume_task` 后再继续。

## 6. 文件传输约定

`publish_article` 不接收本地路径，只接收 CS_Admin 文件服务生成的临时 `source_download_url` 和可选平台列表。文件服务主机必须配置在 `MCP_FILE_SERVICE_ALLOWED_HOSTS` 白名单内；不允许 URL 账号密码、`file://`、任意开放网址抓取或跨白名单重定向。

单文件最大 50 MB，下载超时 30 秒，处理完成后临时文件自动删除。

## 7. 安全边界

MCP 不暴露任意命令、SQL、本地路径、Cookie 内容、浏览器 Profile、密码或 Token。`cleanup_locks` 只清理 Chrome Profile 的 Singleton 锁文件，不杀 Chrome 进程、不删除 Cookie。

本项目保留 `start_login`、`logout_account` 两个账号环境管理工具，因为发布前必须由管理员完成浏览器扫码和账号切换；这些工具只返回状态，不返回凭据。公开发布仍由现有 Flask 平台流程控制，本 MCP 不提供任意平台或任意 URL 发布能力。

## 8. 工具总览

| 分类 | 工具 |
| --- | --- |
| 查询 | `list_accounts`、`list_articles`、`list_tasks`、`get_task_logs`、`get_queue_status` |
| 登录 | `start_login`、`get_login_result` |
| 发布 | `publish_article`、`get_publish_result`、`resume_task` |
| 账号与环境 | `logout_account`、`cleanup_locks` |

所有工具的输入 Schema 都是闭合对象：`additionalProperties: false`。

## 9. 完整工具接口

### `list_accounts`

无参数。返回两个平台的 `platform`、`status`、`last_login_time`。状态：`logged_in`、`logged_out`、`logging_in`、`login_failed`。

### `list_articles`

无参数。返回文章标题、文件名、关键词、字数、图片数和关联任务；不返回原始本地路径。

### `list_tasks`

无参数。返回任务 ID、平台、状态、标题、社区/话题选择结果、草稿地址和脱敏错误信息。

### `get_task_logs`

参数：

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| `task_id` | integer，正整数 | 是 |

### `get_queue_status`

无参数。返回 `queue_size`、`running`、`pending`。

### `start_login`

启动指定平台的浏览器登录流程，立即返回异步任务。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `platform` | `zol` 或 `xiaoheihe` | 是 | 平台 |
| `force` | boolean | 否 | 默认 `false`；为 `true` 时先清理该平台 Cookie |

### `get_login_result`

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| `task_id` | string | 是 |

返回 `awaiting_user_action`、`completed` 或 `failed`。

### `publish_article`

下载临时 DOCX 并创建平台发布任务，立即返回异步任务。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source_download_url` | string | 是 | 白名单文件服务的 HTTP(S) 地址 |
| `platforms` | array | 否 | `zol`、`xiaoheihe`，默认两个平台 |

### `get_publish_result`

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| `task_id` | string | 是 |

返回 `pending`、`running`、`awaiting_user_action`、`completed` 或 `failed`。

### `resume_task`

恢复暂停或等待选择的 Flask 发布任务。小黑盒人工选择时应同时提供社区和话题。

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| `task_id` | integer，正整数 | 是 |
| `community` | string | 否 |
| `topic` | string | 否 |

### `logout_account`

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| `platform` | `zol` 或 `xiaoheihe` | 是 |

清除指定平台 Cookie 并将账号状态重置为 `logged_out`，不影响另一个平台。

### `cleanup_locks`

无参数。返回 `cleaned_lock_files`；只清理 Singleton 锁文件，不杀进程。

## 10. 运行与交付

在项目根目录启动：

```powershell
$env:FLASK_BASE_URL = 'http://127.0.0.1:5000'
$env:MCP_BIND_HOST = '10.0.0.28'
$env:MCP_PORT = '8765'
$env:MCP_ALLOWED_HOSTS = '10.0.0.28'
$env:MCP_FILE_SERVICE_ALLOWED_HOSTS = '<CS_Admin文件服务Host>'
python -m mcp_server.server
```

交给调用和测试人员：

1. 本文件 `MCP_API_REFERENCE.md`。
2. [mcp_server/registration.json](mcp_server/registration.json)。
3. MCP URL：`http://10.0.0.28:8765/mcp`。

不要交付 `.env`、数据库、日志、Chrome Profile、Cookie、备份或任何适配器密钥。若测试人员不在同一内网，不能直接使用此地址，需要先建立 VPN 或受控反向代理，并重新配置 Host 白名单。
