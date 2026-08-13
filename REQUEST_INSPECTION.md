# REQUEST_INSPECTION：多行正文编辑器校验修复

致首席架构师 Hermes：

获批范围已完成，请审查以下文件。未修改 `web/`、前端资产、数据库模型或 UI。

## 1. 共享校验层

- `platforms/content_validation.py`
  - HTML 实体最多四轮稳定反转义；明确覆盖 `&nbsp;`、数值实体和嵌套实体。
  - Unicode NFC；统一 CRLF/CR/NEL/LS/PS；规范化 NBSP/窄 NBSP/figure space。
  - 仅删除编辑器伪字符 `U+200B/U+2060/U+FEFF`，保留 ZWNJ/ZWJ。
  - 从单个多行 `text/heading` 块提取非空段落，使用单调 cursor 做有序、非重叠匹配；重复段落必须在实际正文中出现相同次数。
  - 稳定异常码 `CONTENT_VALIDATION_ERROR`；错误只包含段落索引、长度和脱敏短预览，不返回实际编辑器全文。
  - `safe_media_error()` 清除 Windows/UNC/POSIX 物理路径，以及完整 Authorization/Cookie/Token/Password 值。

## 2. 平台适配器

- `platforms/xiaoheihe.py`
  - 保留原写入排版（块间双 Enter、块内单 Enter）和全部媒体状态字段。
  - 写入文字后按段落有序校验；图片处理后重新定位编辑器并再次校验。
  - `failed_images` 只保留文件名和脱敏错误，不包含本机路径。
- `platforms/zol.py`
  - `_resolve_content_editor()` 明确拆分 iframe / textarea / contenteditable 三种策略。
  - `_read_content_editor_text()` 对 textarea 只调用 `input_value()`，其余只调用编辑器范围的 `inner_text()`。
  - 跳过隐藏旧 iframe；图片操作后重新定位编辑器再读回校验。
  - 保留无图 `fill()` 快速路径、原有图文块顺序和全部媒体错误码/状态字段。

## 3. 测试证据

- `tests/test_content_validation.py`
- `tests/test_regression.py`
- `tests/test_content_delivery_adapter_chain.py`

覆盖：实体/NFC/各种换行/NBSP/零宽字符、ZWNJ/ZWJ、控制字符、重复段、逆序、缺中段、输入不变、上限、物理路径与完整凭据脱敏、XHH 图片后丢文、ZOL 三种互斥读法、隐藏 iframe 回退、ZOL iframe 重建后丢文、全部/部分图片失败，以及：

`ContentVersion → production resolve_delivery_payload → DeliveryService → BasePlatform.publish → XiaoheihePlatform/ZOLPlatform.fill_content`

链路测试仅替换浏览器 DOM 外围与最终草稿保存动作；不替换真实 `publish()`/`fill_content()`。冻结后修改活动草稿，执行仍读取旧不可变版本；假 DOM 以浏览器态返回实体反转义和零宽字符移除后的文本。

最终验证（Python 3.12.13）：

```text
pytest -q: 171 passed, 2 skipped
ruff（新增文件全规则；旧适配器聚焦 F/E9）: passed
py_compile: passed
git diff --check: passed
```

两个 skip 为既有显式 opt-in 浏览器回归。本轮没有启动浏览器、登录、写平台草稿或公开发布。
