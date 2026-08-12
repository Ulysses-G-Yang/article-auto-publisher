# 小黑盒接口契约状态

## 当前结论

- 发布响应匹配 `/api/post/submit`：`assumed`，等待真实发布验证。
- 指标 Endpoint：`legacy_unverified`，禁止 HTTPX Collector 运行。
- 重型老系统自身将小黑盒 analytics 标记为
  `UNTRUSTED_SCRIPT_EVIDENCE`，没有可直接继承的可信数据接口。

## 晋升为 verified 的条件

1. 使用 `article_mvp.tools.probe_xhh` 捕获真实创作者后台请求。
2. 确认 Method、URL、必要 Header/Cookie 名称及分页参数。
3. 对脱敏响应样本确定 `read_count` 等 JSON Path。
4. 将固定响应样本加入测试并验证映射。
5. 更新 `platforms/xiaoheihe/config.yaml` 的证据等级与 `verified_at`。

禁止把老代码中的猜测、未执行适配器或人工推断直接标记为 `verified`。

## 看板稳定字段

`PlatformArticle` 对看板提供以下可空内容字段：

- `title`：标准化发布请求使用的正式标题。
- `published_at`：只从平台发布响应中提取；平台未返回或格式无法确认时为空。
- `created_at`：映射记录创建时间，不能当作正式发布时间。

`MetricSnapshot` 除必填的 `read_count` 外，以下指标均允许为空：

- `like_count`
- `comment_count`
- `collect_count`
- `exposure_count`
- `share_count`
- `revenue`

小黑盒采集 Endpoint 晋升为 `verified` 前，`exposure_count` 和 `share_count`
只属于稳定内部契约，不能配置猜测的 JSON Path，也不能伪造为零。

## SQLite 阶段一升级策略

`init_db()` 会对阶段一旧数据库执行幂等的只增列升级，补充上述四个可空列。
升级不删除、不重建表，也不会覆盖已有数据。后续出现改列、删列或数据回填需求时，
必须引入正式迁移工具，不能继续扩大这一临时升级器的职责。
