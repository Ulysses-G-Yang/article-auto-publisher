# ZOL Word 带图草稿真实验收（2026-08-19）

## 验收边界

- 平台：中关村在线（ZOL）创作者中心。
- 账号：已登记的独立账号 `1wphk1`，验收时为 `ACTIVE/VALID`。
- 输入文件：`多设备共用显示器_川升CS40X_去AI优化发布版.docx`。
- 模式：仅 `DRAFT`；公开发布相关环境开关全部关闭。
- 未执行删除、公开发布或失败自动重试；历史探测草稿均保留。
- 封面控件没有在本次验收中单独核验，因此 `cover` 仍为
  `RETEST_REQUIRED`。

## 事故复现与修复结论

第一轮标题 `ZOL-DOCX-0819193633` 虽然接口返回 `DRAFT_SAVED`，但独立只读
重开后只剩 16/22 个文字或标题块；最后一张图片后的 6 个尾部块没有持久化，
直接插入的 `<h2>` 也被平台清洗成普通段落。该轮不能作为成功证据。

根因有两项：

1. `save_draft()` 错把图片阶段较早的自动保存响应当作完整正文版本的保存证据，
   没有对已绑定草稿执行最终保存。
2. ZOL 的真实 TinyMCE 工具栏使用自有 `header` 控件生成
   `blockquote.wxeditor-title` 章节标题；平台保存时不保留直接注入的语义
   `<h2>/<h3>`。

修复提交：`7f41f18cb70a0fc6675e4c555363584bf64e0ef5`。

- 多图流程先绑定唯一草稿 ID，图片自动保存只能更新该 ID。
- 所有内容写完后，在当前 URL 的 `draftId` 与绑定 ID 一致时只点击一次最终保存，
  且最终响应 ID 必须仍为同一草稿。
- Word H2 使用平台自有“章节标题”结构；H3 继续 fail closed。
- 保存成功后重开精确草稿 ID，重新读取正文 DOM；文字、章节、图片数量或顺序
  任一不一致均返回 `DRAFT_RESULT_UNKNOWN`，禁止假报成功和自动重试。

## 最终真实验收证据

- 标题：`ZOL-DOCX-08191955-PERSIST`。
- Delivery Plan：`2b1bb254-a5e2-4a8c-aaa4-9ed1efc8c144`。
- Delivery Operation：`155eeb3b-ebe3-479a-ad13-f0430b4de7a1`。
- 计划终态：`SUCCESS`。
- 目标与操作终态：`DRAFT_SAVED`，`error_code=null`。
- 文章映射状态：`SUCCEEDED`。
- 草稿标题匹配数：`1`。
- 草稿 ID 仅记录 SHA-256 前缀：`84f21c087c06`；不记录平台原始 ID。
- 预期与实际文字/章节块：`22 / 22`。
- 预期与实际正文图片：`7 / 7`。
- 预期与实际有序 DOM token：`29 / 29`。
- 独立只读重开核验：`ordered_structure_match=true`。
- 五个 Word H2 均以 ZOL 自有章节标题结构持久化并按原顺序回读。
- 公开发布：未执行。

最终核验不是只相信保存接口或当前编辑器内存状态，而是由另一个只读浏览器
会话重新打开平台草稿箱中的唯一标题实体，再打开精确草稿编辑地址并比较完整
DOM 序列。

## 自动化验证

- ZOL 定向契约：`114 passed`。
- 全量：`630 passed, 2 skipped`。
- Ruff 关键错误与导入检查：通过。
- `git diff --check`：通过。

## 当前结论

ZOL 的账号会话、编辑器进入、Word 正文、H2 章节、7 张正文图片、唯一草稿实体
和草稿重开核验已达到 `REAL_VERIFIED`。`can_run_stable_image_draft("zol")`
可以为 `true`。平台封面尚未独立验证，所以
`can_run_complete_word_draft("zol")` 仍必须为 `false`。
