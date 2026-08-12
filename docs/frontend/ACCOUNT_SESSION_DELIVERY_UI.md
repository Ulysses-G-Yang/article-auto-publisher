# 多账号内容投递 UI

## 页面与边界

前端模板为 `web/templates/delivery.html`，预期由后端挂载到 `GET /delivery/new`。本次没有修改路由、API、数据库、账号模型或平台实现。页面复用现有 CoreUI/Bootstrap 5 壳层，不引入新框架。

页面按“平台 → 账号 → 内容与模式”组织：初始不加载账号；选择平台后才调用 `GET /api/platforms/{platform}/accounts?usable=true`。切换平台会立即清空旧账号和选择，并通过 `AbortController` 与请求序号共同阻止旧响应覆盖新平台。

## 账号与隐私

账号选择器使用 `btn-check` radio 横向滑动带，不会自动选择账号。只有 `session_status=VALID` 可以选择；其他状态保持可见但禁用。界面只读取 `display_name`、`masked_platform_user_id`、状态、会话策略和验证时间，不读取或展示原始平台用户 ID、Profile 路径、Cookie 或 Token。

“保持登录态”仅调用 `POST /api/accounts/{account_id}/session-policy` 更新 `persist_login`，不是登录状态开关。账号加载、会话策略和执行单分别有独立错误区。

## 草稿与公开发布

默认模式是 `DRAFT`。草稿在本地显示平台、脱敏账号、标题和模式摘要，用户确认后才创建执行单。

`PUBLISH` 首次提交不携带确认令牌。只有服务器返回 HTTP 428 和 `PUBLISH_CONFIRMATION_REQUIRED` 后，页面才保存一次性令牌并显示高风险确认框。用户明确点击“我已核对，确认公开发布”后，才用该令牌再次提交。平台、账号或模式变化都会清除旧令牌。

成功响应必须与当前平台和账号一致，随后展示 `operation_id`、状态、模式和账号活动日志链接。

## 示例内容缺口

标题按冻结要求预填为《凌晨三点，公司的智能马桶开始给我做绩效面谈》。当前前端任务上下文没有该文章的完整正文，因此正文保持空白并提供明确占位提示，未自行编造另一版本。集成前需由内容所有者粘贴并核对完整正文。

## 验收

```powershell
python -m pytest tests/frontend tests/test_article_mvp.py tests/test_regression.py -q
node --check web/static/js/delivery.js
git diff --check
```

手动验收需覆盖：初始无账号请求、快速切换平台、无自动选中、失效账号禁用、会话策略失败回滚、DRAFT 摘要确认、PUBLISH 428 二次确认、响应平台/账号错配、390px 横向账号带、键盘焦点与模态框关闭。
