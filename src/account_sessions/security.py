"""账号标识脱敏与投递指纹。"""

import hashlib
import json
import re


def mask_platform_user_id(value: str | None) -> str:
    if not value:
        return "待验证"
    suffix = value[-4:] if len(value) >= 4 else value
    return f"****{suffix}"


def delivery_fingerprint(
    *, platform: str, account_id: str, title: str, body: str, mode: str
) -> str:
    canonical = json.dumps(
        {
            "platform": platform,
            "account_id": account_id,
            "title": title,
            "body": body,
            "mode": mode,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_version(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def safe_error_message(value: object) -> str:
    """错误日志只保留诊断摘要，主动遮盖常见凭据键值。"""

    text = str(value or "")[:1000]
    return re.sub(
        r"(?i)(token|cookie|pkey|authorization|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
