# ArticleOps Windows 部署与升级说明

## 安全边界

- RC 只承诺 Content Studio 草稿工作流；`PUBLISH_AFTER_DRAFT=false` 必须保持关闭。
- 不把 `data/`、SQLite 数据库、Cookie、Chrome Profile、Token、日志、测试或 QA 截图复制进发布包。
- 生产机使用独立运行目录；不要把源码 checkout 当作运行数据目录。
- 公开发布、真实登录和平台草稿保存都需要单独的人工授权。本说明不执行这些动作。

## 从精确提交构建

在受信任的源码 checkout 中执行，`<commit-sha>` 必须是完整 40 位 commit SHA：

```powershell
.\scripts\build_release_windows.ps1 `
  -CommitSha <commit-sha> `
  -OutputDirectory .\build\release
```

脚本只从该 SHA 的 Git archive 按白名单复制运行代码，并生成：

- `ArticleOps-v0.4.3-<commit-sha>.zip`：全新安装包。
- `ArticleOps-upgrade-v0.4.3-<commit-sha>.zip`：已有业务电脑升级包。
- 两个压缩包各自同名的 `.sha256` 文件。

交付前在目标机核对 SHA256，并记录压缩包内 `RELEASE_MANIFEST.txt` 的
`source_commit`。本版本不自动创建正式 tag。

## 已有业务电脑原地升级

不要用完整包覆盖旧目录。解压 `ArticleOps-upgrade-v0.4.3-<commit-sha>.zip` 到
临时目录，然后执行：

```powershell
$targetRoot = Read-Host "请输入现有 ArticleOps 运行目录"
.\apply_upgrade_windows.ps1 -TargetRoot $targetRoot -ValidateOnly
.\apply_upgrade_windows.ps1 -TargetRoot $targetRoot
```

第一条命令只校验压缩包与目标目录，不停止服务或写文件；通过后再执行第二条命令。
升级器会验证载荷 SHA-256 和目标目录，再停止当前项目服务，逐文件备份旧程序、
覆盖清单内代码，并在依赖清单变化时更新现有 Python 环境。最后启动服务并执行健康
检查；任何阶段失败都会尝试恢复旧程序。

以下内容始终保留：`data/`、SQLite 数据库、Cookie、Chrome Profile、
`data\production_env.ps1`、上传内容和历史记录。代码备份保存在目标目录的
`data\upgrade_backups\<UTC时间>`，升级器不会删除平台草稿或结束 Chrome。

## 全新 Windows 运行目录

1. 安装 Python 3.12、Conda、Google Chrome Stable；不要复用开发机 Profile。
2. 解压 RC 包到一个新的目录。
3. 复制 `scripts\production_env.example.ps1` 到 `data\production_env.ps1`，设置长度
   不少于 32 的随机 `APP_SECRET_KEY`，再按实际内网地址填写 `MCP_BIND_HOST` 和
   `MCP_ALLOWED_HOSTS`。初始化脚本会单独生成 MCP 内部令牌，绝不复用应用密钥。
4. 在包根目录运行：

```powershell
.\scripts\setup_windows.ps1 -ExpectedCommit <commit-sha>
.\scripts\start_production_windows.ps1
```

`setup_windows.ps1` 会创建 Python 环境、空的运行目录和 Waitress/MCP 所需依赖；
`start_production_windows.ps1` 会检查本项目端口、启动 `run_flask_production.py`
和 MCP，并等待健康检查。停止时执行：

```powershell
.\scripts\stop_production_windows.ps1
```

该脚本不结束 Chrome，也不删除 Profile。

RC 压缩包是运行时白名单包，不包含 `requirements-dev.txt` 或 `tests/`。因此
`setup_windows.ps1` 在包内安装 `requirements.txt`，仍会执行 `compileall`，并在日志中
明确跳过不存在的 `pytest`；源码 checkout 则继续安装开发依赖并执行完整回归。

## CS_Admin MCP 启用步骤

发布包内的 `MCP_API_REFERENCE.md` 和 `mcp_server\registration.json` 是交付给
CS_Admin 管理员的接口文档与一键导入配置。默认配置保持 fail-closed：

1. 先在 ArticleOps 账号页完成真实平台登录，并确认账号为 `ACTIVE/VALID`。
2. 把获准由 CS_Admin 使用的 `account_id` 写入
   `ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS`，多个账号用逗号分隔，禁止使用 `*`。
3. 将 `ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED` 改为 `true`，重启 Flask 与 MCP。
4. 在 CS_Admin 导入 `mcp_server\registration.json`，点击“同步工具”并配置角色授权。
5. 先调用 `list_platform_accounts` 核对公开昵称，再用
   `start_article_draft_delivery` 提交受控 DOCX 临时地址与草稿目标。

当前 MCP 只授予白名单账号 `draft.create`，不授予 `publish.request` 或
`publish.execute`。如果提交超时，任务返回 `SUBMISSION_RESULT_UNKNOWN`，必须人工
核对平台，不得自动重复提交。业务域名、可信代理和文件服务 Host 白名单的配置及
CS_Admin 导入 JSON 见 `MCP_API_REFERENCE.md`。

## RC 生产入口冒烟

在全新临时目录、临时 `APP_DATA_DIR` 和未登录账号下，仅验证：

1. Waitress 入口返回 `/api/status`，MCP 健康检查返回 `status=ok`。
2. `/upload` 默认是空白工作台，网络中没有历史草稿 GET 或空草稿 POST。
3. 明确使用 `draft_id` 的本地 ContentDraft 可恢复。
4. 导入一个 Word 到本地 ContentDraft 后，`cover=NONE`，不会自动创建或执行 DeliveryPlan。

冒烟结束后停止临时进程并删除临时数据目录。不得在此步骤登录平台、保存平台草稿、公开发布或删除业务数据。
