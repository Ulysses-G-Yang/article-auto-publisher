# ArticleOps 执行边界

本文件是当前 `feature/ai-guided-publication` 轮次的根目录执行约束。

## Canonical 仓库与分支

- Canonical repo：`D:\Backup\Documents\article-auto-publisher`
- 当前目标分支：`feature/ai-guided-publication`
- GitHub 远端必须使用 SSH：`git@github.com:Ulysses-G-Yang/article-auto-publisher.git`
- 推送前使用：`GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new'`
- 主线程只做审查与确认；本执行线程是本任务唯一写入代码的线程。

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
- 推送后必须核对 `origin/feature/ai-guided-publication` 的完整 40 位 SHA，再报告结果。
- 不 force push，不恢复或提交无关/历史协作文件。

## 本轮执行范围与暂停条件

本轮仅执行阶段 1—3；不得扩展到真实平台验收、公开发布、数据/Profile/Cookie/Token 处理或服务启停。

1. 阶段 1：冻结版本与发布边界基线，创建 `docs/PUBLICATION_BASELINE.md` 并同步本文件；完成定向检查后暂停，等待主线程审查并交付本批次。
2. 阶段 2：实现发布选项结构化契约及其后端传播、幂等和公开确认指纹；只允许修改任务明确列出的后端契约/模型/数据库/服务文件与相关测试，不执行真实平台动作；完成定向检查后暂停，等待主线程审查。
3. 阶段 3：完成账号级 `publish-options` API、`BasePlatform` 默认 `unsupported`、小黑盒/中关村在线候选发现实现与离线测试；其他平台明确 `unsupported`，不调用真实接口。完成后暂停，进入通用测试、主线程审查、聚焦提交、SSH 推送当前分支及远端完整 40 位 SHA 核对，等待下一轮明确确认。

任何阶段出现账号/Profile、数据、Cookie、Token 或公开发布边界不明，立即停止并报告；不得自动登录、创建/保存草稿、上传、发布、删除或重试不确定的副作用。
