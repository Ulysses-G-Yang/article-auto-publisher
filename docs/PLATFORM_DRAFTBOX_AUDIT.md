# 平台草稿箱展示盘点（2026-08-17）

> 分支：`feat/zhihu-real-delivery` · 状态：只读盘点完成，平台侧问题如实记录

## 一、各平台草稿箱状态

| 平台 | 草稿箱地址（直达链接已接入系统） | 展示状态 | 说明 |
|---|---|---|---|
| 小黑盒 | https://www.xiaoheihe.cn/creator/draft | ✅ 正常 | 标题 + 上次编辑时间 + 类型（文章）完整展示 |
| 微博 | https://card.weibo.com/article/v5/editor#/draft | ✅ 正常 | 草稿箱(02/30) + 标题列表可见 |
| 小红书 | https://creator.xiaohongshu.com/publish/publish | ❌ 仅本机 Profile | “草稿箱(N)”来自浏览器本地状态；同账号干净上下文不可见，不能作为云端草稿 |
| smzdm | https://post.smzdm.com/tougao/（「我的草稿」） | ✅ 正常 | 标题 + 字数 + 图片数 + 创建时间完整展示 |
| 知乎 | https://www.zhihu.com/creator/manage/creation/drafts | ⚠️ 平台侧 | Web 草稿箱 UI 显示「系统升级中，请稍后再试」，列表不可见；**我们的保存验证走 my_drafts API 兜底，正常**（平台侧问题，无法从本系统修复） |
| 百家号 | https://baijiahao.baidu.com/builder/rc/manage（内容管理） | ⚠️ 入口深 | 草稿在 SPA 后台「内容管理-草稿」内，入口交互复杂；未发现展示异常证据 |
| ZOL | https://post.zol.com.cn/v2/home（创作者中心「管理」） | ⚠️ 入口深 | blog 域已迁移论坛；草稿在创作者中心管理菜单内，需真实点击进入 |

## 二、已知问题（如实记录）

1. **知乎 Web 草稿箱「系统升级中」**：平台侧故障，本系统无法修改；投递验证不受影响（API 兜底）。
2. **图片上传自动化限制**（2026-08 验收结论，见 platforms/weibo.py 与 platforms/baijiahao.py docstring）：
   - 微博正文插图：编辑器无 file input / 插入菜单无图片 / drop 无效 → 自动化无法插图
   - 百家号封面上传：filechooser 上传后预览不落图 → 自动化受限
   - 因此**投递草稿目前为纯文字**（图片块在部分平台无法自动上传）；用户手动粘贴/拖拽可补充
3. **百家号 / ZOL 草稿箱入口**：SPA 深层菜单，自动化进入成本高；未发现展示异常。
4. **小红书长文草稿非云端**：原隔离 Profile 可见的卡片不会随账号 Cookie 出现在
   干净浏览器中；自动投递已关闭，只保留账号管理。

## 三、系统侧增强（已上线）

- 投递计划结果中，**DRAFT_SAVED 目标显示「查看平台草稿箱」按钮**（一键新标签打开对应平台草稿箱，见 content-studio.js `platformDraftBoxUrl`）。

## 四、待用户确认/决定

- 如需验证**带图片草稿**在平台草稿箱的展示，需真实投递带图草稿（涉及平台限制，需明确选择账号并确认后执行）。
- 百家号 / ZOL 草稿箱如用户有具体展示问题，提供现象后针对性排查。
