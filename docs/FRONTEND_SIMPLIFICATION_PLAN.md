# 前端交互精简计划（对标行业设计）

> 分支：`feat/zhihu-real-delivery` · 文档日期：2026-08-14
> 状态：已获用户批准，待执行（阶段 A 先行）

## 一、背景与目标

- 现状：`/upload` 已是单一「创作+投递」入口（符合行业"一个 Composer"理念），但存在 **3 处入口重复、2 套导航、1 个遗留页面**，`/accounts` 与 `/upload` 存在来回跳。
- 目标：对标 蚁小二/融媒宝（国内：左侧导航 + 创作页勾选分发）与 Buffer（国外：one composer + 视图切换）的交互，**去冗余、收敛导航、统一风格**，不动后端契约。
- 风格红线：只用现有组件类（btn / badge / card / modal）+ 统一继承 base 骨架；不引入新样式库。

## 二、风格基线（改动红线）

```
框架：CoreUI（Bootstrap 5 基础）
骨架：base.html 侧边栏+顶栏（/、/upload、/accounts 已继承；/data-center 需并入）
组件：btn / badge(text-bg-*) / card / CoreUI Modal / breadcrumb / form-control
自定义 CSS：仅 content-studio.css、account-sessions.css（不新增文件）
```

## 三、阶段 A：去冗余（低风险，纯删减，先行）

| # | 改动 | 文件 | 验证 |
|---|---|---|---|
| A1 | 删除遗留 delivery 页 | 删 `delivery.html` / `delivery.js` / `delivery.css` 及模板引用；`/delivery/new` 保留 302 → `/upload`；删除相关前端测试 | 前端测试更新 + `/delivery/new` 302 验证 |
| A2 | data-center 并入统一导航 | `dashboard.html` 删独立侧边栏与 `#module-*` 锚点链接，改用 base 骨架 | 页面 200 + 导航高亮正确 |
| A3 | 首页入口去重 | `base.html` 删顶部「＋创作与投递」按钮（保留侧边栏）；`index.html` 空状态链接保留（引导作用） | 首页 200 + 导航一致性 |

## 四、阶段 B：交互收敛（中风险，体验优化）

| # | 改动 | 说明 |
|---|---|---|
| B1 | `/upload` 账号管理平台卡提示化 | 整卡点击跳转 → 卡内弱化文字链接「账号管理 → 平台账号页」，避免误触来回跳 |
| B2 | 账号卡按钮分组 | 每账号 5 按钮 → 主操作（重新登录/删除）外露，次要（验证/活动日志）收进「更多」下拉 |

## 五、阶段 C：对标升级（大改，逐项评估后拍板）

| # | 改动 | 对标 |
|---|---|---|
| C1 | 目标构建器支持「多平台多账号勾选」批量添加 | 蚁小二 / Buffer 勾选分发 |
| C2 | 首页 legacy 队列 → 302 `/upload`（`/task/{id}` 保留） | 统一单入口 |

## 六、每轮执行流程（A 阶段每项）

```
改代码 → ruff（改动文件）→ tests/frontend + 全量 pytest
→ 逐页 HTTP 200 + 关键元素 → focused commit → push（rebase 前端分支）
→ 完成后给出「浏览器人工核对清单」（导航 / 按钮 / 弹窗 / 徽章）
```

## 七、执行顺序

**A1 → A2 → A3**（每项一提交一推送）→ 用户验收风格 → B 逐项确认 → C 评估后拍板

> 阶段 A 已完成（2026-08-14）：A1 删 delivery（`c193d78`）、A2 data-center 导航对齐（`3533acb`）、A3 首页入口去重（`9ef1139`）。

---

## 八、阶段 D：账号页清理 + 目标选择器滑块化（用户新需求，本次执行）

### 背景
- `/accounts` 页面存在新旧两套账号界面并存：上面是 legacy 硬编码板块（仅 ZOL/小黑盒，用旧接口 `/api/accounts`），下面是新「多账号会话」模块（全平台动态）。**以新模块为准**，上面是冗余。
- `/upload` 投递目标选择器交互繁琐（逐个「平台 → 账号 → 添加目标」），用户要求改造成 **iOS 风格滑块**：竖排一列，先滑动平台开关启用，再勾选该平台一个或多个账号。
- 投递模式（平台草稿 / 公开发布）也要滑块化选择。
- `/upload` 编辑器目前只有自动保存（无显式保存按钮），需补「保存」按钮；图片插入需增强（后续）。

### D1 删除 accounts legacy 板块
| 改动 | 文件 |
|---|---|
| 删 ZOL/小黑盒 硬编码账号卡、legacy `reloginModal`、legacy toast 容器、内嵌 Alpine `accounts` 脚本、内嵌 legacy 样式 | `web/templates/accounts.html` |
| 页面头部容器去 Alpine 化（`x-data="accounts"` → 普通容器） | 同上 |
| 更新前端契约测试：accounts 不再依赖 legacy `/api/accounts` | `tests/frontend/test_frontend_contracts.py` |

### D2 投递目标选择器滑块化（竖排 + 平台开关 + 账号多选 + 模式滑块）
| 改动 | 文件 |
|---|---|
| 竖排一列平台行：logo + 平台名 + 平台开关（Bootstrap `form-switch`） | `web/templates/upload.html` |
| 开关开 → 行内展开该平台账号多选（checkbox，可勾选一个或多个） | `web/static/js/content-studio.js` |
| 每个勾选目标显示模式滑块（平台草稿/公开发布，二段 switch） | 同上 |
| 勾选变化实时更新 targets（PUT 批量）；校验保留（至少一目标、账号 VALID、不重复、`delivery_enabled` 才可投递） | 同上 |
| 后端契约不变（仍是 `platform + account_id + mode` targets 数组） | — |
| 更新目标构建器前端测试 | `tests/frontend/test_content_studio_contracts.py` |

### D3 编辑器显式保存按钮
| 改动 | 文件 |
|---|---|
| 编辑区加「保存」按钮（触发 `saveDraftNow()` 手动同步），自动保存保留 | `web/templates/upload.html` + `content-studio.js` |

## 九、阶段 E：后续增强（待逐项拍板）

| # | 改动 | 对标 |
|---|---|---|
| E1 | 图片插入增强（工具栏插入 / 拖拽插图） | 行业编辑器 |
| E2 | 首页 legacy 队列 → 302 `/upload`（`/task/{id}` 保留） | 统一单入口 |
| E3 | `task_detail.html` 随 E2 决定保留/下线 | — |

## 十、风格基线（阶段 D/E 沿用）

- 只用现有组件类：Bootstrap `form-switch`、`btn`、`badge`、`card`、CoreUI Modal；不引入新样式库
- 自定义样式进现有 css 文件（content-studio.css），不新增文件
- 不动后端契约、不碰 `uv.lock`、不真实发布
- 每轮：ruff → tests/frontend + 全量 pytest → 页面 200 → focused commit → push → 给出浏览器人工核对清单

## 十一、执行顺序（阶段 D）

**D1（删 legacy 板块）→ D2（目标选择器滑块化）→ D3（保存按钮）**，每项一提交一推送，完成后用户浏览器验收。
