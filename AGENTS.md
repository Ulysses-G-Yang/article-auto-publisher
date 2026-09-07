# ArticleOps 执行边界

本文件是当前 `main` 收口后的根目录执行约束。具体任务仍以用户明确授权和
`HANDOFF_CURRENT.md` 的当前状态为准。

## Canonical 仓库与分支

- Canonical repo：`D:\Backup\Documents\article-auto-publisher`
- 唯一长期业务/开发基线：`main`
- 当前主工作树：`D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`
- GitHub 远端必须使用 SSH：`git@github.com:Ulysses-G-Yang/article-auto-publisher.git`
- 推送前使用：`GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new'`
- 当前根任务同时负责实现、审查与交付；其他审查任务只读，不得同时写入同一工作树。

## 运行时与数据安全

- 项目 Python 固定为 `C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe`（Python 3.12）。
- 绝不触碰未跟踪的 `uv.lock`。
- 绝不清理、复制、覆盖或提交 `data/`、任何真实 Profile、Cookie、Token、原始响应或用户内容。
- 公开发布在本轮默认关闭且不修改；未来若任务明确授权，只能按该任务单独审查和执行。
- 真实登录、真实草稿保存、公开发布、删除动作均需单独取得用户明确确认。
- 小红书失败不得自动重试；遇到不确定状态立即停止并报告。

## Git 纪律

- 每轮先运行定向测试，再运行全量测试、Ruff 与 `git diff --check`。
- 每次只暂存本任务相关文件，禁止 `git add -A`，不得夹带 `uv.lock` 或用户数据。
- 每个聚焦批次创建语义明确的 focused commit，推送 `main` 或任务明确的临时分支。
- 推送后必须核对当前目标远端分支的完整 40 位 SHA，再报告结果。
- 不 force push，不恢复或提交无关/历史协作文件。
- 每个任务完成时更新 `HANDOFF_CURRENT.md`，并与该任务实现放在同一 focused commit 中。

## 本轮执行范围与暂停条件

- 内置数据中心页面、导航和专用静态资源保持下线。
- `/data-center/api/dashboard`、`/data-center/healthz` 和 `/api/legacy-summary`
  作为外部看板的只读兼容接口保留。
- `/data-center/` 旧书签只重定向到主站，不恢复旧页面。
- Windows 源码更新必须保留 `data`、`uploads`、`images` 和其他运行数据；依赖变化与任何
  数据迁移都必须在任务/交接中显式说明并单独验证。
- 禁止使用 `git reset --hard`、`git clean` 或其他会覆盖、删除用户文件的命令；更新前核对
  安装目录、监听 PID 与绝对入口。
- 启动前先核对监听 PID 与绝对入口；只允许当前 worktree 的最新提交运行，
  不保留旧 checkout 的 Flask/MCP 进程。
- Windows 桌面快捷入口必须从自身安装目录读取现有生产配置，动态使用本机
  `FLASK_PORT`，同时核对 Web/MCP 进程归属；不得硬编码开发机 IP 或误杀其他目录进程。
- 更新只从 `main` 获取源码；不再制作 ZIP 或新的发行包，既有 release 仅作历史回滚参考。
- 完整诊断/脱敏包已取消，不列为待办；数据看板属于项目外部系统，不由本仓库接管。

出现账号/Profile、数据、Cookie、Token 或公开发布边界不明时立即停止；不得自动登录、
创建/保存草稿、上传、发布、删除或重试不确定的副作用。
