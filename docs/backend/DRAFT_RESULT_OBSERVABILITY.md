# 草稿保存结果可观测性设计（v1 草案）

状态：**草案，待评审**。评审通过前不写任何代码。

日期：2026-08-25
范围：`feature/shared-sdk-heartbeat-delay` 分支，方案一（证据链结构化）+ 方案二（只读核验 API + 前端）

---

## 1. 问题陈述

### 1.1 现状

保存草稿的判定是"层层收紧"：各平台 `save_draft()` 必须**证明草稿实体真的持久化**
（捕获保存接口 2xx + 草稿箱标题唯一匹配 + 重开后标题/图文一致）才返回 URL；
`base.py publish()` 见空串即返回 `success=false`；`DeliveryService` 将其落为
`FAILED` 或 `RESULT_UNKNOWN`。

这个设计防"假成功"是正确的，但存在**信息不对称**：

1. 适配器内部每一步验证的结果（"保存接口响应拿到了吗？""草稿箱里标题唯一吗？"）
   只存在于局部变量，异常时随异常丢失，业务人员看不到。
2. 执行单 API（`operation_payload`）只返回 `error_code` + `error_message`，
   无法回答"草稿到底在不在"。
3. 现有只读探测工具（`scripts/probe_weibo_editor_readonly.py` 等）能力齐全但都是
   CLI，业务人员无法在界面触发。

**结果**：明明草稿已保存，界面只显示"失败/结果未知"，业务人员必须翻终端日志，
或手动打开平台草稿箱核对。

### 1.2 目标

- 业务人员**不依赖终端日志**即可判断"草稿是否真的存在"。
- 不改变保存判定逻辑本身（防假成功语义不变），不改变公开发布开关，不引入自动重试。
- 复用现有账号 Profile、租约、只读探测能力，新增动作全部只读、无副作用。

---

## 2. 方案一：证据链结构化

### 2.1 目标

把平台适配器 `save_draft()` 内部各步骤的验证结果结构化带出，随执行单响应返回给前端，
让业务人员一眼看懂"失败在哪一环、草稿是否已存在于平台"。

### 2.2 契约设计（新增，向后兼容）

`DeliveryOperation` 新增可空列 `verification_evidence`（JSON 文本）：

```json
{
  "save_response_2xx": false,
  "save_http_status": null,
  "save_platform_code": null,
  "draft_list_title_unique": true,
  "draft_list_match_count": 1,
  "reopen_title_match": null,
  "reopen_dom_blocks_match": null,
  "draft_url": null,
  "unknown": true,
  "summary": "保存接口响应未捕获，但草稿箱中存在标题唯一匹配的草稿"
}
```

字段语义：

| 字段 | 类型 | 含义 |
|---|---|---|
| `save_response_2xx` | bool/null | 是否捕获到保存接口 2xx 响应（微博/SMZDM 等显式接口模式） |
| `save_http_status` | int/null | 捕获到的保存接口 HTTP 状态码 |
| `save_platform_code` | str/null | 平台业务码（如微博响应 `code`） |
| `draft_list_title_unique` | bool/null | 草稿箱列表中标题精确匹配数是否为 1 |
| `draft_list_match_count` | int/null | 标题精确匹配数 |
| `reopen_title_match` | bool/null | 重开草稿后标题是否一致 |
| `reopen_dom_blocks_match` | bool/null | 重开后 DOM 图文结构是否与冻结版本一致 |
| `draft_url` | str/null | 可核验草稿 URL（即使整体失败，若已重开拿到也保留） |
| `unknown` | bool | 是否为 `RESULT_UNKNOWN` 语义（禁止自动重试） |
| `summary` | str | 给业务人员的一句话脱敏总结（模板拼接，不含路径/凭据/正文） |

规则：

- 全部字段可空；`null` 表示"未执行到该步骤"。
- 成功时也有证据（`save_response_2xx: true` 等），但前端只在失败/未知时展示。
- `summary` 由服务端模板生成，不携带原始异常文本、Profile 路径、Cookie、Token 或正文。
- 数据库迁移：`ALTER TABLE delivery_operations ADD COLUMN verification_evidence TEXT`，
  幂等迁移（与现有 `heartbeat_enabled` 迁移同模式，见 `database.py`）。

### 2.3 平台适配器改造点（每个平台）

各平台 `save_draft()` 当前内部已有局部证据，需要改为**写入一个共享证据对象**，
并在成功/异常路径都能带出。引入基类工具：

`platforms/base.py` 新增 `DraftVerificationEvidence` 数据类（模块级，纯数据结构）：
- 各步骤 setter（幂等，只在尚未记录时写，避免成功后再被覆盖）
- `to_dict()` → 上述 JSON 结构
- `summarize(error_code)` → 生成 `summary` 模板文本

各平台改动：

| 平台 | 现有局部证据 | 改动 |
|---|---|---|
| 微博 `weibo.py` | `captured{status,code,published,blocked_publish}`、草稿箱匹配数、重开标题/DOM | 把 `captured` 与匹配结果写入证据对象；异常前 `evidence.mark_*` |
| 知乎 `zhihu.py` | 列表 API 匹配 `matches`、`_verify_persisted_draft` 结果 | 写入唯一匹配数与重开结果 |
| ZOL `zol.py` | `autosave_id`、`_bound_draft_id`、草稿卡片 `existing` | 写入自动保存响应 ID 与绑定 ID 是否一致、卡片匹配数 |
| SMZDM `smzdm.py` | `captured{status,error_code}`、`_find_unique_new_draft` 结果 | 写入捕获状态与实体差集结果 |
| 小黑盒 `xiaoheihe.py` | 保存响应/草稿箱核对（与微博同模式） | 同上 |

原则：**只新增证据记录调用，不改变任何成功/失败分支逻辑**。

### 2.4 异常携带链

`DraftResultUnknownError`/`DraftBaselineError` 等平台异常新增可空属性
`evidence: DraftVerificationEvidence | None`：

- `raise DraftResultUnknownError(msg, evidence=evidence)`（构造签名向后兼容，缺省 None）
- `base.py publish()` 的 except 分支把 `exc.evidence` 放进返回值：
  `result["verification_evidence"] = exc.evidence.to_dict() if exc.evidence else None`
- `DeliveryService.execute_operation()` 在 `_mark_failed`/`_mark_completed*` 时把
  `result.get("verification_evidence")` 持久化到 `DeliveryOperation.verification_evidence`。

注意：异常对象在 `except` 处构造时证据尚未收集完，因此平台适配器应在 `raise` 前
`evidence.mark_*` 全部已执行步骤；`to_dict()` 在异常处理时调用（此刻证据已完整）。

### 2.5 API 与前端展示

- `operation_payload()` 增加 `verification_evidence` 字段（可空，向后兼容）。
- `web/static/js/content-studio.js` `planTargetDetail()` 扩展：
  - `RESULT_UNKNOWN` 且 `verification_evidence.draft_list_title_unique === true` 时显示：
    `"结果未知，但平台草稿箱已存在标题唯一匹配的草稿（保存接口响应未捕获）。建议：点此打开平台草稿箱人工核对。"` 附直达链接（复用 `platformDraftBoxUrl`）。
  - `FAILED` 且有证据时显示 `summary` + 证据明细（只读文本，不显示原始异常堆栈）。
- 首页 `web/templates/index.html` 的最近投递列表，`op.verification_evidence` 存在时
  显示 `summary` 摘要。

### 2.6 测试

- 数据库迁移幂等测试（重复启动不报错）。
- 每个平台适配器：构造证据对象 → 走失败分支 → 断言 `to_dict()` 字段正确、
  `summary` 脱敏（不含路径/凭据）。
- `DeliveryService`：异常携带证据 → `_mark_failed` 持久化 → `operation_payload`
  返回字段。
- 前端契约测试（`tests/frontend/`）：`RESULT_UNKNOWN + 证据存在` 的文案渲染。

---

## 3. 方案二：只读核验 API + 前端按钮

### 3.1 目标

执行单失败/未知后，业务人员在界面一键触发**只读核验**：系统用该账号隔离 Profile
（带租约）只读打开平台草稿箱，按标题查唯一草稿，返回"草稿在/不在 + 结构摘要"，
业务人员自行判断，无需打开终端。

### 3.2 新 API

```
POST /api/delivery-operations/{operation_id}/verify-draft
```

请求体：`{}`（标题从执行单读取，不允许调用方传标题，防止探测无关草稿）

响应（同步返回，最长等待约 90 秒；超时返回 202 轮询？——见 3.5 决策点）：

```json
{
  "operation_id": "...",
  "platform": "weibo",
  "title_matched": true,
  "match_count": 1,
  "draft_url": "https://card.weibo.com/article/v5/editor#/draft/4183864",
  "structure": {
    "char_count": 210,
    "body_image_count": 0,
    "heading_count": 0,
    "verified_against_frozen": false
  },
  "readonly": true,
  "probe_id": "uuid"
}
```

字段语义：

| 字段 | 含义 |
|---|---|
| `title_matched` | 草稿箱是否存在标题精确匹配实体 |
| `match_count` | 匹配数量（>1 视为歧义，返回 `AMBIGUOUS`） |
| `draft_url` | 命中草稿的编辑/草稿链接（脱敏，无 Cookie/Token） |
| `structure` | 脱敏结构摘要：字数、正文图片数、H2 数等（不含正文文本） |
| `readonly` | 恒为 true；探测全程拦截非只读请求 |
| `probe_id` | 探测记录 ID（审计用） |

错误码（沿用现有稳定码风格）：

| error_code | 含义 |
|---|---|
| `PROBE_TITLE_MISSING` | 执行单无标题，拒绝探测 |
| `PROBE_ACCOUNT_INACTIVE` | 账号非 ACTIVE 或会话非 VALID，先重新登录 |
| `PROFILE_IN_USE` | Profile 被其他流程占用，稍后重试 |
| `PROBE_TITLE_AMBIGUOUS` | 标题匹配 >1，需要人工区分 |
| `PROBE_NOT_FOUND` | 草稿箱无匹配草稿 |
| `PROBE_RESULT_UNKNOWN` | 页面/浏览器异常，无法证明（禁止自动重试） |
| `PROBE_UNSUPPORTED_PLATFORM` | 该平台尚未实现只读核验 |

### 3.3 实现

新增 `src/account_sessions/draft_verify.py`（或并入 `delivery_service.py`，见决策点）：

- 输入：`operation_id`、`AccessContext`（`LOCAL_WEB_CONTEXT` 或 MCP 独立上下文）。
- 流程：
  1. 读取执行单 → 校验 `platform`、`title`、账号状态。
  2. `self.accounts._lease(account, purpose="VERIFY")` 取得 Profile 租约
     （复用现有 `AccountProfileLease`，与心跳/投递共用，不会并发抢 Profile）。
  3. `platform_factory(account)` 构建平台实例，`initialize()`（strict lock）。
  4. 调用平台新增的**只读核验钩子** `verify_draft_readonly(title)`：
     - 默认实现返回 `{"unsupported": True}`（fail-closed，不猜测选择器）。
     - 微博/知乎/ZOL/SMZDM/百家号分别实现：只读打开草稿箱 → 按标题精确唯一匹配 →
       返回结构摘要。**复用现有 `save_draft()` 中已实现的草稿箱查询与 DOM 结构读取
       逻辑，抽取为只读方法**（如 `_find_unique_exact_draft` 已是只读，直接复用）。
  5. 全程注册 `route` 拦截非只读请求（GET 放行，其余 abort），与微博
     `_guard_public_publish` 同模式。
  6. 写 `probe_id` 审计记录（`account_activity`，action=`DRAFT_PROBE`），不落平台侧数据。
  7. 关闭 context/浏览器（复用 `cleanup()`），释放租约。
- 权限：复用 `session.read` + `draft.create` capability；MCP 侧通过独立
  `AccessContext` 暴露同能力（可选，本期不做 MCP 暴露，见边界）。

### 3.4 前端按钮

`web/static/js/content-studio.js` `planTargetDetail()`：

- `RESULT_UNKNOWN` 或 `FAILED` 且平台支持核验时，目标行渲染按钮：
  `"核验平台草稿"`（仅当执行单 `status` 是终态且 `mode === DRAFT`）。
- 点击 → `POST /api/delivery-operations/{id}/verify-draft` → 展示结果卡片：
  - 草稿在：绿色提示 `"平台草稿箱存在标题唯一匹配的草稿（字数 X，图片 Y）"` + 直达链接。
  - 不在：红色提示 `"平台草稿箱未找到该标题草稿"`。
  - 歧义/未知：黄色提示 + 打开草稿箱人工核对。
- 页面按钮不可重复提交（防抖）；探测期间显示"正在只读核验…"。

### 3.5 待定决策点（评审时确认）

| # | 决策点 | 选项 | 建议 |
|---|---|---|---|
| D1 | 探测同步等待还是异步轮询 | A. 同步（最长 90s 前端等待）；B. 异步 `202 + probe_id` 轮询 | **A 同步**：探测通常 20-60s，同步最简单；超时返回 `PROBE_RESULT_UNKNOWN` |
| D2 | 新模块位置 | A. 并入 `delivery_service.py`；B. 新增 `draft_verify.py` | **B 独立文件**：职责单一，避免 delivery_service 膨胀 |
| D3 | 只读钩子放哪 | A. `BasePlatform` 新增抽象方法；B. 平台可选实现 + `hasattr` 检测 | **A 基类默认 `unsupported`**，显式声明平台能力 |
| D4 | MCP 是否暴露 | A. 本期不做；B. 顺带暴露 | **A 不做**：先服务 Web 端，MCP 契约冻结不变 |
| D5 | 证据列存储 | A. JSON TEXT 列；B. 单独子表 | **A JSON TEXT**：只读展示用，无需查询索引 |

---

## 4. 安全边界（两条方案共同）

- **不改变判定逻辑**：`save_draft()` 成功/失败分支、`RESULT_UNKNOWN` 语义、禁止自动
  重试全部保持不变。方案一只加"记录证据"，方案二只加"只读核验"。
- **只读探测**：注册非 GET 请求拦截；不点保存/发布/删除；不清 Cookie；不自动登录。
- **租约复用**：核验与投递/心跳共用 `AccountProfileLease`，`PROFILE_IN_USE` 时返回
  错误码而非强抢。
- **脱敏**：证据 `summary`、核验响应、审计日志不含正文、Profile 路径、Cookie、
  Token、原始平台 ID。
- **公开发布**：开关保持关闭；`verify-draft` 不触碰 `publish_after_draft`。
- **fail-closed**：平台未实现只读钩子 → 返回 `PROBE_UNSUPPORTED_PLATFORM`，绝不猜测
  选择器。

## 5. 改动文件清单（预估）

| 文件 | 改动 |
|---|---|
| `src/account_sessions/models.py` | `DeliveryOperation.verification_evidence` 列 |
| `src/account_sessions/database.py` | 幂等迁移 |
| `src/account_sessions/contracts.py` | （可选）`VerifyDraftResponse` 模型 |
| `src/account_sessions/delivery_service.py` | 持久化证据；`operation_payload` 返回证据；新增 verify-draft 入口（或调 `draft_verify.py`） |
| `src/account_sessions/web.py` | 新路由 `POST /api/delivery-operations/{id}/verify-draft` |
| `src/account_sessions/draft_verify.py` | 新文件：只读核验用例（方案二） |
| `platforms/base.py` | `DraftVerificationEvidence`；`publish()` 带出证据；`verify_draft_readonly` 默认 unsupported |
| `platforms/weibo.py` 等 5-6 个平台 | `save_draft()` 写证据；抽取只读核验钩子 |
| `web/static/js/content-studio.js` | 证据展示 + 核验按钮 |
| `web/templates/index.html` | 最近投递展示证据摘要 |
| `tests/` | 迁移、适配器证据、DeliveryService 持久化、verify-draft、前端契约测试 |

## 6. 验收标准

1. 构造 `DRAFT_RESULT_UNKNOWN` 场景（mock 保存接口无响应但草稿箱有唯一草稿）：
   执行单 API 返回 `verification_evidence.draft_list_title_unique=true`，前端显示
   "草稿箱已有唯一匹配草稿" 文案。
2. `POST verify-draft`：微博/知乎/ZOL/SMZDM 真实 Profile 只读探测返回
   `title_matched/match_count/draft_url/structure`；全程无保存/发布请求（route 拦截
   计数为 0）。
3. `PROFILE_IN_USE` 时返回错误码，不阻塞其他流程。
4. 全量 pytest、Ruff、`git diff --check` 通过；无 data/、凭据、uv.lock 改动。
5. 公开发布开关保持关闭；未执行任何真实登录/草稿保存/发布/删除（除验收授权的一次
   只读探测）。

## 7. 评审问题

1. 决策点 D1-D5 是否按建议选择？
2. 证据 `summary` 文案是否需要中英双语或更简洁？
3. verify-draft 是否要支持"按执行单标题 + 平台"之外的手动输入标题（如业务人员
   事后手动补录）？—— 建议本期不做，保持最小面。
4. 是否需要把该能力暴露给 MCP（CS_Admin 侧）？
