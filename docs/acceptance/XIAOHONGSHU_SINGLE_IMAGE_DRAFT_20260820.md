# 小红书单图正文草稿真实验收（2026-08-20）

## 结论

- 平台账号：`jayoma`（公开脱敏 ID：`****3540`）
- 模式：`DRAFT`；公开发布开关保持关闭
- 标题：`XHS单图验收-20260820-001`
- 结果：`DRAFT_SAVED`
- 计划：`0d39e268-cefa-4a6a-b7d1-62bbd3baf12c`
- 操作：`ff7194b3-cb0d-4de5-a5f7-c28ad908e153`

## 页面证据

1. 长文编辑器为 `div.tiptap.ProseMirror`，页面没有常驻正文 file input。
2. `.edit-page.new-ui button.menu-item` 中正文图片按钮 SVG path 指纹唯一。
3. 点击该按钮产生一次临时 FileChooser：单文件，`accept` 为
   `image/jpeg,image/jpg,image/png,image/webp`，不属于封面区域。
4. 只执行一次 `set_files`，TipTap 正文图片数由 0 稳定增加为 1。
5. 保存后从草稿抽屉切换“长文笔记”，唯一标题命中 1 条并重开“编辑”。
6. 重开后标题精确一致，DOM 顺序为 `TEXT_BEFORE, IMAGE, TEXT_AFTER`，
   正文图片数精确为 1。

## 安全边界

- 未公开发布，未点击公开发布入口。
- 失败路径禁止自动重试；本次只创建一篇唯一标题验收草稿。
- 未输出 Cookie、Token、Profile 内容或图片物理路径。
- 本证据只晋级“小红书正文单图顺序”；封面和 Word 标题映射仍未验收。
