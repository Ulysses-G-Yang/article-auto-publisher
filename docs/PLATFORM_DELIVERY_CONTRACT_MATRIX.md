# ArticleOps 六平台投递证据矩阵

更新时间：2026-08-19 真实验收基线（代码清单由当前分支维护）

这份矩阵是只读的验收证据，不是投递开关，也不会被生产执行路径自动读取。代码存在、单元测试通过、页面能打开，都不等于真实平台链路已经可用；只有交接文档明确记录的真实结果才能标记为 `REAL_VERIFIED` 或 `REAL_FAILED`。

## 状态与更新纪律

| 状态 | 含义 |
|---|---|
| `UNVERIFIED` | 尚无足够代码或真实证据，禁止推断可用 |
| `CODE_TESTED` | 只有本地契约/单元测试通过，不代表真实平台成功 |
| `REAL_VERIFIED` | 有日期和仓库内证据引用，真实页面结果满足验收门 |
| `REAL_FAILED` | 有日期和仓库内证据引用，真实验收明确失败 |
| `RETEST_REQUIRED` | 修复已进入代码，但修复后尚未重新进行真实验收 |
| `DISABLED` | 功能被安全开关关闭，当前不得执行 |
| `NOT_APPLICABLE` | 当前平台/流程不适用 |

每条记录都必须同时维护：`page_state`、可空的 `last_real_check`、仓库相对路径 `evidence_refs` 和明确的 `success_criteria`。新增真实验收后，先补证据文件和日期，再更新 [platform_delivery_readiness.py](../src/content_studio/platform_delivery_readiness.py)，最后运行 `tests/test_platform_delivery_readiness.py`；不得用“代码已实现”替代真实证据。

## 验收门定义

| 能力项 | success_criteria |
|---|---|
| `account_session` | 隔离账号真实登录态为 `ACTIVE/VALID`，且没有残留 Profile 锁 |
| `editor_entry` | 指定账号进入目标编辑器，页面状态和编辑器入口符合平台契约 |
| `text_draft` | 标题、正文和段落顺序完整写入，并在平台侧保存为 `DRAFT` |
| `body_images` | 正文图片数量稳定，顺序与冻结内容版本一致 |
| `cover` | 封面策略对应图片在平台预览中真实出现，未误传到其他控件 |
| `draft_verification` | 通过平台草稿箱标题/计数和媒体状态核对，不能只信接口返回码 |
| `public_publish` | 用户逐目标确认、一次性令牌和公开发布总开关同时满足；当前保持关闭 |

## 当前矩阵

证据引用均为仓库相对路径，具体字段的机器可读版本以 Python 模块为准。

### 小黑盒 `xiaoheihe`

| 能力项 | 状态 | page_state | last_real_check | evidence_refs |
|---|---|---|---|---|
| account_session | `REAL_VERIFIED` | `account_active_valid_during_word_draft_acceptance` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| editor_entry | `REAL_VERIFIED` | `real_editor_entry_passed` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| text_draft | `REAL_VERIFIED` | `real_reopen_verified_22_text_and_heading_blocks` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| body_images | `REAL_VERIFIED` | `real_reopen_verified_7_ordered_body_images` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| cover | `RETEST_REQUIRED` | `real_run_stopped_before_cover_verification_waiting_rerun` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| draft_verification | `REAL_VERIFIED` | `unique_draft_card_and_persisted_dom_verified` | 2026-08-19 | `docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| public_publish | `DISABLED` | `global_publish_gate_closed` | — | `AGENTS.md`, `codex_handoff_20260817.md` |

### 中关村在线 `zol`

| 能力项 | 状态 | page_state | last_real_check | evidence_refs |
|---|---|---|---|---|
| account_session | `REAL_VERIFIED` | `account_active_valid_during_word_draft_acceptance` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| editor_entry | `REAL_VERIFIED` | `real_creator_editor_entry_passed` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| text_draft | `REAL_VERIFIED` | `real_reopen_verified_22_text_and_heading_blocks` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| body_images | `REAL_VERIFIED` | `real_reopen_verified_7_ordered_body_images` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| cover | `RETEST_REQUIRED` | `cover_control_not_independently_verified` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| draft_verification | `REAL_VERIFIED` | `unique_title_and_29_ordered_tokens_reopened` | 2026-08-19 | `docs/acceptance/ZOL_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| public_publish | `DISABLED` | `global_publish_gate_closed` | — | `AGENTS.md`, `codex_handoff_20260817.md` |

### 知乎 `zhihu`

| 能力项 | 状态 | page_state | last_real_check | evidence_refs |
|---|---|---|---|---|
| account_session | `REAL_VERIFIED` | `account_active_valid_during_word_draft_acceptance` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| editor_entry | `REAL_VERIFIED` | `real_draftjs_editor_entry_passed` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| text_draft | `REAL_VERIFIED` | `real_reopen_verified_22_text_and_heading_blocks` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| body_images | `REAL_VERIFIED` | `real_reopen_verified_7_ordered_body_images` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| cover | `RETEST_REQUIRED` | `cover_control_not_independently_verified` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| draft_verification | `REAL_VERIFIED` | `unique_api_draft_id_and_29_ordered_tokens_reopened` | 2026-08-19 | `docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| public_publish | `DISABLED` | `global_publish_gate_closed` | — | `AGENTS.md`, `codex_handoff_20260817.md` |

### 什么值得买 `smzdm`

| 能力项 | 状态 | page_state | last_real_check | evidence_refs |
|---|---|---|---|---|
| account_session | `REAL_VERIFIED` | `account_active_valid_during_word_draft_acceptance` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| editor_entry | `REAL_VERIFIED` | `real_prosemirror_editor_entry_passed` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| text_draft | `REAL_VERIFIED` | `real_reopen_verified_22_text_and_heading_blocks` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| body_images | `REAL_VERIFIED` | `real_reopen_verified_7_ordered_body_images` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| cover | `RETEST_REQUIRED` | `cover_control_not_independently_verified` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| draft_verification | `REAL_VERIFIED` | `unique_title_and_29_ordered_tokens_reopened` | 2026-08-19 | `docs/acceptance/SMZDM_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| public_publish | `DISABLED` | `global_publish_gate_closed` | — | `AGENTS.md`, `codex_handoff_20260817.md` |

### 百家号 `baijiahao`

| 能力项 | 状态 | page_state | last_real_check | evidence_refs |
|---|---|---|---|---|
| account_session | `REAL_VERIFIED` | `account_active_valid_during_word_draft_acceptance` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| editor_entry | `REAL_VERIFIED` | `real_ueditor_entry_passed` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| text_draft | `REAL_VERIFIED` | `real_reopen_verified_22_text_and_heading_blocks` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| body_images | `REAL_VERIFIED` | `real_reopen_verified_7_ordered_body_images` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| cover | `REAL_FAILED` | `cover_control_not_independently_verified` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| draft_verification | `REAL_VERIFIED` | `persisted_draft_and_29_ordered_tokens_reopened` | 2026-08-19 | `docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md`, `codex_handoff_20260817.md` |
| public_publish | `DISABLED` | `global_publish_gate_closed` | — | `AGENTS.md`, `codex_handoff_20260817.md` |

### 小红书 `xiaohongshu`（退出投递目录）

账号登录与管理仍可用；网页长文“草稿箱”经跨浏览器复核为 Profile 本地状态，
没有可验证的云端 DRAFT 实体。因此 `delivery_enabled=false`，不参与下面的投递
就绪度计算；公开发布仍关闭。

## 当前计算结果

`can_run_stable_image_draft(platform)` 和 `can_run_complete_word_draft(platform)` 只计算证据，不触发生产执行：

| 平台 | 结果 | 原因 |
|---|---:|---|
| 知乎 | `true` | 账号、编辑器、文字草稿、正文图片、草稿核对均为 `REAL_VERIFIED` |
| ZOL | `true` | 唯一草稿重开后 22 个文字/章节块、7 张正文图片和 29 个有序节点完全一致 |
| 小黑盒 | `true` | 唯一草稿重开后 22 个文字/标题块、7 张正文图片和 29 个有序节点完全一致 |
| 什么值得买 | `true` | 唯一草稿重开后 22 个文字/标题块、7 张正文图片和 29 个有序节点完全一致 |
| 百家号 | `true` | 已重开核对 22 个文字/标题节点、7 张正文图片和 29 个有序节点；封面仍不计入稳定带图草稿条件 |

当前五个投递平台的 `can_run_stable_image_draft()` 均为 `true`；小红书返回
`false` 且不进入新投递计划。`can_run_complete_word_draft()` 仍由各平台封面
证据单独决定，正文 Word 图文与封面不能相互冒充。

这些函数没有接入 `DeliveryService`、CLI、Web 或 MCP。下一次真实验收完成后，先补证据文件和日期，再更新矩阵并晋级状态；不得因为代码或单元测试变化自动晋级为 `REAL_VERIFIED`。
