"""平台响应脱敏与简单 JSON Path 读取工具。"""

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_MARKERS = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "session",
    "mobile",
    "phone",
    "email",
    "nickname",
    "username",
    "avatar",
)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().casefold().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def redact_sensitive(value: Any) -> Any:
    """递归移除敏感字段；列表结构保留，便于后续排查 Schema。"""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if is_sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    return value


def sanitize_url(raw_url: str) -> str:
    """保留 URL 路径和参数名，移除所有查询值与 fragment。"""

    parsed = urlsplit(raw_url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = urlencode([(key, "") for key, _value in query_pairs])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def extract_json_path(payload: Any, path: str) -> Any:
    """读取点分隔路径，支持字典键和列表下标，不执行表达式。"""

    current = payload
    for segment in path.split("."):
        if not segment:
            raise KeyError(f"非法 JSON Path: {path!r}")
        if isinstance(current, Mapping):
            if segment not in current:
                raise KeyError(path)
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(path) from exc
            continue
        raise KeyError(path)
    return current


def payload_shape(value: Any, *, depth: int = 0, max_depth: int = 5) -> Any:
    """输出不含值的响应结构，用于安全的 Network 探测记录。"""

    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, Mapping):
        return {
            str(key): "redacted" if is_sensitive_key(key) else payload_shape(
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [] if not value else [payload_shape(value[0], depth=depth + 1, max_depth=max_depth)]
    return type(value).__name__
