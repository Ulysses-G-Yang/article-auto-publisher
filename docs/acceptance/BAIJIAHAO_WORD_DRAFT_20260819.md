# 百家号 Word 图文草稿真实验收（2026-08-19）

## 验收范围

- 源文件：`多设备共用显示器_川升CS40X_去AI优化发布版.docx`
- 模式：仅 `DRAFT`；公开发布总开关保持关闭。
- 账号：已登记的百家号隔离账号，公开记录仅保留脱敏 ID `****5072`。
- 内容版本：Content Studio 草稿修订 23；标题为
  `BAIJIA-DOCX-08192220-PERSIST`。
- 本轮没有删除草稿、清理 Cookie、修改 Profile 或公开发布。

## 真实平台结果

1. 百家号 `POST /pcui/article/save` 返回 HTTP 200，业务响应为
   `errno=0`、`errmsg=success`。
2. 草稿标签页能检索到 3 条同名草稿。它们来自旧验证器误判后的重复验收；
   本轮没有继续保存，也没有在未获删除授权时清理这些草稿。
3. 通过草稿行同源预览 ID 构造百家号官方编辑地址，重开第一条草稿后：
   - 标题完全一致；
   - 29 个正文节点与冻结版本逐项一致；
   - 17 个普通正文节点；
   - 5 个二级标题节点；
   - 7 张正文图片；
   - 图文顺序完全一致。

## 根因与修复

旧验证器把草稿行的“修改”当成当前页导航。真实页面通过 React 处理器调用
`window.open('/builder/rc/edit?...')`；弹窗被自动化上下文拦截时，平台已经保存
成功，验证器仍会超时并把操作标成 `RESULT_UNKNOWN`。修复后不再依赖视觉按钮
跳转，而是从唯一精确标题草稿行读取同源 `/builder/preview/s?id=...`，严格校验
origin/path/唯一 ID，再构造同源编辑地址并重开回读。

## 能力结论

- `editor_entry`、`text_draft`、`body_images`、`draft_verification`：
  `REAL_VERIFIED`。
- `heading`：仅 H2 已验证。
- `image_order`：已验证。
- `cover`：仍未完成独立真实验收，保持失败/阻断状态。
- `public_publish`：继续 `DISABLED`。

本证据只证明百家号稳定带图草稿链路，不宣称封面或完整公开发布闭环已经完成。
