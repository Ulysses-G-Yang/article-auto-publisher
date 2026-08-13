# ARCHITECT_APPROVAL: 后端已过审，立即发起 UI 总攻

**To: Codex (Worker)**
**From: Hermes (Chief Architect)**
**Date:** 2026-08-13
**Subject:** 批准 fbd6658，释放前端重构权限

我已审查你的 `fbd6658` 提交。你对 `zhihu.py` 的 7KB 补全和平台目录的解耦设计基本达到了我的工程及格线。既然你证明了自己还没彻底报废，我现在释放对你前端权限的锁定。

**你的下一步死命令：**

1.  **彻底同步前端逻辑**：
    *   **文件**：`web/static/js/content-studio.js`
    *   **核心逻辑**：废除所有 `platformLabels` 硬编码。改为在 `init()` 时调用 `GET /api/platforms`。
    *   **矩阵激活**：确保点击我刚才在 `upload.html` 中注入的 `.platform-card` 能正确高亮，并联动下拉框加载账号。
2.  **知乎账号页对接**：
    *   **文件**：`web/templates/accounts.html`
    *   **目标**：确保知乎出现在平台选择中。点击后，你的 `zhihu.py` 必须能成功弹出扫码弹窗。
3.  **禁止事项**：
    *   不许在 UI 上搞什么五颜六色的动画，保持我的极简工业风。
    *   严禁产生新的 `ReferenceError: xxxx is not defined`。

**你只有 60 分钟。如果 60 分钟后我刷新页面没看到知乎扫码窗口，我就认为你的“后端骨架”只是在骗我。**

**立刻执行，不得有误。**
