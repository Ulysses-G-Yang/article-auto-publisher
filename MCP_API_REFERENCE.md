# 自动化文章发布 MCP API 接口文档

> 文档版本：`1.1.0`
> Server ID：`content.article-publisher`
> 工具数量：`16`
> 草稿平台：小黑盒、ZOL、知乎、微博、什么值得买、百家号

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
  "version": "1.1.0"
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
      "description": "将受控 DOCX 保存到 ArticleOps 已验证的平台账号草稿，并查询账号状态、投递结果和脱敏日志；不开放公开发布"
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
| `REQUEST_KEY_CONFLICT` | 同一 client_request_id 被用于不同内容或目标 | 使用原参数查询，或为新的人工意图生成新 ID |
| `SUBMISSION_RESULT_UNKNOWN` | 提交超时或中断，无法证明是否已创建平台操作 | 人工核对，禁止自动重试 |
| `INTERNAL_ERROR` | MCP 内部异常 | 用 `request_id` 查脱敏日志 |

## 5. 异步任务约定

当前草稿投递和旧兼容流程都使用异步协议：

1. 调用 `start_article_draft_delivery`，获得 MCP `task_id`。
2. 按返回的 `async_task.poll_tool` 调用 `get_article_draft_delivery_result`。
3. 草稿投递每 10 秒轮询；查询工具幂等，不会重复创建计划或平台操作。
4. `status=completed` 读取 `result`；`status=failed` 读取脱敏错误；`status=awaiting_user_action` 按消息完成扫码或社区/话题选择。
5. MCP task_id 与 Content Studio plan id 持久化到 SQLite，MCP 重启后可以继续查询。

发布任务状态包括：`pending`、`running`、`awaiting_user_action`、`completed`、`failed`。小黑盒缺少社区或话题时不会误报成功，必须调用 `resume_task` 后再继续。

## 6. 文件传输约定

`start_article_draft_delivery` 不接收本地路径，只接收 CS_Admin 文件服务生成的临时
`source_download_url` 和账号目标。文件服务主机必须配置在
`MCP_FILE_SERVICE_ALLOWED_HOSTS` 白名单内；不允许 URL 账号密码、`file://`、任意
开放网址抓取或跨白名单重定向。

单文件最大 50 MB，下载超时 30 秒，处理完成后临时文件自动删除。

## 7. 安全边界

MCP 不暴露任意命令、SQL、本地路径、Cookie 内容、浏览器 Profile、密码或 Token。`cleanup_locks` 只清理 Chrome Profile 的 Singleton 锁文件，不杀 Chrome 进程、不删除 Cookie。

### 7.1 账号级访问边界

新的查询和草稿工具只通过 Flask 的内部
`/api/internal/mcp/...` 端点工作。Flask 端要求 `Authorization: Bearer <token>`，
并以 `ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS` 做账号级白名单；token 至少 32 个字符，
缺少 token 或白名单时返回 `MCP_ACCESS_NOT_CONFIGURED`，token 错误返回
`MCP_ACCESS_DENIED`。活动 `limit` 必须在 1 到 200 之间。

MCP Adapter 只把 token 注入上述内部请求，绝不附加到旧 REST 请求。返回值
只允许 `public_account` 和 `list_activity` 的公开字段，并在 MCP 边界再次脱敏；
禁止 profile_path、原始 platform_user_id、Cookie、Token。设置
`ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED=true` 后只额外授予白名单账号
`draft.create`，不授予 `publish.request` 或 `publish.execute`。旧的登录、退出、清理、
发布工具保留作兼容，均标记为 `LEGACY`，不能替代新的只读入口。

### 7.2 旧变更工具开关

旧平台级变更工具 `start_login`、`publish_article`、`resume_task`、
`logout_account`、`cleanup_locks` 保留注册和输入 Schema 以兼容旧客户端，但默认
在执行任何 Flask 请求、文件下载、MCP task 写入或清理动作前返回
`LEGACY_MCP_MUTATIONS_DISABLED`。仅在明确的紧急兼容场景设置：

```text
MCP_LEGACY_MUTATIONS_ENABLED=true
```

只有 `true/1/yes/on`（忽略大小写）开启，其他值和缺失值均关闭。该开关不属于
新的账号级只读工作流，生产环境建议保持 `false`；`get_login_result`、
`get_publish_result` 与旧查询工具仍可用于读取已有任务状态。

本项目保留 `start_login`、`logout_account` 两个 `LEGACY` 账号环境管理工具，因为发布前必须由管理员完成浏览器扫码和账号切换；这些工具只返回状态，不返回凭据。公开发布仍由现有 Flask 平台流程控制，本 MCP 不提供任意平台或任意 URL 发布能力。

## 8. 工具总览

| 分类 | 工具 |
| --- | --- |
| 当前草稿投递 | `start_article_draft_delivery`、`get_article_draft_delivery_result` |
| 账号级查询 | `list_platform_accounts`、`get_account_activity` |
| 查询 | `list_accounts`、`list_articles`、`list_tasks`、`get_task_logs`、`get_queue_status` |
| 登录 | `start_login`、`get_login_result` |
| 发布 | `publish_article`、`get_publish_result`、`resume_task` |
| 账号与环境 | `logout_account`、`cleanup_locks` |

所有工具的输入 Schema 都是闭合对象：`additionalProperties: false`。

## 9. 完整工具接口

### `start_article_draft_delivery`

把一个 CS_Admin 临时 DOCX 保存到一个或多个已授权账号的“平台草稿”。不接受
`mode` 字段，因此无法请求公开发布。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source_download_url` | string | 是 | CS_Admin 注入的白名单临时下载地址 |
| `targets` | array | 是 | 1～50 个 `{platform, account_id, persist_login?}` 草稿目标；账号不能重复 |
| `client_request_id` | string | 是 | 8～128 位稳定幂等键；同一业务意图重复调用必须保持不变 |

`platform` 只允许小黑盒、ZOL、知乎、微博、什么值得买和百家号。首次调用会先持久化
MCP 任务，再下载 DOCX、冻结 ContentVersion、创建 DeliveryPlan 并异步执行。网络
超时返回 `SUBMISSION_RESULT_UNKNOWN`，相同请求不会再次提交平台操作。

### `get_article_draft_delivery_result`

参数 `task_id` 为启动工具返回的 MCP task id。返回统一的 `pending`、`running`、
`completed` 或 `failed`，以及逐平台 `status`、草稿地址或脱敏错误。该工具只读取并
对账现有计划，符合 `cs-admin-async-task/v1`，不会产生新的草稿副作用。

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
$env:MCP_FILE_SERVICE_ALLOWED_HOSTS = 'dev.sccsai.com'
$env:ARTICLEOPS_MCP_INTERNAL_TOKEN = '<单独生成的至少32位随机值>'
$env:ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS = '<获准投递的account_id，逗号分隔>'
$env:ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED = 'true'
python -m mcp_server.server
```

交给调用和测试人员：

1. 本文件 `MCP_API_REFERENCE.md`。
2. [mcp_server/registration.json](mcp_server/registration.json)。
3. MCP URL：`http://10.0.0.28:8765/mcp`。

不要交付 `.env`、数据库、日志、Chrome Profile、Cookie、备份或任何适配器密钥。若测试人员不在同一内网，不能直接使用此地址，需要先建立 VPN 或受控反向代理，并重新配置 Host 白名单。
