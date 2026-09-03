# AI 发布建议（Phase 4）

本阶段只提供**只读建议**：读取当前 Content Studio 草稿的标题和完整可见正文，
向 DeepSeek 请求每个平台的社区、话题和关键词建议。结果不会自动修改草稿、创建投递计划、
保存平台草稿或执行公开发布；平台候选与建议仍需由调用方核对。

## 网页配置

打开 `/settings/ai`，可以填写：

- 是否启用 AI 发布建议；
- DeepSeek API 地址；
- 模型 ID；
- API Key。

“保存并测试连接”会先应用当前表单，再请求配置地址的 `/models`，确认密钥有效且模型存在；
不会发送文章正文。页面不回显已有 Key，保存成功后会立即清空密码输入框。

网页输入只保存在当前 Python 服务进程内，不写文件、数据库、Git 或日志。服务重启后，
运行期配置会消失并回退到以下环境变量。当前项目尚未实现真正的 Web 管理员登录，
所以该页面只应部署在可信单用户网络；CSRF 和侧边栏隐藏不能代替鉴权。

## 环境变量配置

默认关闭。只在启动 ArticleOps 的外部 PowerShell 会话中设置：

```powershell
$env:ARTICLEOPS_AI_GUIDANCE_ENABLED = "true"
$env:DEEPSEEK_API_KEY = "在本地环境变量中设置，不要写入仓库"
```

可选配置（均有默认值）：

```powershell
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
$env:DEEPSEEK_CONNECT_TIMEOUT_SECONDS = "10"
$env:DEEPSEEK_READ_TIMEOUT_SECONDS = "90"
$env:DEEPSEEK_MAX_OUTPUT_TOKENS = "2000"
```

`DEEPSEEK_BASE_URL` 启用时必须是无凭据、查询参数和片段的 HTTPS 地址。网页运行期 Key
优先于 `DEEPSEEK_API_KEY`；清除网页 Key 后自动回退环境变量。API Key 不会进入
`DEFAULT_CONFIG`、YAML 配置对象、日志、数据库或响应。接口不自动重试；认证失败、
限流、超时、模型不存在和非法响应会返回稳定中文错误。

## 设置接口

```text
GET  /api/settings/publication-ai
PUT  /api/settings/publication-ai
POST /api/settings/publication-ai/test
```

读取接口只返回启用状态、地址、模型、`api_key_configured`、密钥来源和 `process` 持久化说明；
永远不返回 Key。写入与连接测试由 `/settings/ai` 页面携带同一会话的 CSRF 头，所有响应都
使用 `Cache-Control: no-store`。

## 创作工作台

在 `/upload` 选择至少一个可投递平台后点击“AI 分析发布建议”。页面会先同步当前草稿，
再把当前修订的完整可见正文发送给 DeepSeek。结果只展示建议社区、话题、搜索词、关键词和理由：

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
