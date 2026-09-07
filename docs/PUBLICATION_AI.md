# 生成平台建议与兼容服务配置（Phase 4）

本阶段只提供**只读生成平台建议**：读取当前 Content Studio 草稿的标题和完整可见正文，
向用户配置的 OpenAI 兼容服务请求每个平台的社区、话题和关键词建议。结果不会自动修改草稿、创建投递计划、
保存平台草稿或执行公开发布；平台候选与建议仍需由调用方核对。

## 网页配置

打开 `/settings/ai`，可以填写：

- 是否启用生成平台建议；
- OpenAI 兼容服务地址（支持 DeepSeek 官方服务、自建中转站和兼容服务）；
- 模型 ID；
- API Key。

模型 ID 始终可以手动输入。“读取模型列表”会使用当前表单中的服务地址，并可临时使用尚未
保存的 Key 请求配置地址的 `/models`；这次读取不会保存地址、Key 或模型，也不会发送文章正文。
服务不提供标准 `/models`、列表为空或请求失败时，只显示中文提示，手动填写和“保存设置”仍然可用。

“保存设置”只更新当前表单配置，不依赖 `/models` 成功。“测试连接”只测试**已经保存**的配置；
测试失败不会撤销配置。页面不回显已有 Key，保存成功后会立即清空密码输入框。

网页输入只保存在当前 Python 服务进程内，不写文件、数据库、Git 或日志。服务重启后，
运行期配置会消失并回退到本机环境变量。当前项目尚未实现真正的 Web 管理员登录，
所以该页面只应部署在可信单用户网络；CSRF 和侧边栏隐藏不能代替鉴权。

## 环境变量配置

默认关闭。Windows 生产运行复用现有 `data\production_env.ps1`，由管理员在本机维护；
现有 `scripts\launch_articleops_windows.ps1` 启动时按既有流程加载它，Web/MCP 进程继承
其中的环境变量。本轮不自动生成、覆写、迁移或检查真实 `data\production_env.ps1`，也不把
它复制进 Git。以下四个变量是运维人员需要关注的核心 AI 配置项，均保持注释状态直到
管理员按需在本机配置：

```powershell
# data\production_env.ps1（管理员本机配置；不要提交到仓库）
# $env:ARTICLEOPS_AI_GUIDANCE_ENABLED = "false"
# $env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# $env:DEEPSEEK_MODEL = "deepseek-v4-flash"
# $env:DEEPSEEK_API_KEY = "<local secret; never commit>"
# 可选的超时和输出限制（均有默认值）：
# $env:DEEPSEEK_CONNECT_TIMEOUT_SECONDS = "10"
# $env:DEEPSEEK_READ_TIMEOUT_SECONDS = "90"
# $env:DEEPSEEK_MAX_OUTPUT_TOKENS = "2000"
```

实际启用 AI 时，管理员必须在本机配置中将 `ARTICLEOPS_AI_GUIDANCE_ENABLED` 设为 `"true"`；
示例中的注释 `"false"` 只是安全占位，不会启用 AI。

`DEEPSEEK_BASE_URL` 启用时必须是无凭据、查询参数和片段的 HTTPS 地址。网页运行期 Key
优先于 `DEEPSEEK_API_KEY`；清除网页 Key 后自动回退环境变量。长期 Key 通过本机环境注入；
网页输入的临时 Key 只在当前 Python 进程内存中生效，不会进入 `DEFAULT_CONFIG`、YAML 配置对象、
Git、日志、数据库或响应。管理员应使用
Windows 文件 ACL 将本机配置限制为管理员和运行账号；该文件是明文配置，不是加密金库，
不要把它描述为安全存储。接口不自动重试；认证失败、限流、超时、模型不存在和非法响应
会返回稳定中文错误。

页面的“保存设置”只是当前 Python 进程的临时覆盖；服务重启后回到本机环境变量。将配置
永久保存在网页或数据库中需要另行设计管理员权限，本轮不实现。

## 设置接口

```text
GET  /api/settings/publication-ai
POST /api/settings/publication-ai/models
PUT  /api/settings/publication-ai
POST /api/settings/publication-ai/test
```

读取接口只返回启用状态、地址、模型、`api_key_configured`、密钥来源和 `process` 持久化说明；
永远不返回 Key。写入与连接测试由 `/settings/ai` 页面携带同一会话的 CSRF 头，所有响应都
使用 `Cache-Control: no-store`。

模型列表请求体：

```json
{
  "base_url": "https://your-ai-service.example/v1",
  "api_key": "可选的一次性页面输入"
}
```

`api_key` 为空时复用当前服务进程或环境变量中的 Key；非空时只用于本次模型列表请求。
响应只包含去重后的模型 ID、数量和检查时间，不回显 Key。服务地址或页面 Key 改变后，
前端会立即清空旧模型列表，防止误选另一个服务的模型。

## 创作工作台

在 `/upload` 选择至少一个可投递平台后点击“生成平台建议”。页面会先同步当前草稿，
再把当前修订的完整可见正文发送给已配置的 AI 服务。结果只展示建议社区、话题、搜索词、关键词和理由：

- 不自动修改草稿；
- 不自动勾选平台、账号或话题；
- 不创建投递计划；
- 不保存平台草稿；
- 不执行公开发布；
- 分析期间文章发生修改时，旧建议会被丢弃。

## 接口

```http
POST /api/content-drafts/{draft_id}/publication-advice
Content-Type: application/json
```

请求体只包含当前草稿修订号和目标平台（既有八个平台，重复平台会按首次出现顺序去重）：

```json
{
  "revision": 3,
  "platforms": ["xiaoheihe", "zol", "zhihu"]
}
```

成功响应只返回建议，不回显文章正文、提示词、API Key 或原始上游响应：

```json
{
  "draft_id": "draft-uuid",
  "revision": 3,
  "model": "deepseek-v4-flash",
  "recommendations": [
    {
      "platform": "xiaoheihe",
      "topic_queries": ["显示器"],
      "suggested_topics": ["显示器选购"],
      "suggested_community": "硬件交流",
      "keywords": ["显示器", "桌面"],
      "reason": "文章主题与该平台的硬件讨论场景匹配。"
    }
  ],
  "generated_at": "2026-09-03T00:00:00+00:00"
}
```

接口要求修订号与当前草稿一致；正文超过 200000 个字符、AI 未启用、缺少 Key、
上游认证失败、限流、超时或返回内容不符合严格 JSON 契约时均不会产生任何写入副作用。
