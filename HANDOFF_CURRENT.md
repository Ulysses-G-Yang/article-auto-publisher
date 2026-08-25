# ArticleOps 当前模块化开发交接

日期：2026-08-25

> 本版本由 `feature/shared-sdk-heartbeat-delay` 分支更新：新增
> `delivery_tools` 外挂工具模块（共享 Playwright SDK、Cookie 心跳、
> 随机真人延时），详见 §13。

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

## 11. delivery_tools 外挂模块（feature/shared-sdk-heartbeat-delay）

三个外挂模块已实现于 `src/delivery_tools/`，设计为不修改现有发布业务逻辑；
现有 `content_studio/`、`account_sessions/`、`article_mvp/`、`platforms/`、
`app.py`、`config.py`、`pyproject.toml` 均未改动。

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

## 12. 最小验证与 Git 交付

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

## 13. 可直接复制给新 AI 的启动提示

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
