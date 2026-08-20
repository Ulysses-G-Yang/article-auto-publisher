# ArticleOps Windows RC 部署说明

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

- `ArticleOps-v0.4.1-rc1-<commit-sha>.zip`
- 同名 `.sha256` 文件

交付前在目标机核对 SHA256，并记录压缩包内 `RELEASE_MANIFEST.txt` 的
`source_commit`。本候选版本不自动创建正式 `v0.4.1` tag。

## 全新 Windows 运行目录

1. 安装 Python 3.12、Conda、Google Chrome Stable；不要复用开发机 Profile。
2. 解压 RC 包到一个新的目录。
3. 复制 `scripts\production_env.example.ps1` 到 `data\production_env.ps1`，设置长度不少于 32 的随机 `APP_SECRET_KEY`，再按实际内网地址填写 `MCP_BIND_HOST` 和 `MCP_ALLOWED_HOSTS`。
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

## RC 生产入口冒烟

在全新临时目录、临时 `APP_DATA_DIR` 和未登录账号下，仅验证：

1. Waitress 入口返回 `/api/status`，MCP 健康检查返回 `status=ok`。
2. `/upload` 默认是空白工作台，网络中没有历史草稿 GET 或空草稿 POST。
3. 明确使用 `draft_id` 的本地 ContentDraft 可恢复。
4. 导入一个 Word 到本地 ContentDraft 后，`cover=NONE`，不会自动创建或执行 DeliveryPlan。

冒烟结束后停止临时进程并删除临时数据目录。不得在此步骤登录平台、保存平台草稿、公开发布或删除业务数据。
