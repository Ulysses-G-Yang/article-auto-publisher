## [TASK] ArticleOps 多平台图文草稿验收

- **Status**: 百家号完整 Word + 7 图 + 封面 + 持久化重开已通过 | **Next Action**: `只读审计小红书封面控件与草稿重开 DOM`
- **Critical State**: 分支 `feat/zhihu-real-delivery`，HEAD `fb290126cc5b9103f38d4f1185e943c14a424633`；百家号操作单 `50fc0033-0e6f-4b1a-8029-ddd25431e12b` 为 `DRAFT_SAVED`，映射 `SUCCEEDED`；公开发布关闭；`uv.lock` 未跟踪且不得触碰。
- **Verification**: `718 passed, 2 skipped`；Ruff、py_compile、`git diff --check` 通过；本地/远端 SHA 一致。
- **Recovery Path**: `cd D:\Backup\Documents\article-auto-publisher && Get-Content .codex\hybrid-attention-context-guard\checkpoints\20260820-0212-baijiahao-cover.json`
