# CoreUI 上游基线与二开边界

## 固定基线

ArticleOps 的管理后台以 CoreUI Free Bootstrap Admin Template `v5.6.0`
为结构基线。为满足生产基线的精确锁定要求，`package.json` 与
`package-lock.json` 均将 `@coreui/coreui` 固定为 `5.9.0`，并将
SimpleBar 固定为 `6.3.3`。

- 上游 SSH：`git@github.com:coreui/coreui-free-bootstrap-admin-template.git`
- 上游 tag：`v5.6.0`
- 上游 commit：`da2c89f5e71a762fb46a3583f42d5f740d965b1d`
- 瘦身前 ArticleOps CoreUI v5.6.0 vendored 快照：`origin/archive/coreui-full-v5.6.0`
- 最小可复现源码：`frontend/coreui-free-bootstrap-admin-template/`
- 生产编译产物：`web/static/vendor/coreui-template/`
- 许可证：`frontend/coreui-free-bootstrap-admin-template/LICENSE`
- 上游忽略规则：`frontend/coreui-free-bootstrap-admin-template/.gitignore`

该远程归档保留的是本次瘦身前 ArticleOps 已引入、已固定依赖的
vendored 快照，并非官方完整且未修改的上游源码树。官方完整源码以
上述 SSH 仓库的 `v5.6.0` tag 和 commit 为准。

## 精简范围

瘦身前 ArticleOps vendored 目录包含 1,138 个文件、8,376,776 字节。
本次从活跃分支移除了 1,129 个原有文件，包括演示页面、Pug 模板、示例
图片、示例图标、图表脚本、示例样式及不再使用的上游开发配置。这些内容
没有被 ArticleOps 模板、业务 JavaScript、生产静态资源或发行脚本引用，
已经从活跃分支移除；瘦身前的 ArticleOps vendored 状态仍由上述远端
归档分支保存。活跃目录另新增了一个确定性运行资产构建脚本。

活跃目录只保留：

- MIT License、上游版本和锁文件。
- `src/scss/style.scss` 与 SimpleBar 的应用级补充样式。
- PostCSS 配置和确定性运行资产构建脚本。
- 构建与来源说明。

同时移除了仅用于演示站的图表、图标、Pug、同步、预览、lint 和格式化
依赖。活跃源码最终为 10 个文件、73,414 字节，文件数和体积均减少
99.12%。锁文件中的包节点由 729 个降至 121 个；全新 Windows 目录执行
`npm ci` 实际安装 107 个包。这不是 CoreUI 版本升级，`@coreui/coreui`
仍精确固定为 `5.9.0`。

## 实际构建依赖

| 依赖 | 锁定版本 | 保留原因 |
| --- | --- | --- |
| `@coreui/coreui` | `5.9.0` | 编译 CoreUI SCSS，并提供生产 JavaScript bundle |
| `simplebar` | `6.3.3` | 侧边栏滚动 CSS 与 JavaScript |
| `sass` | `1.102.0` | 将 `src/scss/style.scss` 编译为 CSS |
| `postcss` / `postcss-cli` | `8.5.26` / `11.0.1` | 执行生产 CSS 后处理 |
| `autoprefixer` | `10.5.4` | 补齐目标浏览器前缀 |
| `postcss-combine-duplicated-selectors` | `10.0.3` | 合并重复选择器 |
| `postcss-drop-empty-css-vars` | `0.0.0` | 清理空 CSS 变量 |
| `clean-css-cli` | `5.6.3` | 生成生产压缩 CSS |
| `rimraf` | `6.1.3` | 跨平台清理 `dist/` |

以上版本来自当前 `package-lock.json`；开发机和 CI 必须使用 `npm ci`，
不得用普通安装临时改写锁文件。

## 生产运行

Python/Flask 运行时直接服务已编译的 CoreUI CSS、CoreUI bundle 和
SimpleBar 资产，不需要在现场安装 Node.js。开发者需要重建运行资产时：

```powershell
Set-Location frontend/coreui-free-bootstrap-admin-template
npm ci
npm run build
```

构建结果严格限定为生产需要的五个文件：License、CoreUI CSS、CoreUI
JavaScript 和两份 SimpleBar 资产。生产不使用 source map，因此产物不会
引用或携带 `.map` 文件。运行以下命令可比较构建结果与仓库中的生产资产，
比较时只规范化 Windows/Unix 换行，不忽略其他差异：

```powershell
npm run verify-vendor
```

完整演示站原构建会生成 1,288 个文件、约 20.18 MB；精简构建只生成
5 个文件、481,812 字节。

仓库根目录原有 `build/` 忽略规则也会匹配嵌套目录；新增构建脚本首次纳入
Git 时必须显式暂存。嵌套 `.gitignore` 继续排除 `node_modules/` 与 `dist/`，
保证构建后工作树保持干净。

## ArticleOps 修改范围

ArticleOps 以固定上游版本为基础，在业务模板和样式层实现：

- 中文化的侧边栏、Header、面包屑和响应式容器。
- “创作与投递”业务工作台。
- 账号会话和发布任务的业务扩展样式。
- 本地 IndexedDB 恢复、服务端乐观锁和投递确认交互。

业务扩展源码位于 `web/static/` 与对应的 Jinja 模板，不覆盖上游
MIT 版权文件。
