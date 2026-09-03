## [TASK] ArticleOps AI guided publication phases 1-4

- **Status**: 阶段 1-4 已完成；验证结果为 `1221 passed, 7 skipped`。**Next Action**: 用户在外部 shell 配置 DeepSeek Key 后，单独授权一次真实 AI 建议请求验收；UI/MCP 仍未接入。
- **Critical State**: 工作树 `D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`，分支 `feature/ai-guided-publication`，基线 `0091eda1dcc4170a90641c41de5ccc9e64bc2006`。
  - 阶段 1：冻结发布基线文档（`c7b77a8978e9c88fdb3fb92a0a422f78fa27c046`）。
  - 阶段 2：冻结平台发布选择与投递契约（`b02501d197dd2c411d3e889b3682af343098669d`）。
  - 阶段 3：完成发布选项解析 fixture、账号级只读候选发现接口及安全边界（`c3ba5d0e11cd82ec11ed94283a64f1546c872e7b`、`fbf52ff1ef9e8698ec9dd0cca9a3abf68d667700`）。
  - 阶段 4：完成 DeepSeek 配置读取、严格 JSON 客户端及草稿级只读发布建议 API（`2337f0ca71cd6d411319a87ef80f08a9570f8e0d`）。
  - 未执行真实 DeepSeek 请求或任何平台操作；未接入 UI/MCP；未触碰现有 5000 服务。
- **Recovery Path**: `cd D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`，从上述基线和阶段提交恢复；本交接不依赖 checkpoint 文件。
