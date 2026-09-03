# Design QA — CoreUI 创作与投递工作台返工

## 对照与环境

- 视觉基线：仓库内固定的 CoreUI Free Bootstrap Admin Template `v5.6.0`。
- 官方本地构建截图：`docs/frontend/qa/coreui-official-v5.6.0-1280.png`。
- ArticleOps 工作台截图：
  - `docs/frontend/qa/content-studio-1440.png`
  - `docs/frontend/qa/content-studio-1024.png`
  - `docs/frontend/qa/content-studio-390.png`
- 集成服务：隔离的测试数据目录、公开发布关闭、账号自动执行关闭；
  验收未创建投递计划、未登录、未保存平台草稿、未公开发布。

## 官方派生结果

工作台沿用了官方基线的 256px 深色侧栏、白色 Header、浅灰画布、
面包屑、卡片边框、表单、Badge、Modal 和响应式层级。业务扩展只增加
图文块编辑、多目标投递与安全确认语义，没有复制旧页面的第二套外壳。
ArticleOps 品牌色取代官方 Dashboard 的示例 KPI 色，但间距、字号和信息
密度保持同一企业后台语境。

## 尺寸与逐页结果

在 1440、1024、390 三种实际 CSS viewport 宽度下逐页检查 `/`、
`/upload`、`/accounts`、`/task/999999`：

- 所有页面的 `documentElement.scrollWidth <= innerWidth`，无页面级横向溢出。
- 1440/1024 下固定侧栏和主区边界正常，没有重复的 256px 左偏移。
- 390 下侧栏默认完全移出屏幕，主内容占满可用宽度；内容、投递目标、
  核对执行和安全信息均按顺序完整渲染。
- 每条路由只有一个正确高亮导航项；修复后验收时间窗控制台错误为 0。

### 2026-08-12 移动端 P1 复验

先前的 390px 全页截图暴露出一个自动断言漏检：`body { overflow-x: hidden; }`
隐藏了实际向右越界的标题、操作按钮和正文块，单看 `scrollWidth` 会产生假阴性。
本轮移除该隐藏规则，同时给工作台网格、卡片、图文块和表单显式补齐
`width: 100%` 与 `min-width: 0`，移动端操作按钮允许换行。

应用内浏览器在集成 QA 服务上重新测量，结果如下（单位为 CSS px；表中是
同类关键元素的最小左边界与最大右边界）：

| Viewport | inner/client/scroll width | shell/card | title/block/target/execute | textarea | actions | 越界项 |
| --- | --- | --- | --- | --- | --- | --- |
| 390 | 390 / 390 / 390 | 12 / 378 | 28.67 / 361.33 | 75.73 / 350.27 | 39.73 / 350.27 | 0 |
| 1024 | 1024 / 1009 / 1009 | 范围内 | 范围内 | 范围内 | 范围内 | 0 |
| 1440 | 1440 / 1425 / 1425 | 范围内 | 范围内 | 范围内 | 范围内 | 0 |

390px 新截图为 `docs/frontend/qa/content-studio-390.png`，原截图宽度只有
374px 且关键编辑控件被裁切；新截图像素宽度与 CSS viewport 均为 390，
操作按钮、三步进度、标题输入和图文块工具条完整。1024 与 1440 复验
`bad=[]`，三种尺寸的应用控制台错误均为空。

隔离 QA 还用两个假 `VALID` 小黑盒账号复验了目标流程：先选平台才出现
两个动态昵称，账号初始值为空且不会自动选择；人工选择后添加一个目标，
刷新仍保留一个目标。全过程 `planCount=0`，未创建或执行 DeliveryPlan，
也未触发任何平台动作。

对应的显式 opt-in 浏览器回归位于
`tests/frontend/test_content_studio_browser.py`。它要求调用方提供隔离 QA URL，
默认测试套件会跳过；该测试同时断言 body 不能靠 `overflow-x: hidden` 掩盖
越界，并验证“平台→两账号→不自动选择→添加→刷新恢复”但不创建计划。

## 交互与安全检查

- 系统草稿真实从后端加载；前端没有硬编码种子正文。
- 无投递目标时点击“检查并继续”，页面显示“至少添加一个投递目标”，
  并将焦点移至第一个平台选项；不会创建计划。
- 选择小黑盒后才请求账号；隔离环境无账号时显示“当前平台没有可用账号”，
  不自动选择账号。
- `CREATING`、`PARTIAL_FAIL`、`RESULT_UNKNOWN` 均有中文状态和语义色；
  结果未知明确要求人工核对，页面不提供公开发布自动重试入口。

## 修复历史

1. 首轮 1280 浏览器检查发现官方 `.wrapper` 自带左内边距又叠加业务
   `margin-left`，主区被双重推移并产生横向溢出。已统一为单一 256px 偏移。
2. 同轮发现侧栏与导航容器同时初始化 `coreui.navigation`，控制台报重复实例。
   已移除重复数据标记；新标签页和最终验收时间窗错误均为 0。
3. 全新 checkout 发现仓库根 `build/` 规则误忽略上游构建脚本。已精确纳入
   官方 `build/**` 与嵌套 `.gitignore`，全新 checkout 的 `npm ci` 和
   `npm run build` exit 0，构建后 Git 工作树保持干净。
4. 390px 截图复审发现 `overflow-x: hidden` 掩盖实际裁切。已移除该规则，
   修正工作台收缩边界和按钮换行，并用元素矩形而非仅 `scrollWidth`
   建立防回归门；应用内浏览器复验无越界。

final result: passed
