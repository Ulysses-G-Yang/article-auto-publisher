# DRAFT 真实验收入口

`scripts/run_draft_acceptance_server.py` 是一次性、仅保存草稿的人工验收入口，
不是生产启动脚本，也不允许用于 `PUBLISH`。它解决的是“未完成真实验收时，
默认格式能力门禁会阻止测试”这一验收准备问题：能力声明只注入当前 Flask
进程内存，默认注册表和数据库不被预认证或修改。

## 启动条件

启动时必须显式传入至少一个 `--platform` 和固定确认词 `--confirmation
DRAFT_ONLY`。平台参数可重复，但只能来自当前投递目录；端口可选，监听地址
始终固定为 `127.0.0.1`，调试器和自动重载始终关闭。

示例（仅在用户明确授权具体内容、账号和 DRAFT 模式后执行）：

```powershell
C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe `
  scripts\run_draft_acceptance_server.py `
  --platform xiaoheihe `
  --confirmation DRAFT_ONLY
```

脚本不会输出账号标识、Profile、Cookie、Token、本机文件路径或原始内容。正式
验收仍必须通过 Content Studio 选择具体账号和内容，并由执行者按“一次执行、
失败即停”的门禁操作。

## 安全门

以下任一环境变量只能未设置或明确为 `false/0/no/off`；`true/1/yes/on` 及
其他模糊值都会拒绝启动：

- `PUBLISH_AFTER_DRAFT`
- `ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH`
- `LEGACY_UPLOAD_QUEUE_ENABLED`

配置中的 `publish_after_draft` 和 `legacy_upload_queue_enabled` 也必须为假。
脚本不会替用户关闭这些开关；门禁失败时应先修正环境并重新获得授权。

## 能力声明边界

只有本次 `--platform` 选择的平台在内存中获得 `FEATURE_KEYS`，未选择的平台
仍为空能力。`DEFAULT_PLATFORM_FORMAT_CAPABILITIES`、数据库记录和平台适配器
不会被修改。代码存在、单元测试通过与真实平台验收通过是三件不同的事；真实
验收完成后仍应更新平台证据矩阵，而不是把本入口的临时能力当作永久认证。

本入口禁止用于公开发布、生产部署、自动调度、重试或绕过账号租约。若平台
操作结果不确定，必须停止并人工核对，不能再次执行。
