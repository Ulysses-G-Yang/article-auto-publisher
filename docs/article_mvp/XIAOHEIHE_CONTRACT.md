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
