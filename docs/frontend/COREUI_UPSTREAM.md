# CoreUI 上游基线与二开边界

## 固定基线

ArticleOps 的管理后台以 CoreUI Free Bootstrap Admin Template `v5.6.0`
为结构基线。为满足生产基线的精确锁定要求，`package.json` 与
`package-lock.json` 均将 `@coreui/coreui` 固定为 `5.9.0`。

- 上游 SSH：`git@github.com:coreui/coreui-free-bootstrap-admin-template.git`
- 上游 commit：`da2c89f5e71a762fb46a3583f42d5f740d965b1d`
- 完整源码：`frontend/coreui-free-bootstrap-admin-template/`
- 生产编译产物：`web/static/vendor/coreui-template/`
- 许可证：`frontend/coreui-free-bootstrap-admin-template/LICENSE`
- 上游忽略规则：`frontend/coreui-free-bootstrap-admin-template/.gitignore`

## 已知上游依赖风险

在固定的 `v5.6.0` 基线执行 `npm ci` 后，`npm audit` 报告 19 项
开发工具链依赖漏洞：5 项 moderate、13 项 high、1 项 critical。
这些问题来自上游构建依赖，不进入 Flask 生产运行时；本次为保证二开
基线可复现而原样记录，未擅自升级或改写锁文件。后续应在独立升级任务中
对照新版 CoreUI 构建结果、视觉回归和许可证后再处理。

## 生产运行

Python/Flask 运行时直接服务已编译的 CoreUI CSS、CoreUI bundle 和
SimpleBar 资产，不需要在现场安装 Node.js。开发者需重建上游基线时：

```powershell
Set-Location frontend/coreui-free-bootstrap-admin-template
npm ci
npm run build
```

上游的 `build/**` 构建脚本与 `.gitignore` 必须一同保留。仓库根目录原有
`build/` 忽略规则也会匹配嵌套目录，因此首次纳入时使用强制暂存；文件
进入 Git 后可在全新 checkout 直接构建。嵌套 `.gitignore` 会继续排除
构建产生的 `node_modules/` 与 `dist/`，保证构建后工作树保持干净。

## ArticleOps 修改范围

ArticleOps 不直接改写上游基线，而是在它的布局与组件结构上实现：

- 中文化的侧边栏、Header、面包屑和响应式容器。
- “创作与投递”业务工作台。
- 账号会话、发布任务和数据中心的业务扩展样式。
- 本地 IndexedDB 恢复、服务端乐观锁和投递确认交互。

业务扩展源码位于 `web/static/` 与对应的 Jinja 模板，不覆盖上游
MIT 版权文件。
