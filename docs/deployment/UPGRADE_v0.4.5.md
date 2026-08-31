# ArticleOps v0.4.5 原地升级说明

本说明适用于已经按 v0.4.4 完整安装或升级完成的 Windows 运行目录。已有业务电脑只使用
`ArticleOps-upgrade-v0.4.5-<source-sha>.zip`；不要用完整安装包覆盖旧目录，不要重新运行
`setup_windows.ps1`，也不要把升级包直接解压到业务目录。

文件名中的 `<source-sha>` 是最终构建提交的完整 40 位 SHA。本次发行整理的功能代码基线
是 `a49293cfc3e949f31a84a6e022c4e978d8128586`，但它不是预先指定的最终交付 SHA；最终
身份以升级包内 `UPGRADE_MANIFEST.json` 的 `source_commit` 为准。

## 升级前准备

1. 停止新增业务任务，在一个独立临时目录解压升级包。
2. 对照同名 `.sha256` 文件核对升级包 SHA-256，并确认 `UPGRADE_MANIFEST.json` 的
   `source_commit` 与交付单一致。
3. 确认目标目录包含 `app.py`、`config.py` 和 `scripts`，并停止该目录的生产服务。
4. **把整个 `data/` 复制到目标目录之外的安全位置。** `data/` 包含数据库、账号会话、
   Cookie、Chrome Profile 和本机配置；升级器的程序文件回滚备份不能代替这份数据备份。

可在当前 PowerShell 会话中执行以下示例；请记录输出的备份目录：

```powershell
$targetRoot = (Resolve-Path -LiteralPath (Read-Host "请输入现有 ArticleOps 运行目录")).Path
& (Join-Path $targetRoot "scripts\stop_production_windows.ps1")

$dataSource = Join-Path $targetRoot "data"
$dataBackup = Join-Path (Split-Path -Parent $targetRoot) `
  ("ArticleOps-data-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Copy-Item -LiteralPath $dataSource -Destination $dataBackup -Recurse -ErrorAction Stop
$sourceFileCount = @(Get-ChildItem -LiteralPath $dataSource -Recurse -File).Count
$backupFileCount = @(Get-ChildItem -LiteralPath $dataBackup -Recurse -File).Count
if (-not (Test-Path -LiteralPath $dataBackup -PathType Container) -or
    $sourceFileCount -ne $backupFileCount) {
  throw "data 备份失败，停止升级"
}
Write-Host "data 备份完成：$dataBackup"
```

如果无法停止服务、`data/` 不存在或备份无法确认完整，停止升级并联系交付方。

## 第一步：只校验，不升级

在**升级包解压后的根目录**运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot `
  -ValidateOnly
```

`ValidateOnly` 只校验目标目录、清单和载荷哈希；它不复制文件，也不会修改目标目录。
必须看到“升级包校验通过”后才能继续。任何校验错误都应停止，不能跳过校验直接升级。

## 第二步：注入生产配置并升级

启动脚本不会生成、覆写或自动读取 `data\production_env.ps1`。实际升级最后会调用目标
目录原有的启动脚本，因此必须让发起升级的 PowerShell 会话先具有完整生产环境变量。

如果本机使用 `data\production_env.ps1`，在**同一个 PowerShell 会话**中执行：

```powershell
$productionEnv = Join-Path $targetRoot "data\production_env.ps1"
. $productionEnv

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot
```

如果由部署系统管理配置，可不 dot-source 文件，但部署系统必须把同等变量注入运行升级
命令的进程。至少应确认 `APP_ENV=production`、应用密钥、Flask 地址、MCP 地址、独立 MCP
内部令牌、Host 白名单、文件服务 Host 白名单和账号 ID 白名单均已设置。MCP 账号白名单
不得使用 `*`；`ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED` 只在白名单审核完成后才能开启。
`PUBLISH_AFTER_DRAFT` 和公开发布权限必须保持关闭。

## 升级会更新和保留什么

升级器只更新 `UPGRADE_MANIFEST.json` 列出的程序、前端、文档和必要依赖，并调用目标
目录已有的停止/启动脚本。升级载荷不覆盖客户已有的 `setup_windows.ps1`、
`start_production_windows.ps1`、`stop_production_windows.ps1` 或
`production_env.example.ps1`。

以下内容会保留：

- `data/` 及其中的 SQLite 数据库、Cookie、Chrome Profile、账号会话和
  `production_env.ps1`；
- `uploads/`、`images/`、上传内容和历史记录；
- 客户当前的端口、Host 白名单、账号白名单和公开发布关闭配置。

被替换的旧程序文件会备份到 `data\upgrade_backups\<UTC时间>`。这个目录用于程序回滚，
不是完整的业务数据备份。

## 升级后核对

1. 确认升级器输出“升级完成”，并记录其中的 `source_commit` 和程序备份目录。
2. 打开 `/api/status` 和 MCP `/healthz`，确认两个服务健康。
3. 打开账号页，确认原账号和登录状态仍在；不要为验证升级而重新登录或清理 Profile。
4. 检查工作台状态已经中文化。不要为验收升级而保存平台草稿或公开发布。

如果实际升级结果不确定，保留升级前的独立 `data/` 备份和
`data\upgrade_backups\<UTC时间>`，先人工检查服务和目录状态，不要重复执行升级命令。
平台操作若返回 `RESULT_UNKNOWN`，同样不得自动重试，应先人工查看草稿箱或使用只读核验。

小红书仍只开放登录与账号管理；因云端草稿证据尚未闭环，自动投递继续关闭。
