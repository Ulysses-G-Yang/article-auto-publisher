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
6. **验收失败如实报告**：任何一步未验证通过，不得宣称平台可用；
   投递开关（delivery_enabled）必须保持关闭直到真实草稿验收通过。
7. **探测先行、改动最小**：任何新的真实操作前，先用只读探测确认页面结构；
   代码改动只限于必要的最小适配层。

## 3. 当前状态（2026-08-14）

- 账号链路：登录（扫码）/ 会话检测（`web_session` cookie）/ 身份捕获已接入，
  目录 `account_enabled=True`（`src/account_sessions/platform_catalog.py`）。
- 投递链路：**尚未接入**（`platforms/xiaohongshu.py` 投递方法显式
  `PLATFORM_NOT_IMPLEMENTED`），`delivery_enabled=False`。
- 适配器文件：`platforms/xiaohongshu.py`；身份提取：`src/account_sessions/identity.py`
  `_extract_xiaohongshu`；平台工厂：`src/account_sessions/account_service.py`。

## 4. 后续接入小红书投递时的要求

1. 先只读探测创作服务平台编辑器结构（禁止在真实账号上盲试）。
2. 用隔离 Profile + 真实账号完成扫码登录与身份验证（用户确认后）。
3. 实现投递方法并补齐契约测试，全量测试通过、focused commit、SSH push。
4. 人工真实草稿验收（用户明确选择账号与内容，只 DRAFT）。
5. 验收通过后才允许把 `delivery_enabled` 置 True；公开发布仍保持关闭。
6. 每次涉及小红的操作后，在本文件「操作记录」追加一行（谁/何时/做了什么/结果）。
