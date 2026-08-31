# ArticleOps v0.4.6 Windows 部署与升级说明

## 安全边界

- v0.4.6 面向 Content Studio 草稿工作流；`PUBLISH_AFTER_DRAFT=false` 和公开发布权限
  必须保持关闭。草稿授权不等于公开发布授权。
- 平台结果为 `RESULT_UNKNOWN` 时不得自动重试。先人工查看草稿箱或执行只读核验，避免
  已保存成功后重复创建草稿。
- 小红书当前只开放登录与账号管理。云端草稿实体证据尚未闭环，自动投递继续关闭。
- 不把 `data/`、SQLite 数据库、Cookie、Chrome Profile、Token、生产配置、日志、测试或
  QA 截图复制进发布包。
- 生产机使用独立运行目录，不要把源码 checkout 当作包含真实业务数据的运行目录。
- 真实登录、平台草稿保存和公开发布均需各自的人工授权；构建、部署和升级不执行这些动作。

## 从精确提交构建

本次发行整理以
`a49293cfc3e949f31a84a6e022c4e978d8128586` 为功能代码基线，但最终发行提交还会包含
文档和打包收口，因此不能预先把该基线当作最终交付 SHA。

在受信任的源码 checkout 中执行，`<commit-sha>` 必须是最终发行提交的完整 40 位 SHA：

```powershell
.\scripts\build_release_windows.ps1 `
  -CommitSha <commit-sha> `
  -OutputDirectory .\build\release
```

脚本只从该 SHA 的 Git archive 按白名单复制运行代码，并生成：

- `ArticleOps-v0.4.6-<commit-sha>.zip`：全新安装包。
- `ArticleOps-v0.4.6-<commit-sha>.zip.sha256`：完整包校验值。
- `ArticleOps-upgrade-v0.4.6-<commit-sha>.zip`：已有 v0.4.4/v0.4.5 业务电脑升级包。
- `ArticleOps-upgrade-v0.4.6-<commit-sha>.zip.sha256`：升级包校验值。

交付前核对两个压缩包的 SHA-256。完整包以 `RELEASE_MANIFEST.txt`、升级包以
`UPGRADE_MANIFEST.json` 中的 `source_commit` 作为代码身份。构建脚本本身不创建 tag；
正式标签和 Release 只能在包内容与清单验收通过后创建。

## 已有业务电脑原地升级

不要用完整包覆盖旧目录。把
`ArticleOps-upgrade-v0.4.6-<commit-sha>.zip` 解压到独立临时目录，并严格按
`UPGRADE_v0.4.6.md` 操作：

1. 停止业务服务，把整个 `data/` 备份到目标目录之外。
2. 核对压缩包 SHA-256 和 `UPGRADE_MANIFEST.json` 的 `source_commit`。
3. 先运行 `-ValidateOnly`；只有校验通过后才能执行实际升级。
4. 实际升级前，在同一 PowerShell 会话 dot-source 目标目录的
   `data\production_env.ps1`，或由部署系统注入同等环境变量。

最小命令顺序如下：

```powershell
$targetRoot = Read-Host "请输入现有 ArticleOps 运行目录"

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot `
  -ValidateOnly

$productionEnv = Join-Path $targetRoot "data\production_env.ps1"
. $productionEnv

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot
```

升级器会验证载荷 SHA-256 和目标目录，再停止当前项目服务、逐文件备份旧程序、覆盖清单
内代码，并在依赖清单变化时更新现有 Python 环境；最后启动服务并执行健康检查。失败时会
尝试恢复旧程序。

`data/`、SQLite 数据库、Cookie、Chrome Profile、`data\production_env.ps1`、
`uploads/`、`images/` 和历史记录始终保留。旧程序文件备份在目标目录的
`data\upgrade_backups\<UTC时间>`；它不能代替升级前对整个 `data/` 的独立备份。
升级器不会删除平台草稿或结束 Chrome。

## 全新 Windows 运行目录

1. 安装 Python 3.12、Conda、Google Chrome Stable；不要复用开发机 Profile。
2. 把 `ArticleOps-v0.4.6-<commit-sha>.zip` 解压到新的独立目录。
3. 在包根目录运行初始化脚本。初始化脚本会读取 `RELEASE_MANIFEST.txt` 并校验
   `-ExpectedCommit`，随后创建空运行目录和 Python 环境；首次新装时可生成本机
   `data\production_env.ps1`。确认 `APP_SECRET_KEY` 与 MCP 内部令牌分别为长度
   不少于 32 的随机密钥，并按实际网络填写 Host 白名单。初始账号白名单使用无法命中真实
   账号的全零 UUID，确保服务可以启动但 MCP 不能访问任何真实账号；完成登录后再替换为
   明确获准的真实 `account_id`。
4. **启动前必须显式加载配置。** `start_production_windows.ps1` 不生成、不覆写、也不
   自动读取 `data\production_env.ps1`。

```powershell
.\scripts\setup_windows.ps1 -ExpectedCommit <commit-sha>

. .\data\production_env.ps1
.\scripts\start_production_windows.ps1
```

如果生产配置由部署系统管理，可不使用本地配置文件，但必须把同等环境变量注入启动脚本
所在的进程。启动脚本只校验并继承当前进程环境，然后启动 Waitress 和 MCP；缺少必需变量
会直接拒绝启动。

停止时执行：

```powershell
.\scripts\stop_production_windows.ps1
```

停止脚本不结束 Chrome，也不删除 Profile。完整包是运行时白名单包，不包含
`requirements-dev.txt`、`tests/` 或 `pytest`；初始化时根据 `requirements.txt` 安装运行依赖，
环境检查只验证生产运行模块，仍会执行 `compileall`，并明确跳过不存在的 `pytest` 测试目录。

## 生产配置与 MCP 白名单

生产配置属于客户运行数据，不进入交付包或 Git。使用本地文件时，将配置保存在
`data\production_env.ps1`；每次手工启动前都在**当前 PowerShell 会话**中执行：

```powershell
. .\data\production_env.ps1
.\scripts\start_production_windows.ps1
```

MCP 必须保持 fail-closed：

1. 设置独立的 `ARTICLEOPS_MCP_INTERNAL_TOKEN`，不能复用 `APP_SECRET_KEY`。
2. 设置 `MCP_ALLOWED_HOSTS` 和 `MCP_FILE_SERVICE_ALLOWED_HOSTS`，只列真实获准的 Host。
3. 在 ArticleOps 账号页完成真实平台登录并确认账号为 `ACTIVE/VALID`。
4. 将获准由 CS_Admin 使用的 `account_id` 写入
   `ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS`，多个账号用逗号分隔，禁止使用 `*`。
5. 白名单复核完成后，才可将 `ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED` 改为 `true`，
   重新 dot-source 配置并重启服务。
6. `PUBLISH_AFTER_DRAFT=false`、`ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH=false` 和
   `MCP_LEGACY_MUTATIONS_ENABLED=false` 必须保持关闭。

服务恢复后，先调用 `list_platform_accounts` 核对平台、公开昵称和可用账号，再使用
`start_article_draft_delivery` 创建受控草稿任务；不得跳过账号核对直接投递。

发布包内的 `MCP_API_REFERENCE.md` 和 `mcp_server\registration.json` 供 CS_Admin 管理员
核对接口与导入配置。当前 MCP 只授予白名单账号 `draft.create`，不授予
`publish.request` 或 `publish.execute`。若提交超时或返回 `SUBMISSION_RESULT_UNKNOWN`，
必须人工核对平台，不得自动重复提交。

## 生产入口离线冒烟

在全新临时目录、临时 `APP_DATA_DIR` 和未登录账号下，只验证：

1. Waitress `/api/status` 返回成功，MCP `/healthz` 返回 `status=ok`。
2. `/upload` 默认是空白工作台，网络中没有历史草稿 GET 或空草稿 POST。
3. 明确使用 `draft_id` 的本地 ContentDraft 可恢复。
4. 导入一个 Word 到本地 ContentDraft 后，`cover=NONE`，不会自动创建或执行
   DeliveryPlan。

冒烟结束后停止临时进程并删除临时数据目录。不得在此步骤登录平台、保存平台草稿、公开
发布或删除业务数据。
