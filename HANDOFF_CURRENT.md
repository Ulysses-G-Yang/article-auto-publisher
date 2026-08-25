# ArticleOps 当前模块化开发交接

日期：2026-08-25

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
| 当前基线 SHA | `d55ea1f40adf41cc58870e8c43f9d6fc292b178c` |
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
- `AGENTS.md` 中已经过期的目标分支和旧 P0 顺序。

`AGENTS.md` 的安全边界和 Git 纪律仍然有效，尤其包括：只暂存本任务文件、
focused commit、SSH 推送、远端 SHA 核对，以及禁止触碰用户数据和登录凭据。
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
git fetch origin integrate/weibo-draft-v0.4.3
git worktree add `
  -b "feature/<module-slug>" `
  "D:\Backup\Documents\article-auto-publisher-worktrees\<module-slug>" `
  origin/integrate/weibo-draft-v0.4.3

Set-Location "D:\Backup\Documents\article-auto-publisher-worktrees\<module-slug>"
git rev-parse HEAD
git status --short
```

起点必须输出：

```text
d55ea1f40adf41cc58870e8c43f9d6fc292b178c
```

如果远端集成分支已经前进，以 `git fetch` 后的新远端 40 位 SHA 为准，并在任务报告中明确说明；不得擅自回退到本文 SHA。

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

## 11. 最小验证与 Git 交付

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

## 12. 可直接复制给新 AI 的启动提示

```text
你负责 ArticleOps 的一个独立新模块。请先只读预检，不要续做任何旧 P0 或历史平台待办。

可信起点：
- repo：D:\Backup\Documents\article-auto-publisher
- base：origin/integrate/weibo-draft-v0.4.3
- 当前记录 SHA：d55ea1f40adf41cc58870e8c43f9d6fc292b178c
- Python：C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe
- remote：git@github.com:Ulysses-G-Yang/article-auto-publisher.git

先 fetch 远端，并从 origin/integrate/weibo-draft-v0.4.3 创建独立 feature 分支和独立 worktree。
阅读根目录 HANDOFF_CURRENT.md 和 AGENTS.md；HANDOFF_CURRENT.md 决定当前分支和协作流程，
AGENTS.md 的安全边界与 Git 纪律继续有效。

在我提供完整任务包前禁止写代码。收到任务包后，只修改允许目录；冻结 API/路由/模型/数据库/MCP 契约；
不得触碰百家号或其他未授权模块，不得触碰 data/Profile/Cookie/Token/uv.lock；
不得执行真实登录、草稿、发布或删除。完成后运行 fresh 测试，创建 focused commit，SSH 推送，
并交付远端 40 位 SHA、文件清单、接口变化、测试结果、风险和未执行副作用。

你的第一条回复只报告：实际 worktree、分支、HEAD、远端、工作树状态，以及等待的任务包字段。
```
