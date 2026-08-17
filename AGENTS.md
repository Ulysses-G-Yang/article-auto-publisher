# ArticleOps 执行边界

本文件是当前 `feat/zhihu-real-delivery` 轮次的根目录执行约束。

## Canonical 仓库与分支

- Canonical repo：`D:\Backup\Documents\article-auto-publisher`
- 当前目标分支：`feat/zhihu-real-delivery`
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
- 推送后必须核对 `origin/feat/zhihu-real-delivery` 的完整 40 位 SHA，再报告结果。
- 不 force push，不恢复或提交无关/历史协作文件。

## 当前 P0 顺序与暂停条件

1. 创建并交付本文件。
2. 复用既有小红书 Profile 与账号租约，执行一次发布页只读 DOM 探测：只导航并读取 DOM，不输入正文、不选文件、不保存、不点击「新的创作」、不登录、不清 Cookie、不改变业务状态。
3. 仅依据真实探测证据选择正文图片控件；删除危险 fallback，无法证明正文控件时 fail closed，不猜测生产 selector。
4. 补齐控件选择、图片数量稳定增加、路径脱敏与 `completed/partial/failed` 测试，并同步小红书风控文档。
5. 验证、focused commit、SSH push 后暂停，等待下一次人工确认。

任何探测页没有已存在草稿的正文上传控件、无法证明控件属于正文编辑器、出现登录/验证码/异常导航、或需要创建/保存草稿时，立即停止；不得自动创建草稿、上传、保存、发布、删除，也不得进行六平台验收或重新启用微博投递。
