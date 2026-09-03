# ArticleOps 发布选项冻结基线

更新时间：2026-09-02

## 版本与分支基线

- 正式 v0.4.6：`cd4eebb4b5435162e4594a74c886115361d92328`。
- 本分支基线：`0091eda1dcc4170a90641c41de5ccc9e64bc2006`（`feature/ai-guided-publication`）。
- 本分支基线包含 v0.4.6、头条草稿链路和最新前端变更；后续工作不得破坏这些既有能力。

## 当前可投递平台

当前 `delivery_enabled=true` 的平台共 7 个：

| 平台 ID | 平台名称 |
| --- | --- |
| `xiaoheihe` | 小黑盒 |
| `zol` | 中关村在线 |
| `zhihu` | 知乎 |
| `weibo` | 微博 |
| `smzdm` | 什么值得买 |
| `toutiao` | 头条号 |
| `baijiahao` | 百家号 |

## 公开发布边界

所有平台的 `public_publish` readiness 默认保持 `DISABLED`。本轮完成基线、选项冻结契约和候选接口，但不调用真实平台；不打开任何公开发布开关，也不执行公开发布。

## 不可破坏的业务链路

- 现有草稿链路必须保持兼容，包括旧请求和旧数据库。
- 账号会话与 Profile 约束必须保持兼容；本轮不读取、不操作任何真实 Profile、Cookie 或 Token。
- `ContentVersion` 及其内容语义、幂等边界必须保持兼容。

## 本轮动作边界

本阶段无真实平台动作：不登录、不导航真实发布页、不输入正文、不选文件、不上传、不保存草稿、不公开发布、不删除，不启动或重启服务，不读取 `data/`、真实 Profile、Cookie、Token、原始响应或用户内容。

阶段 1 文档完成后暂停，等待主线程审查；阶段 2 后端契约实现和阶段 3 定向验证/交付均须遵守上述边界。

## 阶段 3：账号级发布选项能力边界

- 已提供 `POST /api/account-sessions/<account_id>/publish-options` 账号级只读接口；请求查询、候选数量和响应字段均有界，平台只从账号记录派生，响应不返回 Profile、Cookie、Token、正文或原始平台响应。
- 小黑盒与中关村在线的离线候选解析仅接受受限列表或单层映射 fixture，并执行去空、去重、来源查询和数量限制；生产候选发现因候选位于可能触发 autosave 的编辑器，始终 `supported=false`、`PUBLISH_OPTIONS_READONLY_UNVERIFIED`，不进入编辑器，不初始化平台、租约、浏览器或 identity。
- 其他平台仅保留 `BasePlatform` 默认 `unsupported` 只读钩子，不启动浏览器；当前不宣称线上候选可用。
- 阶段 3 不接入 AI、UI 或 MCP，也不改变既有 `select_topic`、保存草稿、发布链路及公开发布门。
