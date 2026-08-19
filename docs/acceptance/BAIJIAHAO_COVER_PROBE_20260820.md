# 百家号封面探测与 DRAFT 持久化验收（2026-08-20）

## 安全边界

- 账号：已登记百家号账号（平台 ID 仅以 `****5072` 核对）。
- Profile 通过账号级独占租约打开；未清理或复制 Cookie/Profile。
- 取证阶段只向已存草稿的封面弹窗选择一次受控图片，记录
  脱敏 DOM 状态后取消；不保存、不发布。
- 正式验收仅执行一次 `DRAFT`，公开发布开关保持关闭。

## 真实 DOM 证据

1. 编辑器存在唯一可见“选择封面”入口。
2. 打开后出现单个可见 `cheetah-modal` 封面弹窗。
3. 弹窗页签包含“正文/本地上传”“AI 封图”“免费正版图库”。
4. 空白编辑器只有一个 image input；正文已有图片时，封面弹窗同时
   存在 cropper 和 local-upload 两个 `accept=image/*` input。生产选择器
   只接受唯一 `FeEditorApp-*-upload` 祖先下的本地上传控件。
5. 同一页面另有 `accept=video/*` 控件，绝不能回退到页面第一个 file input。
6. React 接收图片后会立即清空 `input.files`；同时可见预览节点数从
   14 变为 15，视觉指纹改变，并出现唯一启用的“确定 (1)”。

## 真实 DRAFT 验收结果

- 冻结内容：指定 Word 文档的 v2 图文版本，正文 7 张图，封面策略
  `FIRST_BODY_IMAGE`。
- 执行单：`50fc0033-0e6f-4b1a-8029-ddd25431e12b`。
- 结果：`DRAFT_SAVED`，文章映射 `SUCCEEDED`。
- 重开核验：唯一标题命中，标题、全部文字和 7 张正文图的顺序一致；
  封面区域有且仅有一张已加载 `FeEditorApp-*-coverImg`，并显示
  “编辑/删除”动作。
- 最终状态：`PASSED`。

## 生产成功门

- `ContentVersion.cover_strategy/cover_asset_id` 是唯一封面真值。
- 平台适配器只接收执行期解析出的冻结受控资产路径，不从当前正文
  重新猜测封面。
- `NONE` 返回 `not_required`；素材缺失或控件不唯一时 fail closed。
- 上传成功要求：视觉预览指纹变化、唯一启用的“确定 (N)”或“确认”
  动作出现、封面弹窗完整关闭；不依赖会被 React 清空的 FileList。
- 草稿成功另外要求：保存后按唯一标题重新打开，标题、图文 token
  顺序与冻结版本一致，且唯一封面缩略图已加载。
- 任一验收条件不满足即返回 `RESULT_UNKNOWN`，禁止自动重试。
