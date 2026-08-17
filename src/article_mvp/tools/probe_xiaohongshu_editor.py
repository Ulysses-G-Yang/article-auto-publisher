"""小红书发布页只读 DOM 探测。

本工具只允许复用现有账号 Profile 导航到发布页并读取文件控件的脱敏结构。
它不登录、不点击、不输入、不选文件、不保存草稿，也不会更新账号数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# ruff: noqa: E402
from account_sessions.leases import AccountProfileLease
from account_sessions.models import PlatformAccount
from account_sessions.runtime_paths import default_database_path
from platforms.xiaohongshu import XiaohongshuPlatform

CREATOR_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
_SENSITIVE_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:[a-z]:[\\/][^\s,;]+|"
    r"(?:cookie|token|authorization|session)\s*[:=]\s*[^\s,;]+)"
)


class ProbeError(RuntimeError):
    """可安全展示给操作者的探测错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# 只读取下列属性；刻意不读取 input.value、outerHTML、请求、Cookie 或响应正文。
DOM_PROBE_SCRIPT = r"""() => {
    const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const visible = (element) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.opacity !== '0'
            && rect.width > 0
            && rect.height > 0;
    };
    const semanticText = (element) => [
        element.getAttribute('aria-label'),
        element.getAttribute('title'),
        element.getAttribute('placeholder'),
        element.getAttribute('data-placeholder'),
        element.getAttribute('data-testid'),
        typeof element.className === 'string' ? element.className : '',
        element.id,
    ].filter(Boolean).join(' ');
    const hasCoverMarker = (element) => {
        let current = element;
        for (let depth = 0; current && depth < 9; depth += 1, current = current.parentElement) {
            if (/(?:封面|\\bcover\\b)/i.test(semanticText(current))) return true;
        }
        return false;
    };
    const hasBodyEditor = (element) => {
        let current = element;
        for (let depth = 0; current && depth < 9; depth += 1, current = current.parentElement) {
            const currentText = semanticText(current);
            if (/(?:封面|\\bcover\\b)/i.test(currentText)) return false;
            if (current.matches && current.matches(
                '.tiptap.ProseMirror, [contenteditable="true"]'
            )) {
                return true;
            }
            if (current.querySelector && current.querySelector(
                '.tiptap.ProseMirror, [contenteditable="true"]'
            )) {
                return true;
            }
        }
        return false;
    };
    const neighboringLabels = (element) => {
        const labels = [];
        const add = (value) => {
            const text = compact(value);
            if (text && !labels.includes(text)) labels.push(text);
        };
        let current = element;
        for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
            if (['LABEL', 'LEGEND'].includes(current.tagName)) add(current.textContent);
            for (const attribute of ['aria-label', 'title', 'placeholder', 'data-placeholder']) {
                add(current.getAttribute(attribute));
            }
            for (const sibling of [current.previousElementSibling, current.nextElementSibling]) {
                if (sibling && ['LABEL', 'LEGEND'].includes(sibling.tagName)) {
                    add(sibling.textContent);
                }
                if (sibling) add(sibling.getAttribute('aria-label'));
            }
        }
        return labels.slice(0, 6);
    };
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
    return {
        inputs: inputs.map((element) => ({
            type: element.getAttribute('type') || '',
            accept: element.getAttribute('accept') || '',
            multiple: element.hasAttribute('multiple'),
            visible: visible(element),
            in_body_editor: hasBodyEditor(element) && !hasCoverMarker(element),
            neighbor_labels: neighboringLabels(element),
        })),
    };
}"""


def _read_only_sqlite_uri(path: Path) -> str:
    """构造 SQLite mode=ro URI，避免探测打开不存在的数据库并创建文件。"""

    return f"file:///{quote(path.as_posix().lstrip('/'), safe='/:')}?mode=ro"


def _load_account(account_id: str) -> PlatformAccount:
    database_path = default_database_path()
    if not database_path.is_file():
        raise ProbeError("ACCOUNT_DATABASE_NOT_FOUND")
    try:
        with sqlite3.connect(_read_only_sqlite_uri(database_path), uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT account_id, platform, platform_user_id, display_name,
                       profile_path, is_legacy_profile, status, session_status,
                       persist_login
                FROM platform_accounts
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ProbeError("ACCOUNT_DATABASE_READ_FAILED") from exc
    if row is None:
        raise ProbeError("ACCOUNT_NOT_FOUND")
    if row["platform"] != "xiaohongshu":
        raise ProbeError("ACCOUNT_PLATFORM_MISMATCH")
    if row["status"] != "ACTIVE":
        raise ProbeError("ACCOUNT_NOT_ACTIVE")
    if row["session_status"] != "VALID":
        raise ProbeError("LOGIN_REQUIRED")
    return PlatformAccount(
        account_id=str(row["account_id"]),
        platform=str(row["platform"]),
        platform_user_id=row["platform_user_id"],
        display_name=str(row["display_name"]),
        profile_path=str(row["profile_path"]),
        is_legacy_profile=bool(row["is_legacy_profile"]),
        status=str(row["status"]),
        session_status=str(row["session_status"]),
        persist_login=bool(row["persist_login"]),
    )


def _redact_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:80]
    return _SENSITIVE_VALUE_RE.sub("<redacted>", text)


def sanitize_probe_payload(payload: Any) -> dict[str, Any]:
    """只保留 DOM 探测允许输出的字段，并再次做敏感值脱敏。"""

    raw_inputs = payload.get("inputs", []) if isinstance(payload, dict) else []
    inputs: list[dict[str, Any]] = []
    if isinstance(raw_inputs, list):
        for item in raw_inputs:
            if not isinstance(item, dict):
                continue
            labels = item.get("neighbor_labels", [])
            inputs.append(
                {
                    "type": _redact_label(item.get("type")),
                    "accept": _redact_label(item.get("accept")),
                    "multiple": bool(item.get("multiple")),
                    "visible": bool(item.get("visible")),
                    "in_body_editor": bool(item.get("in_body_editor")),
                    "neighbor_labels": [
                        _redact_label(label) for label in labels if label
                    ][:6]
                    if isinstance(labels, list)
                    else [],
                }
            )
    return {"file_input_count": len(inputs), "inputs": inputs}


def classify_probe_payload(payload: dict[str, Any]) -> str:
    """给出下一步安全状态；不返回可直接执行的 selector。"""

    inputs = payload.get("inputs", [])
    body_inputs = [item for item in inputs if item.get("in_body_editor")]
    image_inputs = [
        item for item in body_inputs if "image" in str(item.get("accept") or "").lower()
    ]
    if image_inputs:
        return "BODY_IMAGE_INPUT_CANDIDATE"
    if body_inputs:
        return "BODY_INPUT_WITHOUT_IMAGE_ACCEPT"
    return "NO_BODY_IMAGE_INPUT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小红书发布页只读 DOM 探测")
    parser.add_argument(
        "--account-id",
        required=True,
        help="已存在且 session_status=VALID 的小红书账号 ID",
    )
    return parser.parse_args()


async def run_probe(account_id: str) -> dict[str, Any]:
    """复用现有 Profile 租约执行一次只读页面探测。"""

    account = _load_account(account_id)
    lease = AccountProfileLease(account, purpose="XHS_DOM_PROBE")
    platform = XiaohongshuPlatform(
        profile_dir=account.profile_path,
        strict_profile_lock=True,
    )
    try:
        with lease:
            await platform.initialize()
            if platform.page is None:
                raise ProbeError("BROWSER_PAGE_NOT_AVAILABLE")
            await platform.page.goto(
                CREATOR_PUBLISH_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            raw_payload = await platform.page.evaluate(DOM_PROBE_SCRIPT)
            payload = sanitize_probe_payload(raw_payload)
            return {
                "status": classify_probe_payload(payload),
                **payload,
                "manual_action_required": classify_probe_payload(payload)
                == "NO_BODY_IMAGE_INPUT",
            }
    finally:
        await platform.cleanup()


async def _main() -> int:
    args = parse_args()
    try:
        result = await run_probe(args.account_id)
    except ProbeError as exc:
        result = {
            "status": exc.code,
            "file_input_count": 0,
            "inputs": [],
            "manual_action_required": exc.code in {"LOGIN_REQUIRED", "NO_BODY_IMAGE_INPUT"},
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc).upper()
        if "PROFILE_IN_USE" in message or "SINGLETON" in message:
            code = "PROFILE_IN_USE"
        elif "TIMEOUT" in message:
            code = "NAVIGATION_TIMEOUT"
        else:
            code = "PROBE_FAILED"
        result = {
            "status": code,
            "file_input_count": 0,
            "inputs": [],
            "manual_action_required": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
