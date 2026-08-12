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
- `POST /api/content-drafts`：`{title, blocks}`，新建空白草稿。
- `GET /api/content-drafts/{draft_id}`
- `PATCH /api/content-drafts/{draft_id}`：`{revision, title, blocks}`。
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

## 兼容边界

- `/delivery/new` 使用 302/308 重定向到 `/upload`，仅原样保留 `draft_id`。
- `/api/upload` 默认返回 `410 LEGACY_UPLOAD_QUEUE_DISABLED`，环境变量
  `LEGACY_UPLOAD_QUEUE_ENABLED=true` 才恢复旧的“上传即入队”。
- `/api/delivery-operations` 暂留兼容；新工作台只依赖计划接口。
- 自动化测试不执行真实平台草稿或公开发布。
