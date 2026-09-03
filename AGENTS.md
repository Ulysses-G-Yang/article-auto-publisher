# ArticleOps 执行边界

本文件是当前 `refactor/dashboard-ai-model-picker` 轮次的根目录执行约束。

## Canonical 仓库与分支

- Canonical repo：`D:\Backup\Documents\article-auto-publisher`
- 当前目标分支：`refactor/dashboard-ai-model-picker`
- GitHub 远端必须使用 SSH：`git@github.com:Ulysses-G-Yang/article-auto-publisher.git`
- 推送前使用：`GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new'`
- 当前根任务同时负责实现、审查与交付；其他审查任务只读，不得同时写入同一工作树。

## 运行时与数据安全

- 项目 Python 固定为 `C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe`（Python 3.12）。
- 绝不触碰未跟踪的 `uv.lock`。
- 绝不清理、复制、覆盖或提交 `data/`、任何真实 Profile、Cookie、Token、原始响应或用户内容。
- 公开发布始终关闭；不得修改任何公开发布开关为开启状态。
- 真实登录、真实草稿保存、公开发布、删除动作均需单独取得用户明确确认。
- 小红书失败不得自动重试；遇到不确定状态立即停止并报告。

## Git 纪律

- 每轮先运行定向测试，再运行全量测试、Ruff 与 `git diff --check`。
- 每次只暂存本任务相关文件，禁止 `git add -A`，不得夹带 `uv.lock` 或用户数据。
- 每个聚焦批次创建语义明确的 focused commit，推送当前目标分支。
- 推送后必须核对 `origin/refactor/dashboard-ai-model-picker` 的完整 40 位 SHA，再报告结果。
- 不 force push，不恢复或提交无关/历史协作文件。

## 本轮执行范围与暂停条件

- 内置数据中心页面、导航和专用静态资源保持下线。
- `/data-center/api/dashboard`、`/data-center/healthz` 和 `/api/legacy-summary`
  作为外部看板的只读兼容接口保留。
- `/data-center/` 旧书签只重定向到主站，不恢复旧页面。
- Windows 升级清单必须显式列出已退役程序文件；删除前备份，失败时恢复，
  且不得删除 `data`、`uploads`、`images` 或任何运行数据。
- 启动前先核对监听 PID 与绝对入口；只允许当前 worktree 的最新提交运行，
  不保留旧 checkout 的 Flask/MCP 进程。

出现账号/Profile、数据、Cookie、Token 或公开发布边界不明时立即停止；不得自动登录、
创建/保存草稿、上传、发布、删除或重试不确定的副作用。
