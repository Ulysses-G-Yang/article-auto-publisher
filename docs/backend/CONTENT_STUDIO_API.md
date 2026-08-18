# Content Studio API 契约 v1

本契约服务于统一的 `/upload`“创作与投递”工作台。内容域使用独立 SQLite
数据库；旧文章和旧任务只读，账号身份仍由 `account_sessions` 域负责。

## 草稿响应

```json
{
  "draft_id": "uuid",
  "source_type": "BLANK | DOCX | LEGACY_ARTICLE | SYSTEM_SEED",
  "source_ref": "可空来源引用",
  "title": "文章标题",
  "cover": {
    "strategy": "NONE | FIRST_BODY_IMAGE | EXPLICIT",
    "asset_id": "封面图片 uuid 或 null",
    "asset_url": "/api/content-assets/{asset_id} 或 null"
  },
  "blocks": [
    {
      "block_id": "稳定块 ID",
      "type": "text | image",
      "text": "文本块可用",
      "asset_id": "图片块可用的 uuid",
      "asset_url": "/api/content-assets/{asset_id}",
      "alt": "可空替代文本",
      "position": 0
    }
  ],
  "status": "ACTIVE | ARCHIVED",
  "revision": 1,
  "targets": [],
  "created_at": "UTC ISO-8601",
  "updated_at": "UTC ISO-8601"
}
```

服务器从不返回 `storage_path`、本机文件路径、Cookie 或 Token。缺失值返回
`null`，不得伪造为零或空账号。

## 端点

- `GET /api/content-drafts?limit=50&offset=0`
- `POST /api/content-drafts`：`{title, blocks, cover}`，新建空白草稿；`cover` 省略时为
  `NONE`。
- `GET /api/content-drafts/{draft_id}`
- `PATCH /api/content-drafts/{draft_id}`：`{revision, title, blocks, cover}`。省略
  `cover` 保持当前策略；显式 `NONE` 清空；`FIRST_BODY_IMAGE` 始终解析本次正文
  顺序的第一张图片；`EXPLICIT` 必须引用当前草稿的受控资产。
- `POST /api/content-drafts/import-docx`：multipart 的 `file` 字段；只创建草稿。
- `GET /api/content-sources/legacy-articles?limit=50&offset=0`
- `POST /api/content-drafts/from-legacy/{article_id}`：按需复制内容和图片。
- `POST /api/content-drafts/{draft_id}/assets`：multipart 的 `file` 字段。
- `GET /api/content-assets/{asset_id}`：返回受控图片内容。
- `PUT /api/content-drafts/{draft_id}/targets`：`{revision, targets}`。
- `POST /api/content-drafts/{draft_id}/delivery-plans`：`{revision}`。
- `POST /api/delivery-plans/{plan_id}/execute`：见下文。
- `GET /api/delivery-plans/{plan_id}`：轮询各目标状态。

PATCH 和 PUT 成功后 `revision` 增加一。修订号不匹配返回：

```json
{
  "error": "DRAFT_REVISION_CONFLICT",
  "message": "草稿已在其他页面更新，请选择服务端版本或另存副本",
  "server_draft": {}
}
```

## 目标与计划

封面策略只有 `NONE`、`FIRST_BODY_IMAGE`、`EXPLICIT` 三种。DOCX/旧文章导入有正文图片
时默认使用 `FIRST_BODY_IMAGE`；普通图片上传不会自动改变封面。正文换序时，已有
`FIRST_BODY_IMAGE` 草稿会跟随新首图；若正文没有图片，服务会拒绝保存，要求显式改为
`NONE`。计划创建前会校验封面资产归属和文件存在性。

目标输入字段固定为：

```json
{
  "platform": "xiaoheihe | zol",
  "account_id": "uuid",
  "mode": "DRAFT | PUBLISH",
  "persist_login": true
}
```

同一草稿不能重复添加同一 `account_id`。计划创建时冻结同一份不可变
`ContentVersion`；草稿后续改动不会静默改变旧计划。

```json
{
  "plan_id": "uuid",
  "draft_id": "uuid",
  "content_version": "sha256",
  "cover": {
    "strategy": "FIRST_BODY_IMAGE",
    "asset_id": "冻结后的图片 uuid",
    "asset_url": "/api/content-assets/{asset_id}"
  },
  "status": "READY",
  "targets": [
    {
      "target_id": "uuid",
      "platform": "xiaoheihe",
      "account_id": "uuid",
      "account_display_name": "平台昵称快照",
      "mode": "DRAFT",
      "status": "READY",
      "operation_id": null,
      "confirmation_required": false
    }
  ]
}
```

执行请求：

```json
{
  "target_ids": ["可选目标 uuid"],
  "draft_batch_confirmed": true,
  "confirmations": {"公开目标 uuid": "一次性令牌"}
}
```

存在平台草稿目标却未批量确认时返回
`428 DRAFT_BATCH_CONFIRMATION_REQUIRED`。每个公开目标单独返回
`PUBLISH_CONFIRMATION_REQUIRED`、`confirmation_token` 与 `expires_at`；目标、
账号、模式或冻结内容变化后令牌失效。一个目标失败不会阻止其他目标创建执行单。

### 执行状态与中断恢复

- `CREATING`：服务端正在为目标原子创建或关联账号执行单。
- `QUEUED`：执行单已经持久化但尚未开始；服务重启后可以安全重新调度。
- `RUNNING`：平台操作已经开始。
- `DRAFT_SAVED` / `PUBLISHED`：平台结果已经确认。
- `PARTIAL_FAIL` / `FAILED` / `BLOCKED`：该目标未完整成功，不影响其他目标。
- `RESULT_UNKNOWN`：进程在平台操作期间中断，平台可能已经产生副作用；必须人工核对，
  系统禁止自动重试。

目标先通过数据库租约进行原子领取，再以稳定 `request_key` 幂等创建账号执行单。
相同请求重复到达会返回原执行单；请求内容、账号、模式、操作者或冻结内容不一致时
返回冲突，不会借用另一请求的结果。启动恢复只重新调度 `QUEUED`；遗留的
`RUNNING` 会转换为 `RESULT_UNKNOWN`。

## 兼容边界

- `/delivery/new` 使用 302/308 重定向到 `/upload`，仅原样保留 `draft_id`。
- `/api/upload` 默认返回 `410 LEGACY_UPLOAD_QUEUE_DISABLED`，环境变量
  `LEGACY_UPLOAD_QUEUE_ENABLED=true` 才恢复旧的“上传即入队”。
- 开关关闭时旧队列 worker 不启动，也不会暂停历史任务或追加历史任务日志。
- `/api/delivery-operations` 暂留兼容；新工作台只依赖计划接口。
- 自动化测试不执行真实平台草稿或公开发布。
