# CoreUI 上游基线与二开边界

## 固定基线

ArticleOps 的管理后台以 CoreUI Free Bootstrap Admin Template `v5.6.0`
为结构基线，它的 `package-lock.json` 将 `@coreui/coreui` 解析为 `5.9.0`。

- 上游 SSH：`git@github.com:coreui/coreui-free-bootstrap-admin-template.git`
- 上游 commit：`da2c89f5e71a762fb46a3583f42d5f740d965b1d`
- 完整源码：`frontend/coreui-free-bootstrap-admin-template/`
- 生产编译产物：`web/static/vendor/coreui-template/`
- 许可证：`frontend/coreui-free-bootstrap-admin-template/LICENSE`

## 生产运行

Python/Flask 运行时直接服务已编译的 CoreUI CSS、CoreUI bundle 和
SimpleBar 资产，不需要在现场安装 Node.js。开发者需重建上游基线时：

```powershell
Set-Location frontend/coreui-free-bootstrap-admin-template
npm ci
npm run build
```

## ArticleOps 修改范围

ArticleOps 不直接改写上游基线，而是在它的布局与组件结构上实现：

- 中文化的侧边栏、Header、面包屑和响应式容器。
- “创作与投递”业务工作台。
- 账号会话、发布任务和数据中心的业务扩展样式。
- 本地 IndexedDB 恢复、服务端乐观锁和投递确认交互。

业务扩展源码位于 `web/static/` 与对应的 Jinja 模板，不覆盖上游
MIT 版权文件。
