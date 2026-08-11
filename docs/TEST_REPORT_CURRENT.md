# 当前版本测试报告

- 执行日期：2026-08-11
- 分支：`agent/account-management-publishing-fixes`
- Python：3.12.13（`article-publisher-py312`）
- pytest：8.4.2
- Playwright：1.49.1
- Flask：`127.0.0.1:5000`，Debug 已关闭
- MCP：`10.0.0.28:8765`，`/healthz` 返回 200

## 自动化结果

```text
conda run -n article-publisher-py312 python -m unittest discover -s tests -v   46 passed
conda run -n article-publisher-py312 python -m pytest -q                       46 passed
conda run -n article-publisher-py312 python scripts/check_environment.py       passed
conda run -n article-publisher-py312 python -m compileall                       passed
```

覆盖范围包括：

- ZOL Cookie 有效/过期识别、标题和 iframe/textarea/contenteditable 正文输入；
- ZOL 图片全失败/部分失败结果；
- ZOL 话题精确匹配和歧义转人工选择；
- 小黑盒社区/话题分离、图片失败和人工覆盖；
- 历史任务不自动启动、平台锁、浏览器关闭错误分类；
- 任务恢复 API、MCP Schema、异步任务持久化和 ZOL `needs_selection` 映射；
- Cookie 清理只作用于指定 Profile，不删除整个 Profile。

## 服务检查

- Flask `/api/status`：HTTP 200，队列运行中，当前无待处理任务。
- MCP `/healthz`：HTTP 200，`server_id=content.article-publisher`。
- `127.0.0.1:8765` 的其他 AutoMatrix 进程未停止；本项目 MCP 使用 `10.0.0.28:8765`。

## 真实回归结果

使用同一篇含 4 张图片的 DOCX、有效登录态和草稿模式完成：

- ZOL 任务 15：修复前未通过。第 1 张图片上传后残留图片弹窗遮罩，后续点击被拦截并进入 `paused/SELECTOR_ERROR`；该任务保留为缺陷证据，没有计入成功次数。
- ZOL 任务 16：自动候选歧义 → `needs_selection` → 人工传入“显示器分屏”恢复，4/4 图片、话题和草稿通过；
- ZOL 任务 17、18：自动话题“显示器分屏”连续两次通过，4/4 图片和草稿标题通过；
- 小黑盒任务 19、20：社区、话题、4/4 图片和草稿标题连续两次通过；
- 没有公开发布。

任务 15 暴露的问题已通过关闭残留图片弹窗、等待上传按钮可用、按 iframe 分开统计图片数量等代码修复，并由任务 16~20 的真实回归验证。当前没有未解决的自动化测试或本轮验收项；平台页面改版、验证码、账号权限变化仍属于外部风险。

## 生产预演与重启结果

- `run_flask_production.py` 使用 Waitress 在临时 `127.0.0.1:5001` 启动成功，`/api/status` 返回 200；
- 生产预演配置 `APP_DEBUG=false`、`PUBLISH_AFTER_DRAFT=false`；
- 恢复开发服务后，历史任务没有进入 `queued`、`processing` 或 `retrying`；
- MCP `10.0.0.28:8765/healthz` 重启后返回 200。

## 安全说明

本报告不包含 Cookie 值、密钥、MCP 签名 URL 或 Chrome Profile 内容。用户删除的 `工作汇报_731.md` 未纳入本次提交。
