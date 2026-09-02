"""账号标识脱敏与投递指纹。"""

import hashlib
import json
import re
from collections.abc import Mapping


def mask_platform_user_id(value: str | None) -> str:
    if not value:
        return "待验证"
    suffix = value[-4:] if len(value) >= 4 else value
    return f"****{suffix}"


def canonical_platform_selection(selection: Mapping[str, object] | None) -> str | None:
    """Return deterministic JSON for the bounded platform selection map."""

    if selection is None:
        return None
    return json.dumps(
        dict(selection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def platform_selection_hash(selection: Mapping[str, object] | None) -> str | None:
    """Hash a selection without conflating it with the immutable content hash."""

    canonical = canonical_platform_selection(selection)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def delivery_fingerprint(
    *,
    platform: str,
    account_id: str,
    title: str,
    body: str,
    mode: str,
    platform_selection: Mapping[str, object] | None = None,
) -> str:
    material = {
        "platform": platform,
        "account_id": account_id,
        "title": title,
        "body": body,
        "mode": mode,
    }
    canonical_selection = canonical_platform_selection(platform_selection)
    if canonical_selection is not None:
        # Store the parsed canonical map so key order cannot alter the request
        # or confirmation fingerprint.
        material["platform_selection"] = json.loads(canonical_selection)
    canonical = json.dumps(
        material,
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
