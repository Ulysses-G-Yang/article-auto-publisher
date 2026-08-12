# 多账号会话管理 UI

## 兼容边界

`/accounts` 原有 Alpine 登录区、旧接口、重新登录确认框和轮询逻辑保持不变。新增区域是相邻的原生 JavaScript 组件，仅修改前端模板和静态资源，没有修改路由、API、模型、数据库或平台实现。

## 使用流程

1. 初始不加载多账号数据，用户先选择小黑盒或中关村在线。
2. 前端调用 `GET /api/platforms/{platform}/accounts?usable=false`，展示该平台全部公开账号。
3. 卡片只使用 `account_id`、`display_name`、`masked_platform_user_id`、`status`、`session_status`、`persist_login`、`last_verified_at`。
4. `UNVERIFIED`、`LOGIN_REQUIRED`、`ERROR`、`EXPIRED` 可以启动“仅验证现有登录态”；`VALID` 可以退出单个账号。
5. “添加该平台账号”调用平台级 login API，由后端创建隔离 Profile 并打开交互登录。
6. 账户级退出使用 `POST /api/account-sessions/{account_id}/logout`；活动日志使用 `GET /api/account-sessions/{account_id}/activity`，在同页 CoreUI 抽屉中加载并包含加载、失败和空状态。

## 有限目标轮询

验证或新增登录后，只有响应明确给出 `account_id` 时才启动目标轮询。轮询使用单次 `setTimeout` 链，最多 12 次、间隔 2.5 秒；新目标会停止旧目标。没有账号 ID 时不进行全局猜测轮询，用户可点击“刷新账号”。

账号列表接口是当前唯一状态读取接口，因此每次目标轮询会刷新当前平台列表，但停止条件只检查触发操作的目标账号，不会无限轮询所有账号。

## 隐私与错误

组件先将响应投影到公开字段白名单，再渲染 DOM。不会读取或展示 Profile 路径、Cookie、Token 或原始平台用户 ID。账号列表、会话策略、验证、登录、退出和活动日志都有明确错误状态；未知字段不会整体转储到页面。

## 验证

```powershell
python -m pytest tests/frontend tests/test_article_mvp.py tests/test_regression.py -q
node --check web/static/js/account-sessions.js
git diff --check
```
