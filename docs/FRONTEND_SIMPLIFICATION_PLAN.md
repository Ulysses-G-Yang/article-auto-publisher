# 前端交互精简计划（完整版）

> 分支：`feat/zhihu-real-delivery` · 更新：2026-08-14
> 原则：对标行业（蚁小二/融媒宝勾选分发、Buffer one-composer）；只用现有组件类；不动后端契约；每轮测试+commit+push。

---

## 一、已完成 ✅

| 阶段 | 内容 | commit |
|---|---|---|
| A1 | 删除遗留 delivery 页（无路由死文件，-661 行）；`/delivery/new` 302 保留 | `c193d78` |
| A2 | data-center 侧边栏对齐共享导航；删「数据模块」锚点分组 | `3533acb` |
| A3 | 删除顶部冗余「＋创作与投递」按钮，侧边栏为唯一入口 | `9ef1139` |
| 计划 | 本计划文档入库 | `8d813d8` |

## 二、进行中 / 待办（按价值排序）

### 阶段 D：账号页清理 + 交互改版（用户新需求）

**D1 删除 /accounts 遗留板块**（去重）
- 删 ZOL/小黑盒 硬编码卡、legacy reloginModal/toast、内嵌 Alpine `accounts` 脚本与 legacy 样式
- 页面头部去 Alpine 化；保留新「多账号会话」模块（全平台动态）
- 更新前端契约测试（accounts 不再依赖 legacy `/api/accounts`）

**D2 投递目标选择器滑块化**（核心）
- 竖排一列平台行：logo + 平台名 + **平台开关**（Bootstrap `form-switch`，仅 `delivery_enabled` 平台显示）
- 开关开 → 懒加载该平台账号 → 展开 **账号多选**（checkbox，可勾选多个）
- 每个平台行有**模式滑块**（平台草稿 / 公开发布，二段 switch）
- 勾选变化 → 合并生成 targets（`platform + account_id + mode`）→ 防抖 PUT（复用 saveTargets 冲突处理）
- 移除：平台网格单选卡、账号下拉、「添加目标」按钮；目标列表与滑块同源
- 校验保留：账号 VALID 才可勾选、至少 1 目标才能建计划
- 后端契约零改动

**D3 编辑器显式保存按钮**
- 编辑区加「保存」按钮（手动触发 `saveDraftNow()`）；自动保存保留

### 阶段 E：后续增强（逐项拍板）

| # | 改动 |
|---|---|
| E1 | 图片插入增强（工具栏插入 / 拖拽插图） |
| E2 | 首页 legacy 队列 → 302 `/upload`（`/task/{id}` 保留） |
| E3 | `task_detail.html` 随 E2 决定保留/下线 |

---

## 三、风格红线

- 只用现有组件：`form-switch` / `btn` / `badge` / `card` / CoreUI Modal；不引入新样式库
- 自定义样式进现有 css（content-studio.css），不新增文件
- 不碰 `uv.lock`、不真实发布

## 四、每轮执行流程

```
改代码 → ruff → tests/frontend + 全量 pytest → 页面 200 → focused commit → push
→ 完成后给出「浏览器人工核对清单」
```

## 五、执行顺序

**D1 → D2 → D3**（每项一提交一推送）→ 用户浏览器验收 → E 逐项拍板

---

## 附：服务状态排查（2026-08-14）

- 发现 Flask 服务曾中断（5000 端口拒绝连接，页面全挂）→ 已重启恢复，四页全部 200
- 服务以 `Start-Process` 后台运行，无守护；若再中断请告知或直接重启
