# 多账号会话与投递后端边界

## 目标

账号会话域作为现役 Flask 服务中的独立模块运行，保留旧上传、任务队列与账号
接口，不把新执行单写入旧任务表。每个投递执行单在创建时固化平台、账号、
操作者、来源与内容版本，后续不依赖全局“当前账号”。

## 数据与运行目录

- 默认数据库：`data/account_sessions/account_sessions.db`
- 新账号 Profile：`data/account_sessions/profiles/{platform}/{account_id}`
- 旧 Profile：只登记原绝对路径，不复制、不搬移、不删除。
- worktree/迁移工具可用 `LEGACY_CHROME_PROFILE_ROOT` 显式指定旧 Profile 根目录。
- 所有 SQLite 连接启用 WAL、`busy_timeout=5000` 与外键。

`platform_accounts`、`delivery_operations`、`account_activity` 和
`publish_confirmations` 与旧 `app.db` 分离。跨数据库关系只保存不可变标识，
不建立伪外键。

## Profile 安全规则

账号操作同时取得：

1. 规范化 Profile 路径对应的跨进程文件锁；
2. 旧 Profile 兼容现役发布流程的平台级进程内锁。

发现 `SingletonLock`、`SingletonCookie` 或 `SingletonSocket` 时返回占用错误，
绝不删除后重试。`BasePlatform` 的严格模式只尝试一次，且不会创建缺失的受管
Profile。旧 Profile 的联网验证属于业务逻辑只读：不自动登录、不清 Cookie、
ZOL 不桥接 Cookie；但 Chromium 本身仍可能更新缓存或时间戳。

## API 契约

- `GET /delivery/new`
- `GET /api/platforms`
- `GET /api/platforms/{platform}/accounts?usable=true`
- `POST /api/platforms/{platform}/accounts/login`
- `POST /api/accounts/{account_id}/verify`
- `POST /api/accounts/{account_id}/session-policy`
- `POST /api/account-sessions/{account_id}/logout`
- `GET /api/account-sessions/{account_id}/activity`
- `POST /api/delivery-operations`
- `GET /api/delivery-operations/{operation_id}`

账号列表只返回 `account_id`、真实显示名、脱敏平台 ID、会话状态、复用策略和
验证时间，不返回 Profile 路径、Cookie、Token 或原始平台 ID。
活动日志响应使用顶层 `activities` 数组，条目只包含账号快照、来源、动作、
级别、脱敏消息和时间。

## 发布安全门

草稿是默认模式。公开发布必须满足：

1. 调用方拥有账号级 `publish.request` 与 `publish.execute` 权限；
2. 第一次请求取得五分钟有效的一次性确认令牌；
3. 第二次请求的平台、账号、操作者、标题、正文与模式指纹完全一致；
4. 环境变量 `ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH=true` 显式开启。

当前阶段第 4 条保持关闭，因此任何路径都不会公开发布。未来 MCP 需要构造
独立 `AccessContext`，限定 capability 与 allowed account IDs，并复用同一个
`DeliveryService`，不得直接调用平台自动化。

## 日志隔离

平台旧流水线的同步日志写入内存缓冲，执行结束后落到对应账号和执行单的
`account_activity`；不会使用 `task_id=0` 写入旧 `task_logs`。日志保存账号昵称
快照、actor/source/action 和脱敏错误摘要，不保存凭据值。
