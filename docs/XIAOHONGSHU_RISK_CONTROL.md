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

## 3. 当前状态（2026-08-20）

- 账号链路：登录（扫码）/ 会话检测 / 身份捕获已接入，目录
  `account_enabled=True`（`src/account_sessions/platform_catalog.py`）。
- 文字草稿链路：**已验收**；草稿保存和草稿箱计数验证可以如实报告文字草稿结果。
- 图片链路：**完整 Word 正文草稿已真实验收**。真实编辑器没有常驻 file input；点击
  已冻结 SVG 指纹的正文图片工具栏按钮后才产生临时 FileChooser。适配器验证
  `accept/multiple/cover` 属性后只调用一次 `set_files`，并以 TipTap 正文图片数
  稳定增加为成功。2026-08-20 先通过单图顺序门，随后用指定 Word 完成
  29 个有序 token（17 段普通文字、5 个 H2、7 张正文图片）的同名草稿恢复、
  保存和重开核验。封面仍未验收。
- 公开发布：**关闭**。不得通过配置、适配器或测试打开公开发布。
- 适配器文件：`platforms/xiaohongshu.py`；身份提取：`src/account_sessions/identity.py`
  `_extract_xiaohongshu`；平台工厂：`src/account_sessions/account_service.py`。

## 4. 后续小红书图片真实验收要求

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
8. 图片成功判据是正文编辑器图片数量稳定增加；文字草稿已验收不等于图片已验收。
9. 公开发布始终关闭；每次涉及小红的操作后，在本文件「操作记录」追加一行。

## 5. 操作记录

- 2026-08-17 / Codex：复用现有账号租约执行一次发布页只读 DOM 探测；落地页无 file input，未登录、未点击、未输入、未选文件、未保存；图片待真实验收，公开发布关闭。
- 2026-08-17 / Codex：仅实现同上下文人工交接只读探测模式与测试；本轮未启动浏览器、未执行真实探测；图片待真实验收，公开发布关闭。
- 2026-08-20 / Codex：复用账号 `jayoma` 的隔离 Profile，只读确认长文工具栏
  图片按钮及动态 FileChooser；随后按用户授权执行一次 DRAFT-only 单图验收。
  保存后重开唯一标题草稿，确认 `文字 → 图片 → 文字`、图片数 1；未公开发布、
  未自动重试、未清理或记录 Cookie。
- 2026-08-20 / Codex：指定 Word 首次执行在第一个 H2 校验处停止，小红书平台
  自身留下唯一同名自动暂存草稿；基线门阻止重复创建。随后使用仅 DRAFT-only
  验收入口可开启的精确标题恢复门，继续同一草稿并保存。独立重开确认 29 个
  token、17 段普通文字、5 个 H2、7 张正文图片顺序一致；公开发布和封面均未触发。
