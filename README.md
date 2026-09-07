# ArticleOps

ArticleOps 的业务源码唯一长期入口是 GitHub 私有仓库的 `main` 分支：

```text
git@github.com:Ulysses-G-Yang/article-auto-publisher.git
```

本轮主工作树为
`D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`。
业务更新只从 `main` 获取源码；历史功能分支只作为带说明的归档标签保留，不作为开发基线。
数据看板是项目外部系统，本仓库不接管其部署或数据。

## Windows 源码更新

在管理员确认的现有安装源码目录中按以下顺序操作：

1. 停止该安装目录归属的 Web/MCP 服务；先核对监听 PID 和绝对入口，不要按端口或名称误杀其他目录的进程。
2. 确认工作树干净：运行 `git status --short --branch`，不得有未提交变更；不要用
   `git clean`、`git reset --hard` 或其他方式清理。随后执行 `git fetch origin`。不要暂存或覆盖
   `data/`、真实 Profile、Cookie、Token、用户内容或未跟踪的 `uv.lock`。
3. 明确切换到本地 `main` 并验证当前分支，再拉取：

   - 若 `git branch --list main` 已列出本地 `main`，执行 `git switch main`；
   - 若本地没有 `main`，先确认 `origin/main` 存在，由管理员执行
     `git switch --track -c main origin/main` 创建跟踪分支；
   - 执行 `git branch --show-current`，必须输出 `main`，再执行
     `git pull --ff-only origin main`。如果切换或 fast-forward 失败就停止，由管理员处理，
     不自动 reset、rebase 或 force push。

4. 如果依赖文件确实发生变化，使用业务机已安装的 Python 3.12 环境安装依赖：

   ```powershell
   conda run -n article-publisher-py312 python -c "import sys; print(sys.executable)"
   conda run -n article-publisher-py312 python -m pip install -r requirements.txt
   ```

   不要照抄开发机的 `C:\Users\Administrator\...` 路径；若业务机未将 `conda` 放入 PATH，改用
   上一步确认的该环境实际 `python.exe` 路径。

5. 继续使用现有 `scripts\launch_articleops_windows.ps1` 启动入口。它会按既有流程加载安装目录
   `data\production_env.ps1`，并让启动的 Web/MCP 进程继承配置；更新流程不新建脚本，也不自动生成、
   覆写或迁移真实配置文件。端口继续由本机 `FLASK_PORT` 动态决定。
6. 启动后再次核对新 PID、绝对入口、`git rev-parse HEAD` 和健康接口；本轮不改变监听 host/port。

没有 `.git` 的旧 ZIP/发行包安装不能直接 `git pull`。首次切换源码前，管理员必须确认现有源码目录
和既有数据的绝对路径，再决定如何接入 `main`；不能盲目切到一个新的空 `data` 目录。

后续不再制作 ZIP 或新的发行包；现有 release 仅用于历史回滚。完整诊断/脱敏包已取消，不作为待办。

## AI 生成平台建议

AI 能力默认关闭，配置说明见 [`docs/PUBLICATION_AI.md`](docs/PUBLICATION_AI.md)。Windows 生产配置
仍由管理员维护本机 `data\production_env.ps1`；源码更新和开发操作不读取或改写真实配置文件，
运行启动器会按既有流程读取该文件并让服务进程继承配置。本仓库只提供注释示例，不提交真实文件。
运维至少关注以下四个核心环境变量：

```text
ARTICLEOPS_AI_GUIDANCE_ENABLED
DEEPSEEK_BASE_URL
DEEPSEEK_MODEL
DEEPSEEK_API_KEY
```

长期 API Key 通过本机环境注入；网页“保存设置”输入的临时 Key 只保存在当前 Python 进程内存中，
不写入 YAML、Git、日志、数据库或响应，重启后回到本机环境。当前没有 Web 管理员登录，不能把
CSRF 或页面隐藏当作鉴权。
公开发布在本轮保持默认关闭，任何未来真实副作用都需要单独明确授权。

## 当前交接边界

本轮收口后才开始下一阶段目标：单平台仅自己可见的真实发布 → AI 真实选项写入 → 多平台多账号。
这不是当前已有能力的宣称。真实登录、平台草稿保存、公开发布、删除和不确定状态重试均不属于
源码更新流程。

每个完成任务都必须同步更新 `HANDOFF_CURRENT.md`，并与实现放在同一个 focused commit 中；提交时
只暂存任务文件，使用 SSH 推送并核对远端完整 40 位 SHA。
