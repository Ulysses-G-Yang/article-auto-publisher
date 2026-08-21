# ⚠️ 小红书风控标记（RISK CONTROL — MUST READ）

> **状态：永久生效的注意事项。所有涉及小红书（xiaohongshu）的任务，必须先读本文。**
> 建立时间：2026-08-14（用户明确指示：小红书风控很严，以后所有任务都要小心）

## 1. 为什么标记

小红书是当前接入列表中**风控最严**的平台：

- 登录页即加载重型反爬：`as.xiaohongshu.com/api/sec/v1/*` 签名脚本、
  `redcaptcha` 人机验证码、`sec_poison_id` / `websectiga` 等反爬 cookie。
- 高频、非人类化、失败即重试的操作模式很容易触发验证码、临时封禁甚至
  账号风控（设备标记）。
- 一旦账号被标记，可能影响真实账号的正常使用——**这是用户最在意的损失**。

## 2. 硬性纪律（违反 = 事故）

1. **绝不高频操作**：登录/验证/投递全部走隔离 Profile（`data/account_sessions/profiles/xiaohongshu/*`），
   禁止共享 Profile；单次任务内轮询/点击保持人类节奏（simulator 随机延迟）。
2. **失败不自动重试**：登录超时、验证失败、投递失败一律如实上报，等待人工决策；
   禁止在同一会话内连续多次触发登录或重试平台动作（风控触发器）。
3. **不清理反爬 cookie**：`sec_poison_id`、`websectiga`、`a1`、`gid`、`webId` 等
   属于平台风控体系，**禁止清除、改写、复制或提交**。
4. **身份/投递接口只读捕获**：平台接口带签名与验证码保护，裸 fetch 会被拒；
   只允许「捕获页面自身响应」模式（同小黑盒 restore_login 模式），
   禁止绕过签名或伪造请求。
5. **真实草稿/公开发布必须用户明确确认**：账号选择 + 内容 + 模式（DRAFT/PUBLISH）
   缺一不可；公开发布在总开关关闭前一律拒绝。
6. **验收失败如实报告**：任何一步未验证通过，不得宣称图片链路可用；
   公开发布开关始终保持关闭，适配器在未证实正文控件时必须 fail closed。
7. **探测先行、改动最小**：任何新的真实操作前，先用只读探测确认页面结构；
   代码改动只限于必要的最小适配层，失败不得自动重试。

## 3. 当前状态（2026-08-21 复核）

- 账号链路：登录（扫码）/ 会话检测 / 身份捕获已接入，目录
  `account_enabled=True`（`src/account_sessions/platform_catalog.py`）。
- 文字/图片写入链路：隔离 Profile 内可完成排版，但**不能作为云端草稿验收**。
  2026-08-21 使用同一账号 Cookie 启动不带 LocalStorage/IndexedDB 的新浏览器上下文，
  长文草稿数从原 Profile 的 16 条变为 0 条；因此“草稿箱”属于浏览器本地状态。
- 图片排版：真实编辑器没有常驻 file input；点击
  已冻结 SVG 指纹的正文图片工具栏按钮后才产生临时 FileChooser。适配器验证
  `accept/multiple/cover` 属性后只调用一次 `set_files`，并以 TipTap 正文图片数
  稳定增加为初步成功。随后必须点击一次「一键排版」，等待平台把临时 `blob:`
  图片转成远程资源，并验证 7/7 图片实际加载。2026-08-20 使用用户指定 Word
  完成 29 个逻辑 token（17 段普通文字、5 个 H2、7 张正文图片）的同名草稿
  排版、暂存和重开核验。封面面板只有平台生成的“有图/无图”封面及作者、摘要，
  没有精确选择正文首图的控件；`FIRST_BODY_IMAGE` 仍为真实失败，完整 Word 未闭环。
- 公开发布：**关闭**。不得通过配置、适配器或测试打开公开发布。
- 适配器文件：`platforms/xiaohongshu.py`；身份提取：`src/account_sessions/identity.py`
  `_extract_xiaohongshu`；平台工厂：`src/account_sessions/account_service.py`。

## 4. 后续小红书回归验收要求

1. 默认固定落地页模式只探测 `https://creator.xiaohongshu.com/publish/publish`；
   若需等待已有草稿，必须显式运行：
   `probe_xiaohongshu_editor.py --account-id <id> --manual-handoff --handoff-timeout-seconds 300`。
2. `--manual-handoff` 复用隔离 Profile 与账号租约，启动一个受控 persistent context，
   然后在同一个 page 上等待已有草稿编辑器出现。主线程未来可用 Computer Use 导航这一个受控页面；
   普通浏览器、普通标签页或另一上下文中的手动草稿状态不会传递。
3. 等待期间只读取当前 page 的 origin/path（不记录 query/fragment）和已知正文编辑器是否存在；
   工具不点击「新的创作」或任何业务按钮，不输入、不选文件、不打开 file chooser、不上传、不保存、不发布、不登录。
4. 出现正文编辑器后只运行脱敏 DOM 探测：file input 的数量、type/accept/multiple/visible、
   正文编辑器归属、邻近短标签，以及 toolbar/image-trigger 候选的白名单字段和正文编辑器邻近性；
   不记录 value/path/href/src/style/class 全量、Cookie、Token、响应正文。
5. `LOGIN_REQUIRED`、`CHALLENGE`、`PROFILE_IN_USE`、`MANUAL_HANDOFF_TIMEOUT`、
   `UNEXPECTED_ORIGIN` 均 fail closed 且不自动重试；成功等待只报告 `EDITOR_READY`。
   固定落地页没有正文图片控件时报告 `LANDING_NO_INPUT`，不得自动创建草稿。
6. 正文图片入口以唯一 SVG path SHA-256 指纹定位；点击产生的 FileChooser 必须
   是单文件、图片 MIME 白名单且不属于封面区域。指纹不唯一、属性漂移、封面
   控件或图片数量未增加一律拒绝，禁止回退第一个 file input。
7. 真实草稿验收必须由用户明确选择账号、内容和 DRAFT 模式；失败不自动重试。
8. 原始编辑器图片数量增加只是临时证据；必须完成一次排版并在重开后证明所有
   `resizable-image` 均 `naturalWidth > 0`，否则不得报告图片成功。
9. H2 在原始 TipTap 中必须是 `h2`；平台排版后以 `.heading-level-2` 表示，允许
   因跨页拆成多个连续文本节点，但逻辑文本、样式类型和图文顺序必须全部一致。
10. 公开发布始终关闭；每次涉及小红的操作后，在本文件「操作记录」追加一行。

## 5. 操作记录

- 2026-08-17 / Codex：复用现有账号租约执行一次发布页只读 DOM 探测；落地页无 file input，未登录、未点击、未输入、未选文件、未保存；图片待真实验收，公开发布关闭。
- 2026-08-17 / Codex：仅实现同上下文人工交接只读探测模式与测试；本轮未启动浏览器、未执行真实探测；图片待真实验收，公开发布关闭。
- 2026-08-20 / Codex：复用账号 `jayoma` 的隔离 Profile，只读确认长文工具栏
  图片按钮及动态 FileChooser；随后按用户授权执行一次 DRAFT-only 单图验收。
  保存后曾在**同一 Profile** 重开标题卡片，确认 `文字 → 图片 → 文字`、图片数 1；未公开发布、
  未自动重试、未清理或记录 Cookie。
- 2026-08-20 / Codex：指定 Word 首次执行在第一个 H2 校验处停止，小红书平台
  自身留下唯一同名自动暂存草稿；基线门阻止重复创建。随后使用仅 DRAFT-only
  验收入口可开启的精确标题恢复门，继续同一草稿并保存。独立重开确认 29 个
  token、17 段普通文字、5 个 H2、7 张正文图片顺序一致；该证据现仅证明本地
  Profile 恢复能力，不再证明云端 DRAFT；公开发布和封面均未触发。
- 2026-08-20 / Codex：复查发现上述旧草稿的 7 个预览 URL 已返回 403，纠正“图片
  已持久化”的错误结论。使用用户指定 Word 创建的唯一同名原始草稿完成同草稿
  恢复：补齐平台自动保存遗漏的最后一段后重新打开确认，再执行一次「一键排版」。
  最终计划 `c02a8166-0193-4098-9f46-15feebc5a67b`、操作
  `03159bd5-9955-436e-a919-38b1ccf06480` 为 `SUCCESS/DRAFT_SAVED`；草稿数
  12→12、精确标题 1 条、7/7 远程图片加载、H2 样式和图文顺序均在重开后通过。
  后续只读打开“封面设置”确认无法精确选择正文首图，因此撤销封面成功声明并把
  适配器改为明确失败但允许保留带图草稿。另创建过一条单图排版探测草稿；未删除
  草稿、未公开发布。
- 2026-08-21 / Codex：跨浏览器只读复核同一账号 `jayoma`。原隔离 Profile 可见
  16 条长文卡片、目标标题 3 条；只注入同一登录 Cookie 的干净上下文可见 0 条。
  最新卡片“编辑”后最终页只出现“发布笔记/返回”，没有“保存草稿”动作；无公开
  发布点击、无 Cookie/Profile 清理。由此撤销云端草稿成功结论，关闭小红书自动
  投递，仅保留账号登录与管理。
