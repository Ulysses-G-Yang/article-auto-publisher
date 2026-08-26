# ArticleOps 当前模块化开发交接

日期：2026-08-25

> 本版本由 `feature/shared-sdk-heartbeat-delay` 分支更新：新增
> `delivery_tools` 外挂工具模块（共享 Playwright SDK、Cookie 心跳、
> 随机真人延时），详见 §11。

## 1. 本文目的

本文是当前唯一有效的协作接力入口。协作方式如下：

- 外部 AI：快速开发用户明确指定的新模块，仅在任务包授权范围内写代码。
- Codex：负责只读审查、测试、验收、必要的聚焦修正，以及集成到当前主集成分支。

本文不要求任何 AI 自动续做旧 P0、旧平台待办或历史路线图。没有新的明确任务包，就停止写入并请求确认。

## 2. 当前可信基线

| 项目 | 当前值 |
| --- | --- |
| 仓库 | `D:\Backup\Documents\article-auto-publisher` |
| 当前工作树 | `D:\Backup\Documents\article-auto-publisher-worktrees\weibo-v043-integration` |
| 当前集成分支 | `integrate/weibo-draft-v0.4.3` |
| Git 远端 | `git@github.com:Ulysses-G-Yang/article-auto-publisher.git` |
| 本文之前的功能代码基线 SHA | `d55ea1f40adf41cc58870e8c43f9d6fc292b178c` |
| Python | `C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe` |

开始任何新模块前都必须执行 `git fetch origin`，并确认起点为远端
`origin/integrate/weibo-draft-v0.4.3`。不得从旧本地分支、旧交付包或旧 SHA 开始。

## 3. 当前已完成的四个提交

1. `a811f4f361c030158fa341ad140d03f296bc951f`
   `fix(content): normalize Word delivery structure`
   - 完善 Word 视觉标题识别及样式字号继承。
   - 生成不修改原稿的投递副本，移除可安全降级的展示 marks/link 样式。
   - 将段内文字和图片按原 child 顺序投影，保留图片数量、重复图片和图文顺序。

2. `ae8bd7e580a581f37384f0424a28c2e64b4983f5`
   `fix(content): preserve source semantics in delivery normalization`
   - 保留 canonical Word 文档的原始 H1 语义，只在投递副本中归一正文标题。
   - 标题块保持原子性，避免标题中的混合节点泄漏到正文投影。
   - 升级投递策略版本并补齐公式节点 fail-closed 检测。

3. `a1b4d751a9b307b085bde19f62fcdb7140e5a210`
   `feat(studio): simplify bulk target selection`
   - 增加可投递平台和有效账号的批量选择/取消能力。
   - 草稿目标取消二次勾选，公开发布确认仍保留。
   - 合并目标保存、处理跨草稿响应及修订竞态，减少连续操作造成的冲突。

4. `d55ea1f40adf41cc58870e8c43f9d6fc292b178c`
   `fix(delivery): serialize batch platform execution`
   - 同一进程中的平台实际操作使用全局串行批次。
   - 除进程内第一条外，平台操作之间随机等待 8～20 秒；测试可注入零延迟。
   - 单项失败继续下一项；重启恢复按计划和目标位置稳定排序。

## 4. 旧文档状态

以下内容只能作为历史参考，不能作为当前任务来源：

- 旧 `codex_handoff*.md` 中的分支、优先级和待办。
- `RESUMABLE_SESSION.md` 中的恢复命令和进行中状态。
- `AGENTS.md` 中以下内容已经失效，不得执行：旧 canonical/current branch、旧主线程与执行线程角色、
  硬编码的 `origin/feat/...` 推送及 SHA 核对目标、旧 P0 顺序，以及旧暂停条件。

`AGENTS.md` 只有以下通用规则继续有效：固定 Python 3.12 路径；禁止触碰
`data/`、Profile、Cookie、Token、`uv.lock`；真实副作用必须单独授权；只暂存任务文件；
focused commit；使用 SSH 推送；禁止 force push。实际推送和远端 SHA 核对始终针对当前新分支，
不得使用 `AGENTS.md` 中硬编码的旧分支。
若历史文档与本文冲突，以本文的当前分支和协作流程为准；若安全规则冲突，采用更严格的规则。

## 5. 外部 AI 的默认边界

外部 AI 必须遵守：

- 只实现用户或 Codex 下发任务包中明确指定的新模块。
- 不自行续做旧平台、旧 P0、旧路线图或“顺手修复”的旁支问题。
- 不修改百家号或任何未被任务包明确授权的平台模块。
- 不扩大数据库、MCP、账号、权限、发布契约或前端范围。
- 不自行执行真实登录、保存草稿、公开发布、删除或数据迁移。
- 发现任务包外问题时只记录证据和风险，不直接修改。

## 6. 新分支与独立工作树模板

将 `<module-slug>` 替换为简短模块名；目录和分支都不得复用旧工作树。

```powershell
Set-Location 'D:\Backup\Documents\article-auto-publisher'
git fetch origin
git worktree add `
  -b "feature/<module-slug>" `
  "D:\Backup\Documents\article-auto-publisher-worktrees\<module-slug>" `
  origin/integrate/weibo-draft-v0.4.3

Set-Location "D:\Backup\Documents\article-auto-publisher-worktrees\<module-slug>"
git rev-parse HEAD
git status --short
```

实际开发起点必须等于 `git fetch` 后的远端集成分支 HEAD。`d55ea1f...`
只是本文之前的功能代码基线，不是要求新工作树停留的提交。创建工作树后执行：

```powershell
$localHead = git rev-parse HEAD
$remoteHead = git rev-parse origin/integrate/weibo-draft-v0.4.3
if ($localHead -ne $remoteHead) {
  throw "工作树 HEAD 与远端集成分支不一致：$localHead != $remoteHead"
}

git merge-base --is-ancestor d55ea1f40adf41cc58870e8c43f9d6fc292b178c HEAD
if ($LASTEXITCODE -ne 0) {
  throw '当前 HEAD 不包含本文之前的功能代码基线'
}
```

在任务报告中记录 `$remoteHead` 的完整 40 位 SHA；不得擅自回退到功能代码基线。

## 7. 新模块任务包协议

每个任务包必须在写代码前冻结以下内容：

```text
任务名称：
业务目标：
基线分支与 SHA：
允许修改的目录/文件：
禁止修改的目录/文件：
冻结 API/路由/模型/数据库/MCP 契约：
输入、输出与错误语义：
必须通过的定向测试：
是否要求全量测试：
允许的副作用：默认“无”
停止条件：
目标提交信息：
```

默认冻结项为现有 API 路径、请求/响应字段、数据库结构、账号权限和 MCP 工具契约。
只有任务包逐项明确授权时才能修改。任务包存在歧义、需要新权限、需要真实平台动作、
或需要修改禁止目录时，外部 AI 必须停止并请求补充，不得自行推断。

## 8. 外部 AI 的交付报告

完成模块后，必须向 Codex 提供：

1. 分支名、focused commit 和远端完整 40 位 SHA。
2. 精确文件清单及每个文件的职责变化。
3. 新增或变化的接口、输入、输出、错误码和兼容性说明。
4. 实际运行的测试命令和完整结果摘要。
5. 已知风险、未覆盖场景和明确停止条件。
6. 未执行的真实副作用清单，例如未登录、未保存平台草稿、未发布、未删除。
7. 工作树是否干净，以及是否存在未跟踪文件。

没有推送远端、没有 40 位 SHA 或夹带无关修改，均不视为完成。

## 9. Codex 的审查、修正与集成流程

1. 获取外部 AI 的远端分支，只读检查基线、提交范围和工作树差异。
2. 按任务包审核安全边界、冻结契约、错误语义、并发/幂等及兼容性。
3. 在干净环境重新运行定向测试和必要的全量测试，不采信仅口头报告。
4. 若发现小型明确问题，Codex 可做单独 focused 修正提交；若涉及架构或契约变化，退回任务包重新确认。
5. 集成前再次核对提交清单，确保不含 `data/`、凭据、运行产物或无关文件。
6. 集成到 `integrate/weibo-draft-v0.4.3` 后重新运行验收，SSH 推送并核对远端 40 位 SHA。
7. 只有代码、测试、提交、推送和远端 SHA 全部完成后，才向用户报告交付完成。

## 10. 安全与副作用红线

- 禁止读取、复制、修改、提交或输出 `data/`、Profile、Cookie、Token、原始敏感响应。
- 禁止触碰未跟踪的 `uv.lock`。
- 真实登录、真实草稿保存、公开发布、删除动作必须分别取得用户明确授权。
- 公开发布保持关闭，测试不得通过修改开关绕过。
- 平台结果证据不足必须记录 `RESULT_UNKNOWN`，禁止自动重试可能已产生副作用的操作。
- 禁止 force push、批量暂存和恢复用户无关修改。
- 文档、日志、测试 fixture 和提交信息不得包含秘密、本机凭据或真实用户内容。

## 11. delivery_tools 外挂模块（feature/shared-sdk-heartbeat-delay，参考实现）

三个外挂模块已实现于 `src/delivery_tools/`，设计为不修改现有发布业务逻辑；
现有 `content_studio/`、`account_sessions/`、`article_mvp/`、`platforms/`、
`app.py`、`config.py`、`pyproject.toml` 均未改动。

> ⚠️ **定位说明（2026-08-25 复核）**：按用户决策采用“方向二”，本组模块
> **不接入生产链路**，仅保留为独立工具参考。原因：本项目已有更成熟的现有
> 机制（persistent Chrome profile 自动 Cookie、`AccountProfileLease` 租约、
> `HumanSimulator`、`HeartbeatService/Scheduler`），外挂模块与它们架构不兼容
> 或功能重叠。生产补强走 §11.7 的现有架构内改动。

### 11.1 文件清单与职责

| 文件 | 职责 |
|------|------|
| `src/delivery_tools/__init__.py` | 包说明；三个子模块互不依赖，心跳依赖 sdk |
| `src/delivery_tools/pw_shared_sdk.py` | 共享 Playwright SDK：全局单例 browser + 按账号隔离的 BrowserContext 池 |
| `src/delivery_tools/session_heartbeat.py` | 后台 Cookie 心跳：每账号临时 page 探测登录态，状态 `alive/expire/dead` |
| `src/delivery_tools/human_delay_util.py` | 随机真人延时：动作/页面等待/任务间隙三类装饰器 + 字符级模拟输入 |
| `tests/test_delivery_tools.py` | 27 个单元测试，全部 mock，不启动真实浏览器、不触网、不读凭据 |

### 11.2 共享 Playwright SDK（pw_shared_sdk.py）

- `SharedPlaywright.get_instance()`：全局单例。
- `await sdk.init_global_browser(headless=True)`：服务启动执行一次，幂等；
  只启动 playwright + chromium browser，不创建账号上下文。
- `await sdk.get_or_create_ctx(site, account_id, cookies=None)`：返回该账号
  隔离的 BrowserContext；已在池中且连接正常则直接复用；可传入 cookies 恢复登录态。
- `await sdk.dump_ctx_cookies(ctx, domain_url)`：导出最新 cookie 交给存储层。
- `await sdk.close_one_ctx(site, account_id)` / `close_all_ctx()` / `shutdown()`：
  销毁单个/全部上下文；`shutdown` 同时关闭 browser 与 playwright。
- 未初始化即调用 `get_or_create_ctx` 会抛出 `RuntimeError`。

接入改动点（极小）：服务启动钩子执行 `init_global_browser()`；发布任务内把
“新建 context”替换为 `get_or_create_ctx(...)`，其后原有发布逻辑一行不改。

### 11.3 Cookie 心跳（session_heartbeat.py）

- `single_account_heartbeat(site, account_id, check_url, judge_fn=None, ...)`：
  单账号一轮心跳，返回 `alive/expire/dead`。流程：读取持久化 cookie → 获取
  隔离 context → 临时 page 访问校验页 → 判断登录态 → 有效则导出最新 cookie
  回写 → 更新状态存储；任何异常降级 `dead` 且不抛出。
- `heartbeat_loop(account_meta_list, heartbeat_interval_sec=300, ...)`：后台常驻
  协程，每轮 `asyncio.gather(..., return_exceptions=True)` 并发全部账号；
  单账号失败不拖垮整轮；可配 `max_accounts_per_round` 分批并发。
- `check_account_healthy(site, account_id, load_cookie_fn=None)`：发布任务前置
  校验；未注入存储回调时返回 True（不拦截），注入后按 cookie 非空判断。
- 存储回调全部由外部注入（`load_cookie_fn`/`save_cookie_fn`/`update_status_fn`），
  本模块不接管存储、不自动尝试登录（扫码无法自动化）。
- 登录判断：优先平台自定义 `judge_fn(page, site)`；缺省启发式检查 URL 与
  常见登录表单选择器。

FastAPI/Flask 启动挂载示例：

```python
import asyncio
from delivery_tools.pw_shared_sdk import SharedPlaywright
from delivery_tools.session_heartbeat import heartbeat_loop

# 启动钩子
sdk = SharedPlaywright.get_instance()
await sdk.init_global_browser()
all_accounts = [{"site": ..., "account_id": ..., "check_url": ...}, ...]
asyncio.create_task(heartbeat_loop(all_accounts, heartbeat_interval_sec=300))
```

### 11.4 随机真人延时（human_delay_util.py）

- `@human_action_delay()`：click/select 等动作前随机休眠（动作区间 0.8～2.5s）。
- `await human_page_delay()()`：goto 后页面等待（1.5～4.0s）。
- `await task_gap_sleep()`：两篇任务之间休眠（5～15s）。
- `await human_type(page, selector, text, char_min, char_max)`：字符粒度随机
  输入，不整段 paste。
- `configure_delays({...})`：按平台覆盖区间；`no_delay()`：测试用全零。
- 区间全部来自 `DelayConfig`，不存在硬编码固定 sleep。

### 11.5 验证结果（fresh 运行）

```powershell
& $articleOpsPython -m pytest tests/test_delivery_tools.py -q    # 27 passed
& $articleOpsPython -m pytest -q                                 # 884 passed, 7 skipped
& $articleOpsPython -m ruff check src/delivery_tools tests/test_delivery_tools.py  # All checks passed
git diff --check                                                 # 通过
```

### 11.6 已知风险与未覆盖

- 心跳登录判断启发式仅覆盖常见表单；平台 DOM 改版需维护独立 `judge_fn`。
- 长时间运行 Browser 内存缓慢上涨：建议后续增加“单 context 执行 N 次后销毁
  重建”策略，不重启全局 browser。
- headless 模式更易触发风控；可配置 `headless=new` 更接近真实指纹。
- 未执行任何真实登录、草稿保存、公开发布或删除；生产接线（启动钩子、存储
  回调、发布主流程前置拦截）留待业务侧按任务包接入。

### 11.7 方向二：现有架构内补强（2026-08-25）

用户决策：不接线 delivery_tools，改在现有架构内补强。调研结论：

| 现有机制 | 位置 | 状态 |
|---|---|---|
| 会话心跳 Service/Policy/Scheduler | `src/account_sessions/session_health.py` | 完整，有专门测试覆盖 |
| 心跳接线（verify 注入只读验证） | `src/account_sessions/web.py` | 完整 |
| Profile 跨进程租约 + Singleton 检查 | `src/account_sessions/leases.py` | 完整，有测试 |
| 反检测脚本注入 | `platforms/base.py initialize()` | 完整，有测试 |
| 心跳启用开关 | `ACCOUNT_SESSION_HEARTBEAT_ENABLED` 环境变量 | 默认 false，账号级 `heartbeat_enabled` 默认 true |

**本次修复的缺口**：`AccountSessionRuntimeState._ensure_runtime()` 此前是惰性
初始化——只在第一个 HTTP 请求时才执行数据库 recovery 和启动心跳 scheduler，
与 `docs/backend/ACCOUNT_SESSION_HEARTBEAT.md` 承诺的“生产入口启动时先执行一次
纯数据库 recovery”不符。

修复：
1. `src/account_sessions/web.py` 新增公开方法 `AccountSessionRuntimeState.start()`：
   幂等初始化运行时 + 启动心跳 scheduler（即使开关关闭也执行一次纯数据库
   recovery，不打开浏览器）。
2. `run_flask_production.py` 生产入口强制要求 `account_sessions` 扩展存在，
   并在对外接收请求前调用 `account_state.start()`；初始化失败时
   队列 worker 和 Waitress 均不启动。
3. `HeartbeatScheduler` 使用同一 event loop 内原子赋值的
   `_starting_task/_stopping_task` 状态串行化 `start/stop`，不保留跨 loop 锁；
   `stop()` 主动取消并等待在途扫描的 `finally` 完成后才清理句柄。
   即使 `stop()` 调用方被取消，也会先释放 claim 再重新传播取消；
   并发 `start()` 等旧清理完成后启动新 task。
4. `AccountSessionRuntimeState.close()` 持有现有同步锁完成 scheduler
   stop 与 runtime dispose，防止并发 `start()` 在旧 runtime 关闭中途抢跑。
5. 测试覆盖 `start()` 幂等、真实多线程重叠、调用方取消后
   `HeartbeatService` 仍释放真实数据库 claim、stop/start 并发重启、
   close/start 跨线程串行化，以及生产启动顺序与两类失败阻断。

验证（本轮 fresh）：整份 `tests/test_account_session_health.py`、整份
`tests/test_run_flask_production.py`、账号运行时启动顺序和 Flask 组合层
定向测试共 37 passed；本轮 Python 文件 Ruff 与 `git diff --check` 通过。

**生产启用剩余步骤（必须人工完成，验收门见 ACCOUNT_SESSION_HEARTBEAT.md）**：
1. 目标机器确认 Profile 无其他发布/登录流程，风控窗口允许；
2. 显式设置 `ACCOUNT_SESSION_HEARTBEAT_ENABLED=true` 并重启生产服务；
3. 观察首轮 `HEARTBEAT_*` 日志脱敏、`PROFILE_IN_USE` 不降级 `VALID`；
4. 任何未知状态立即把环境变量设为 `false`（或移除），停止并
   重启生产服务；确认健康汇总为 `heartbeat_enabled=false`、scheduler
   已退出且不再产生新的 `HEARTBEAT_*` 事件后才排查。仅修改环境变量
   不会停止已运行的 scheduler。公开发布开关保持关闭。

## 12. 草稿结果可观测性（方案一+方案二，2026-08-25）

解决"草稿明明保存了但界面只显示失败、业务人员要翻终端日志"的问题。
设计文档：`docs/backend/DRAFT_RESULT_OBSERVABILITY.md`。

### 方案一：证据链结构化（已实现）

- `platforms/base.py` 新增 `DraftVerificationEvidence`：记录保存接口响应、
  草稿箱标题匹配数、重开核验、草稿链接，生成脱敏 `summary`。
- 5 平台适配器（微博/知乎/ZOL/SMZDM/百家号）`save_draft()` 埋点写入证据；
  失败/未知路径通过异常 `evidence` 属性带出。
- `DeliveryOperation` 新增 `verification_evidence` JSON 列（幂等迁移）；
  `_mark_failed`/`_mark_completed*` 持久化，`operation_payload` 解码返回。
- 前端 `planTargetDetail`：`RESULT_UNKNOWN` 且草稿箱有唯一匹配时显示
  "平台草稿箱已存在标题唯一匹配的草稿"。

### 方案二：只读核验 API + 前端按钮（已实现）

- `BasePlatform.verify_draft_readonly(title)`：默认 `{"unsupported": True}`
  （fail-closed）；知乎/微博已实现（只导航草稿箱按标题匹配，不点开编辑页）。
- `POST /api/delivery-operations/{id}/verify-draft`：同步只读核验，复用
  Profile 租约，返回 `title_matched/match_count/draft_url/structure`，
  错误码 `PROBE_UNSUPPORTED_PLATFORM`/`PROBE_NOT_FOUND`/`PROBE_TITLE_AMBIGUOUS`/
  `PROBE_RESULT_UNKNOWN` 等。
- 前端失败/未知目标行出现"核验平台草稿"按钮，一键只读核验并展示结果。
- 审计：核验写入 `account_activity`（action=`DRAFT_PROBE*`）。

验证（fresh）：全量 `914 passed, 7 skipped`；Ruff 通过；`git diff --check` 通过。
新测试：`tests/test_draft_evidence.py`（10）、`tests/test_draft_verify.py`（8）、
前端契约新增证据/核验断言。

安全边界：判定逻辑零改动（防假成功语义不变）；核验只读（拦截非 GET）；
租约复用不强抢；脱敏；公开发布开关保持关闭；未实现平台 fail-closed。

**5 平台只读核验全部实现（2026-08-25 补充）**：
- 知乎/微博：草稿箱列表标题精确匹配（此前已实现）。
- ZOL：`_navigate_draft_verification_page` + `_matching_draft_cards` 只读匹配卡片。
- SMZDM：草稿箱 `.draft-list li` 标题文本匹配（不打开编辑页，避免自动保存副作用）。
- 百家号：作品页草稿 tab + 搜索框填标题（防抖查询）+ 匹配行数（不点"修改"）。
- 测试：`tests/test_draft_verify.py` 新增 4 个平台场景（找到/未找到/歧义）。

### 保存成功放宽判定：降级成功 / 投递未完成（2026-08-25）

需求：保存结果未知时不再一律判失败——只要草稿箱出现同名草稿就算成功；全部通道都
不满足时也不显示"失败"，而是显示"投递未完成"。

- `BasePlatform.publish()` 按优先级降级判定：P1 保存响应 → P2 草稿箱标题唯一匹配
  → P3 重开标题 → P4 重开 DOM；任一满足 ⇒ 降级成功（`success=True`、
  `degraded="draft_list_confirmed"`，`draft_url` 可为空），全部不满足 ⇒
  `{success:False, error_code:"DELIVERY_INCOMPLETE", ...}`（独立终态，区别于
  RESULT_UNKNOWN/FAILED；不在 `RESULT_UNKNOWN_ERROR_CODES`，也不在
  `SESSION_INVALIDATING_ERROR_CODES`）。
- `DeliveryOperation` 新增 `degraded` 列；`_mark_completed*` 持久化降级标记，
  `_mark_failed` 对 `DELIVERY_INCOMPLETE` 写该状态并保留证据链。
- `DeliveryPlanTarget` 新增 `degraded` + `verification_evidence` 列；
  `set_plan_target_result` 落库，`public_plan_target` 回读；计划聚合把
  DELIVERY_INCOMPLETE 归入失败态（全失败 FATAL / 混合 PARTIAL_FAIL），
  降级成功仍按 DRAFT_SAVED 计 SUCCESS 且标记不丢。
- 前端：计划/执行状态标签 `DELIVERY_INCOMPLETE='投递未完成'`（warning 样式）；
  降级成功目标显示"草稿已保存（草稿箱确认）"+ 证据展示 + 可重新验证按钮。
- 测试：`tests/test_delivery_degraded.py`（10 个，publish 降级链 + DeliveryService
  落库）；`tests/test_content_studio.py` 聚合与计划目标回读；
  前端契约断言 `DELIVERY_INCOMPLETE` 标签与"草稿已保存（草稿箱确认）"。

**交付状态（2026-08-25 终版，已推送）**：

- 提交：`4297beea92408edb1329d46dcba359256b3dd41b`
  `feat(delivery): relax save-success to degraded draft-box confirmation or delivery-incomplete`
  （14 文件，+673/−13，含新测试文件 `tests/test_delivery_degraded.py`）。
- 分支 `feature/shared-sdk-heartbeat-delay`；SSH 推送
  `dc15582..4297bee HEAD -> feature/shared-sdk-heartbeat-delay` 后，
  `git ls-remote origin` 与本地 `git rev-parse HEAD` 完全一致（40 位 SHA 匹配）。
- 全量验证：`929 passed, 7 skipped`（exit 0；仅已知 heartbeat 守护线程
  `PytestUnhandledThreadExceptionWarning`，非失败）；Ruff 仅剩
  `platforms/base.py` 13 条 HEAD 即有的既有违规（本轮零新增，对照
  `git show HEAD:` 核实）；`node --check web/static/js/content-studio.js`
  与 `git diff --check` 通过。
- 工作树干净，无未跟踪文件；未执行任何真实登录、草稿保存、公开发布或删除
  副作用；`data/`、Profile、Cookie、Token、`uv.lock` 均未触碰；公开发布开关
  保持关闭。

后续待办（若需）：把该分支成果集成到 `integrate/weibo-draft-v0.4.3` 时，按
§9 流程重跑定向 + 全量测试后再合并；`DELIVERY_INCOMPLETE` 的终端提示文案与
"投递未完成"状态在前端是否追加全局汇总入口，由产品侧决定。

## 13. 最小验证与 Git 交付

PowerShell 示例：

```powershell
$articleOpsPython = 'C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe'

# 按任务包运行定向测试
& $articleOpsPython -m pytest <tests-for-module> -q

# 需要全量时运行
& $articleOpsPython -m pytest -q

# Python 改动检查
& $articleOpsPython -m ruff check <changed-python-files>

# JavaScript 改动逐文件检查
node --check <changed-javascript-file>

# 提交前检查
git diff --check
git status --short
```

Git 交付必须只暂存任务文件：

```powershell
git add -- <task-file-1> <task-file-2>
git commit -m "<focused commit message>"
$env:GIT_SSH_COMMAND = 'ssh -o StrictHostKeyChecking=accept-new'
git push origin HEAD
git rev-parse HEAD
git ls-remote origin "refs/heads/$(git branch --show-current)"
```

当前基线曾验证为 `857 passed, 7 skipped`，但这只是接力时的历史基线。
任何新 AI 都必须在自己的工作树 fresh 运行任务包要求的测试，不能直接引用该结果。

## 14. 可直接复制给新 AI 的启动提示

```text
你负责 ArticleOps 的一个独立新模块。请先只读预检，不要续做任何旧 P0 或历史平台待办。

可信起点：
- repo：D:\Backup\Documents\article-auto-publisher
- base：origin/integrate/weibo-draft-v0.4.3
- 本文之前的功能代码基线 SHA：d55ea1f40adf41cc58870e8c43f9d6fc292b178c（不是实际开发起点）
- Python：C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe
- remote：git@github.com:Ulysses-G-Yang/article-auto-publisher.git

先执行完整的 git fetch origin，并从 origin/integrate/weibo-draft-v0.4.3 创建独立 feature 分支和独立 worktree。
实际工作树 HEAD 必须等于 fetch 后的远端分支 HEAD，并且
git merge-base --is-ancestor d55ea1f40adf41cc58870e8c43f9d6fc292b178c HEAD 必须成功。
报告实际远端 HEAD 的完整 40 位 SHA，不要把功能代码基线当作当前 HEAD。
阅读根目录 HANDOFF_CURRENT.md 和 AGENTS.md；HANDOFF_CURRENT.md 决定当前分支和协作流程。
AGENTS.md 中旧分支、旧线程角色、硬编码 origin/feat 推送目标、旧 P0 和暂停条件均已失效；
只继承其中 Python 路径、数据与凭据安全、真实副作用授权、focused commit、SSH、no force push
和只暂存任务文件等通用规则，实际推送始终使用当前新分支。

在我提供完整任务包前禁止写代码。收到任务包后，只修改允许目录；冻结 API/路由/模型/数据库/MCP 契约；
不得触碰百家号或其他未授权模块，不得触碰 data/Profile/Cookie/Token/uv.lock；
不得执行真实登录、草稿、发布或删除。完成后运行 fresh 测试，创建 focused commit，SSH 推送，
并交付远端 40 位 SHA、文件清单、接口变化、测试结果、风险和未执行副作用。

你的第一条回复只报告：实际 worktree、分支、HEAD、远端、工作树状态，以及等待的任务包字段。
```
