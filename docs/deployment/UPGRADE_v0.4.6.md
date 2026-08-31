# ArticleOps v0.4.6 原地升级说明

本说明用于已有 ArticleOps Windows 业务电脑。只使用
`ArticleOps-upgrade-v0.4.6-<source-sha>.zip`，不要用完整包覆盖旧目录，不要重新运行
`setup_windows.ps1`，也不要把升级包直接解压到业务目录。

## 升级前

1. 停止新增业务任务，在独立临时目录解压升级包。
2. 按同名 `.sha256` 文件核对 ZIP；确认 `UPGRADE_MANIFEST.json` 中的 `source_commit` 与
   交付单一致。
3. 停止目标目录的生产服务。
4. 把目标目录的整个 `data/` 复制到目标目录之外，并核对备份文件数量。

`data/` 包含数据库、账号会话、Cookie、Chrome Profile 和本机配置。没有完整数据备份时
不要升级。

## 只校验

在升级包解压后的根目录运行：

```powershell
$targetRoot = (Resolve-Path -LiteralPath (Read-Host "请输入现有 ArticleOps 运行目录")).Path

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot `
  -ValidateOnly
```

必须看到“升级包校验通过”。`ValidateOnly` 不停止服务、不复制文件、不修改目标目录。

## 执行升级

启动脚本不会生成、覆写或自动读取生产配置。若本机使用
`data\production_env.ps1`，必须在同一个 PowerShell 会话中先加载：

```powershell
$productionEnv = Join-Path $targetRoot "data\production_env.ps1"
. $productionEnv

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\apply_upgrade_windows.ps1 `
  -TargetRoot $targetRoot
```

如果由部署系统管理配置，必须把同等环境变量注入本次升级进程。公开发布相关开关必须继续
保持关闭。

## 升级边界

升级器更新程序、前端、文档和必要依赖；保留：

- `data/`、SQLite 数据库、Cookie、Chrome Profile、账号会话和生产配置；
- `uploads/`、`images/`、上传内容和历史记录；
- 当前端口、Host 白名单、账号白名单和公开发布关闭配置；
- 客户已有的 Windows 启停脚本和配置模板。

被替换的程序文件会备份到 `data\upgrade_backups\<UTC时间>`，但该备份不能代替升级前的
完整 `data/` 备份。

## 升级后只读核对

1. 确认升级器输出的 `source_commit`。
2. 检查 Web `/api/status` 和 MCP `/healthz`。
3. 打开账号页，确认原账号和登录状态仍在；不要为验收而重新登录或清理 Profile。
4. 不保存平台草稿、不公开发布；真实平台验收应单独授权。

若服务或结果不确定，保留数据备份和程序回滚目录，先诊断，不要重复执行升级或平台操作。
