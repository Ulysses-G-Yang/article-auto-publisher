# ArticleOps v0.4.4 原地升级说明

本说明适用于已有 ArticleOps v0.4.3 完整安装的 Windows 运行目录。只交付
`ArticleOps-upgrade-v0.4.4-<source-sha>.zip`；不要用完整安装包覆盖旧目录，
不要重新运行 `setup_windows.ps1`，也不要把升级包解压到业务目录。

## 客户操作

1. 停止正在执行的业务任务，在临时目录解压升级包。先核对同名 `.sha256` 文件，
   并确认 `UPGRADE_MANIFEST.json` 的 `source_commit` 是交付单记录的 40 位 SHA。
2. 确认目标目录是已有 `app.py` 的 ArticleOps 运行目录，在升级包根目录执行以下校验。
   `ValidateOnly` 不停止服务、不复制文件、也不修改目标目录：

   ```powershell
   $targetRoot = Read-Host "请输入现有 ArticleOps 运行目录"
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\apply_upgrade_windows.ps1 -TargetRoot $targetRoot -ValidateOnly
   ```

3. 校验通过后再执行实际升级：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\apply_upgrade_windows.ps1 -TargetRoot $targetRoot
   ```

升级器只更新 `UPGRADE_MANIFEST.json` 列出的程序、前端、文档和必要依赖文件，
并调用目标目录原有的停止/启动脚本。升级载荷明确不包含客户已有的
`setup_windows.ps1`、`start_production_windows.ps1`、`stop_production_windows.ps1`
或 `production_env.example.ps1`，因此不会覆盖原启动方式或配置模板。

## 保留与失败处理

`data/`、SQLite 数据库、`uploads/`、`images/`、Cookie、Chrome Profile、
`data\production_env.ps1` 和端口配置均保留；旧程序备份位于
`data\upgrade_backups\<UTC时间>`。升级器不会登录、发布、删除平台内容，也不会
自动创建新的生产配置；客户已启用的配置和账号信息沿用原目录。本轮只做离线包与
隔离目录验收，未执行真实平台草稿保存或公开发布。

如果校验失败，不要继续执行实际升级。若实际升级结果不确定，先保留备份并人工
检查服务与目录状态，不要重复执行升级命令；其它版本或非完整 v0.4.3 目录先联系
交付方核对，不能直接套用本包。
