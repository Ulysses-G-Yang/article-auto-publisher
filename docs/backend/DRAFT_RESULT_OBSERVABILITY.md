# 草稿结果可观测性补充

本轮从 `origin/integrate/weibo-draft-v0.4.3`（`c7ecc3d`）摘取
`50e6de0`、`1c08f9d`、`4297bee` 的功能文件，并补强降级判定。源分支的
`HANDOFF_CURRENT.md` 不纳入本分支，集成树中已有的文档改写保持不动。

## 结果语义

- 完整保存、重开标题和图文核验通过：`DRAFT_SAVED`。
- 已绑定本次草稿实体，但完整重开核验尚未完成：仍为 `DRAFT_SAVED`，并带
  `degraded=draft_list_confirmed` 与完整性待核对证据。
- 已绑定本次实体，但重开明确发现标题/正文/图片不一致：
  `DRAFT_SAVED_WITH_WARNINGS`；警告写入执行单、计划目标和活动审计。
- 只有旧同名、保存 ID 不匹配、baseline 未产生唯一新实体或没有本次实体证据：
  `DELIVERY_INCOMPLETE`，不自动重试。

标题唯一匹配不再覆盖明确的实体绑定反证。适配器只记录已有保存 ID、现有实体
ID 或保存前后 baseline 差集，不增加平台请求或编辑器选择器。

## 边界

降级只适用于 `DRAFT`。公开发布开关仍关闭；若保存阶段进入降级，
`publish_now` 不会被调用。媒体不完整且草稿 URL 为空时，只要本次实体绑定证据
存在，仍保存为 `DRAFT_SAVED_WITH_WARNINGS`，不把已存在草稿改写为失败。

重启恢复、首页最近投递、Content Studio 工作台和 MCP 均投影 `degraded` 与安全
证据摘要；MCP 仅输出固定白名单字段，不透传 Profile、Cookie、Token 或原始响应。
