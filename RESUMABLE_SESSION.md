## [TASK] ArticleOps AI guided publication phases 1-5

- **Status**: 阶段 1-5 已完成；验证结果为 `1227 passed, 7 skipped`。**Next Action**: 部署本分支后打开 `/settings/ai` 填写 DeepSeek 配置，再在 `/upload` 对一篇非敏感测试稿执行一次只读建议验收；MCP 和自动采纳仍未接入。
- **Critical State**: 工作树 `D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`，分支 `feature/ai-guided-publication`。
  - 阶段 1：冻结发布基线文档（`c7b77a8978e9c88fdb3fb92a0a422f78fa27c046`）。
  - 阶段 2：冻结平台发布选择与投递契约（`b02501d197dd2c411d3e889b3682af343098669d`）。
  - 阶段 3：完成发布选项解析 fixture、账号级只读候选发现接口及安全边界（`c3ba5d0e11cd82ec11ed94283a64f1546c872e7b`、`fbf52ff1ef9e8698ec9dd0cca9a3abf68d667700`）。
  - 阶段 4：完成 DeepSeek 配置读取、严格 JSON 客户端及草稿级只读发布建议 API（`2337f0ca71cd6d411319a87ef80f08a9570f8e0d`）。
  - 阶段 5 后端：运行期内存设置、连接测试和动态 Advisor（`47a4cdca8b6484cf77bf284b35c6267907b61f90`）。
  - 阶段 5 前端：`/settings/ai` 设置页和 `/upload` 只读建议展示（`f990e0bff5dcbbadc0093690a6b2396685cd44e0`）。
  - 1440、768、390 三档隔离浏览器验收无横向溢出；保存后 Key 输入框清空，API 无密钥回显。
  - 未执行真实 DeepSeek 请求或任何平台操作；未接入 MCP；未触碰现有 5000 服务。
- **Recovery Path**: `cd D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication`，从上述基线和阶段提交恢复；本交接不依赖 checkpoint 文件。
