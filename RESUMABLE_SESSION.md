## [TASK] ArticleOps 数据中心下线兼容收口

- **Status**: 归档与三个兼容问题均已完成；全量回归 `1236 passed, 7 skipped`，仅有既有 aiosqlite 线程清理 warning。
- **Next Action**: 核对 5000/8765 监听与进程绝对入口；只从当前 worktree 最新远端 SHA 启动服务。
- **Critical State**:
  - 工作树：`D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`
  - 分支：`refactor/dashboard-ai-model-picker`
  - 修复提交：`cd5168a`（只读 API 与旧书签兼容）、`eca8c20`（升级删除及回滚）、`ee24b68`（主站范围重定向）。
  - 内置数据中心模板和静态资源继续保持删除；`/data-center/api/dashboard` 与 `/data-center/healthz` 必须保留。
  - 三条归档分支已在远端保留并建立云端归档说明，本地 `archive/*` 引用为空。
  - 禁止触碰 `data/`、Profile、Cookie、Token、`uv.lock`；禁止启动旧 checkout 的服务进程。
- **Recovery Path**: `cd D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication && type .codex\hybrid-attention-context-guard\checkpoints\20260903-dashboard-removal-fixes.json`
