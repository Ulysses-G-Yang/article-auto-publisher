# ArticleOps 执行边界

本文件是当前 `feature/ai-guided-publication` 轮次的根目录执行约束。

## Canonical 仓库与分支

- Canonical repo：`D:\Backup\Documents\article-auto-publisher`
- 当前目标分支：`feature/ai-guided-publication`
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
- 推送后必须核对 `origin/feature/ai-guided-publication` 的完整 40 位 SHA，再报告结果。
- 不 force push，不恢复或提交无关/历史协作文件。

## 本轮执行范围与暂停条件

本轮已扩展到阶段 5；不得扩展到真实平台验收、公开发布、数据/Profile/Cookie/Token 处理或现役服务启停。

阶段 5 已由用户明确授权：实现 DeepSeek 运行期设置页面和创作工作台只读建议入口，完成隔离前后端联调与浏览器验收；不执行真实 AI 调用、平台操作或公开发布，不接入 MCP。

1. 阶段 1：冻结版本与发布边界基线，创建 `docs/PUBLICATION_BASELINE.md` 并同步本文件；完成定向检查后暂停，等待主线程审查并交付本批次。
2. 阶段 2：实现发布选项结构化契约及其后端传播、幂等和公开确认指纹；只允许修改任务明确列出的后端契约/模型/数据库/服务文件与相关测试，不执行真实平台动作；完成定向检查后暂停，等待主线程审查。
3. 阶段 3：完成账号级 `publish-options` API、`BasePlatform` 默认 `unsupported`、小黑盒/中关村在线候选发现实现与离线测试；其他平台明确 `unsupported`，不调用真实接口。完成后暂停，进入通用测试、主线程审查、聚焦提交、SSH 推送当前分支及远端完整 40 位 SHA 核对，等待下一轮明确确认。
4. 阶段 4：完成 DeepSeek 配置读取、严格 JSON 客户端和草稿级只读发布建议 API；默认关闭，不自动选择或投递。
5. 阶段 5：增加 `/settings/ai`、运行期内存设置 API、连接测试及 `/upload` 建议展示；API Key 不落盘、不回显，服务重启后回退环境变量。设置页当前只适用于可信单用户网络，侧边栏可见性与 CSRF 不等于真实管理员鉴权。

任何阶段出现账号/Profile、数据、Cookie、Token 或公开发布边界不明，立即停止并报告；不得自动登录、创建/保存草稿、上传、发布、删除或重试不确定的副作用。
