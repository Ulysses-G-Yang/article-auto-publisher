# 账号会话健康心跳 v1

## 目的与默认行为

心跳是账号会话域的只读健康检查：按账号自己的持久 Profile 逐个取得
`AccountProfileLease`，复用 `AccountSessionService.verify_account`，并调用
`allow_interactive_login=False`。它不会创建文章、保存草稿或公开发布。

后台 scheduler 默认关闭：

```text
ACCOUNT_SESSION_HEARTBEAT_ENABLED=false
```

只有显式设置为 `true/1/yes/on` 才会启动。启用后仍然是单个 asyncio task、每批
默认最多一个账号（`max_per_scan=1`），启动先等待一个 `scan_interval`，不会因为
Flask 进程刚启动就立刻打开浏览器。HTTP 健康汇总始终是只读的；没有手动触发
心跳的 API。`start/stop` 由同一 event loop 内的共享生命周期 task 状态
串行化，不保留跨 loop 锁；`stop()` 会主动
取消并等待在途扫描完成清理，确保 `HeartbeatService` 的 `finally`
先释放 busy 标记和数据库 claim，再关闭运行时。并发 `start()` 会等待
旧 stop 完成后创建新 task；即使 stop 调用方被取消，也先完成清理再
重新传播取消。

## 策略与状态机

默认策略为成功 TTL 6 小时、扫描间隔 5 分钟、忙碌退避 30 秒、错误退避 5 分钟
起步并上限 6 小时、登录失效退避 1 小时。每个账号和结果使用稳定 SHA-256
jitter，重启后不会改变同一账号的抖动结果。每次只读验证使用数据库 claim/lease
串行化，默认 claim TTL 为 10 分钟，允许范围为 30 秒到 1 小时；异步验证超时预算
为 claim TTL 的 80%，避免旧 verifier 在租约被接管后继续写入心跳结果。策略字段有
严格的正数/范围校验。

只有 `status=ACTIVE`、`heartbeat_enabled=true` 且 `session_status` 不是
`LOGIN_REQUIRED` 的账号会进入 due 查询；`next_heartbeat_at` 为空或已到期才会
被扫描。due 查询只是候选列表，真正访问 Profile 前还必须以单条条件 `UPDATE`
成功抢到自己的 claim；未抢到租约的 worker 不会打开浏览器。账号会话状态的核心
转移如下：

```text
VALID/UNVERIFIED/ERROR --成功--> VALID
VALID --ACCOUNT_BUSY/PROFILE_IN_USE--> VALID + DEFERRED + 短退避
任意可验证状态 --LOGIN_REQUIRED/SESSION_EXPIRED--> LOGIN_REQUIRED
任意可验证状态 --RATE_LIMITED/CHALLENGE/未知稳定码--> 原状态 + FAILED + 指数退避
```

忙碌只说明 Profile 当前被另一个流程占用，不是登录失效；它保留原有
`VALID` 与 `last_verified_at`，只更新时间、错误码和下一次尝试时间。清理阶段的
普通异常不能覆盖已成功的验证结果；取消、退出和键盘中断会继续向上传播。

心跳结果写入带有 `heartbeat_claim_owner` fencing 条件：只有持有当前 claim 的
worker 才能更新状态、写活动日志并清理租约。claim 丢失时返回稳定的
`HEARTBEAT_CLAIM_LOST`，不覆盖其他 worker 的状态。验证入口仍可能在自己的
内部流程中记录平台验证失败，因此 claim TTL 必须覆盖只读验证的实际最大耗时；
当前服务对异步验证提供 TTL 80% 的超时预算，平台适配器或同步注入函数不得运行
无界阻塞。

使用 `run_flask_production.py` 的生产进程启动时，scheduler 先执行一次
纯数据库 recovery，再根据开关决定是否创建扫描
task。recovery 只恢复带有明确且已过期 heartbeat claim 的 `VERIFYING`：有
`last_verified_at` 恢复为 `VALID`，否则恢复为 `UNVERIFIED`，并把下一次检查设为
当前时间；没有 claim 来源的遗留 `VERIFYING` 保持不变，仍在有效 claim 内的账号
也保持不变。其余过期 claim 只清除三列，不杀 Chrome、不删除 Singleton、不触碰
Profile。即使 `ACCOUNT_SESSION_HEARTBEAT_ENABLED=false`，启动初始化仍执行这次
数据库清理，但绝不会启动扫描或打开浏览器。
生产入口强制要求 `account_sessions` 扩展存在；扩展缺失或启动初始化
失败时，必须在启动队列 worker 和 Waitress 前终止进程，不得带病对外服务。

失败分类优先使用异常类型、稳定 `error_code` 和 `PROFILE_IN_USE:` 等稳定前缀，
不会依据“包含登录”等模糊文本推断。活动日志只写
`HEARTBEAT_SUCCEEDED`、`HEARTBEAT_FAILED`、`HEARTBEAT_DEFERRED` 及稳定错误码，
不写异常正文、Profile 路径、Cookie、Token 或原始平台 ID。

## 环境变量

可选的秒数策略变量：

| 变量 | 默认值 |
| --- | ---: |
| `ACCOUNT_SESSION_HEARTBEAT_ENABLED` | `false` |
| `ACCOUNT_SESSION_HEARTBEAT_SUCCESS_TTL_SECONDS` | `21600` |
| `ACCOUNT_SESSION_HEARTBEAT_SCAN_INTERVAL_SECONDS` | `300` |
| `ACCOUNT_SESSION_HEARTBEAT_MAX_PER_SCAN` | `1` |
| `ACCOUNT_SESSION_HEARTBEAT_BUSY_BACKOFF_SECONDS` | `30` |
| `ACCOUNT_SESSION_HEARTBEAT_ERROR_BACKOFF_BASE_SECONDS` | `300` |
| `ACCOUNT_SESSION_HEARTBEAT_ERROR_BACKOFF_MAX_SECONDS` | `21600` |
| `ACCOUNT_SESSION_HEARTBEAT_LOGIN_REQUIRED_BACKOFF_SECONDS` | `3600` |
| `ACCOUNT_SESSION_HEARTBEAT_CLAIM_TTL_SECONDS` | `600`（范围 `30`–`3600`） |
| `ACCOUNT_SESSION_HEARTBEAT_JITTER_RATIO` | `0.1` |

## 只读健康 API

`GET /api/account-sessions/health` 返回按当前调用方
`session.read` 与账号范围过滤的汇总：`heartbeat_enabled`、`total_accounts`、
`valid`、`stale`、`due`、`login_required`、`busy`、最近/下次心跳时间和安全策略
摘要。响应不包含账号 ID、昵称、Profile、Cookie、Token 或原始平台用户 ID。

## Web/MCP 边界与启用验收门

Web 和 MCP 必须分别通过各自的 `AccessContext`，只允许读取被授权账号的汇总；
心跳内部使用 `SYSTEM` 验证上下文，不向外暴露其账号范围。心跳验证永远不调用
交互式 `login()`，不扫码，不清 Cookie，不执行 ZOL Cookie bridge，也不触碰
delivery/content bridge。心跳不是 Cookie 池、Cookie SDK、Cookie 续期器或跨平台
凭据共享机制；它只验证现有隔离 Profile 的会话状态，原始 Cookie/Token 不进入
心跳数据库、活动日志或健康 API。真实启用前必须由人工确认：

1. 使用测试数据库和隔离测试 Profile 验证迁移、状态转移、退避、单批单账号、
   scheduler `start/stop` 与非重入行为。
2. 确认观测到的 `HEARTBEAT_*` 日志已脱敏，`PROFILE_IN_USE` 不会降级 `VALID`，
   `LOGIN_REQUIRED` 才会进入待登录状态。
3. 由人工在目标机器确认 Profile 无其他发布/登录流程、平台风控窗口允许，且
   明确接受启用后只读打开浏览器的影响；公开发布开关仍保持关闭。
4. 通过全量 pytest、Ruff 和 `git diff --check` 后，使用受控环境变量启用并
   观察首轮结果；任何未知状态都要把环境变量设为 `false`（或移除），
   停止并重启生产服务，确认 `heartbeat_enabled=false`、scheduler 已退出且
   不再产生新的 `HEARTBEAT_*` 事件后才排查。仅修改环境变量不会停止
   已运行的 scheduler。
