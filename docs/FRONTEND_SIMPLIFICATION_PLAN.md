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
