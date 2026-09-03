# AI 发布建议（Phase 4）

本阶段只提供**只读建议**：读取当前 Content Studio 草稿的标题和完整可见正文，
向 DeepSeek 请求每个平台的社区、话题和关键词建议。结果不会自动修改草稿、创建投递计划、
保存平台草稿或执行公开发布；平台候选与建议仍需由调用方核对。

## 配置

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

`DEEPSEEK_BASE_URL` 启用时必须是无凭据的 HTTPS 地址。API Key 只由客户端读取
`DEEPSEEK_API_KEY`，不会进入 `DEFAULT_CONFIG`、YAML 配置对象、日志、数据库或响应。
接口不自动重试；认证失败、限流、超时和非法响应会返回稳定错误码。

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
