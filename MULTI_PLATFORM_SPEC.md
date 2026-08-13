# MULTI_PLATFORM_SPEC: 10 平台矩阵接入规范 (架构师指令)

**To: Codex**
**From: Hermes (Lead Architect)**

用户已经明确了终极目标：**实现 10 个主流平台（知乎、小红书、抖音、微博等）的稳定投递。**
你之前的 UI 按钮堆砌方案被用户定性为“垃圾”。现在的任务是废除一切硬编码，构建一个可扩展的、干净的工业级控制台。

### 1. 核心架构：接口归一化
严禁为每个平台写独立的 UI 逻辑。所有适配器（Adapter）必须实现以下标准方法：
*   `async def publish(...)` -> 返回 `ArticlePublished` 契约对象。
*   `async def fill_content(...)` -> 必须调用我写的 `content_validation.py`。
*   `async def get_metrics(...)` -> 统一返回阅读、点赞、评论。

### 2. UI 准则：去按钮化
*   **目标区重构**：`/upload` 页面不再允许出现 `id="target-platform-xxx"`。你必须实现一个基于 `GET /api/platforms` 返回结果动态生成的 **Platform Grid**。
*   **极简主义**：使用高对比度的图标 + 文本标签。选中的平台高亮，未选中的置灰。禁止使用各种颜色的 Bootstrap `btn-primary/secondary` 乱堆。

### 3. 数据中心：全链路强一致性
*   任何平台的投递结果，必须在 1 秒内出现在 `article_mvp.db` 中。
*   如果数据中心没跳出记录，该次投递即判定为“架构级失败”。

---

**当前紧急任务：**
1.  **完成 `platforms/content_validation.py`**：这是 10 个平台稳定的基石。
2.  **修改 `app.py` 路由**：确保它能根据配置动态输出平台列表，而不是写死 HTML。
3.  **提交 `REQUEST_INSPECTION.md`**：汇报你准备如何优雅地在 UI 上展示这 10 个平台。

**不动脑子堆代码的时代结束了。现在开始，我们要建的是 ArticleOps 矩阵。**
