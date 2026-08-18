"""小红书发布页只读 DOM 探测。

本工具只允许复用现有账号 Profile 导航到发布页并读取脱敏结构。
默认模式不执行任何业务点击；显式草稿箱模式最多点击一次唯一的“草稿箱”入口，
不点击草稿项、不登录、不输入、不选文件、不保存草稿，也不会更新账号数据库。
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
from urllib.parse import quote, urlsplit

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
CREATOR_ORIGIN = "https://creator.xiaohongshu.com"
CREATOR_PUBLISH_PATH = "/publish/publish"
BODY_EDITOR_SELECTOR = "div.tiptap.ProseMirror"
DEFAULT_MANUAL_HANDOFF_TIMEOUT_SECONDS = 300
MAX_MANUAL_HANDOFF_TIMEOUT_SECONDS = 600
MANUAL_HANDOFF_POLL_INTERVAL_SECONDS = 1
DRAFT_LIST_WAIT_TIMEOUT_SECONDS = 15
_LOGIN_PATH_MARKERS = ("/login", "/auth", "/passport")
_CHALLENGE_PATH_MARKERS = ("captcha", "challenge", "verify", "security", "risk")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:[a-z]:[\\/][^\s,;]+|"
    r"(?:cookie|token|authorization|session)\s*[:=]\s*[^\s,;]+|"
    r"https?://[^\s,;]+)"
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
    const bodyEditor = document.querySelector(
        '.tiptap.ProseMirror, [contenteditable="true"]'
    );
    const nearBodyEditor = (element) => {
        if (!bodyEditor) return false;
        let current = bodyEditor;
        for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
            if (current === element || current.contains(element)) return true;
        }
        return false;
    };
    const toolbarCandidates = Array.from(document.querySelectorAll(
        'button, [role="button"], input[type="file"], [aria-label], [title]'
    )).filter((element) => {
        const semantic = [
            element.getAttribute('aria-label'),
            element.getAttribute('title'),
            element.getAttribute('type'),
        ].filter(Boolean).join(' ');
        return /(图片|插图|上传|本地|image|photo|upload)/i.test(semantic);
    });
    const toolbar = toolbarCandidates.slice(0, 12).map((element) => ({
        tag: element.tagName.toLowerCase(),
        type: element.getAttribute('type') || '',
        role: element.getAttribute('role') || '',
        aria_label: compact(element.getAttribute('aria-label')),
        title: compact(element.getAttribute('title')),
        label_text: compact([
            element.getAttribute('aria-label'),
            element.getAttribute('title'),
        ].filter(Boolean).join(' ')),
        visible: visible(element),
        near_body_editor: nearBodyEditor(element),
    }));
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
        toolbar_candidates: toolbar,
    };
}"""


# 入口脚本只读取可见文本和最小交互属性；唯一候选才允许一次 click()。
# 刻意不读取 href/src/style/class/value，也不遍历草稿正文。
DRAFT_LIST_ENTRY_SCRIPT = r"""() => {
    const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const visible = (element) => element.getAttribute('aria-hidden') !== 'true'
        && element.getClientRects().length > 0;
    const entryPattern = /^草稿箱(?:\(\d+\))?$/;
    const candidates = Array.from(document.querySelectorAll(
        'a, button, [role="button"], [role="link"], [tabindex]'
    )).filter((element) => visible(element) && entryPattern.test(compact(element.textContent)));
    if (candidates.length !== 1) {
        return {
            status: candidates.length ? 'DRAFT_LIST_ENTRY_AMBIGUOUS' : 'DRAFT_LIST_ENTRY_MISSING',
            entry_count: candidates.length,
        };
    }
    candidates[0].click();
    return {status: 'DRAFT_LIST_ENTRY_CLICKED', entry_count: 1};
}"""


# 点击入口后只读取草稿项的标签存在性/长度、固定动作摘要和 tag/role；不读取链接、
# 资源、样式、class、value 或正文内容。草稿项的编辑入口只按可见按钮/链接短文本计数。
DRAFT_LIST_PROBE_SCRIPT = r"""() => {
    const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const visible = (element) => element.getAttribute('aria-hidden') !== 'true'
        && element.getClientRects().length > 0;
    const interactiveSelector = 'button, a, [role="button"], [role="link"]';
    const listContainers = Array.from(document.querySelectorAll(
        '[role="list"], ul, ol'
    )).filter(visible);
    const itemCandidates = Array.from(document.querySelectorAll(
        '[role="listitem"], article, li'
    )).filter(visible);
    // 只保留最内层候选，避免把整个列表容器错误统计为一项。
    const items = itemCandidates.filter((element) => !itemCandidates.some(
        (other) => other !== element && element.contains(other)
    ));
    const shortText = (element) => compact(
        element.getAttribute('aria-label')
        || element.getAttribute('title')
        || element.textContent
    );
    const labelLength = (item) => {
        const heading = item.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]');
        const label = (heading && visible(heading) ? shortText(heading) : '')
            || shortText(item.getAttribute('aria-label') || '');
        return label.length;
    };
    const safeAction = (text) => {
        const normalized = compact(text);
        if (/^继续编辑$/i.test(normalized)) return '继续编辑';
        if (/^编辑$/i.test(normalized)) return '编辑';
        if (/^删除$/i.test(normalized)) return '删除';
        if (/^打开$/i.test(normalized)) return '打开';
        if (/^查看$/i.test(normalized)) return '查看';
        if (/^continue editing$/i.test(normalized)) return 'continue editing';
        if (/^edit$/i.test(normalized)) return 'edit';
        if (/^delete$/i.test(normalized)) return 'delete';
        if (/^open$/i.test(normalized)) return 'open';
        if (/^view$/i.test(normalized)) return 'view';
        return '[redacted]';
    };
    const itemsPayload = items.map((item) => {
        const buttons = Array.from(item.querySelectorAll(interactiveSelector))
            .filter(visible)
            .map(shortText)
            .filter(Boolean)
            .slice(0, 8);
        const editEntries = buttons.filter((text) => /编辑|继续编辑|edit/i.test(text));
        const labelLengthValue = labelLength(item);
        return {
            tag: item.tagName.toLowerCase(),
            role: item.getAttribute('role') || '',
            label_present: labelLengthValue > 0,
            label_length: labelLengthValue,
            button_texts: buttons.map(safeAction),
            unique_edit_entry: editEntries.length === 1,
        };
    }).filter((item) => item.unique_edit_entry);
    return {
        status: itemsPayload.length
            ? 'DRAFT_LIST_CANDIDATES'
            : (listContainers.length ? 'DRAFT_LIST_UNRECOGNIZED' : 'DRAFT_LIST_NOT_READY'),
        list_container_count: listContainers.length,
        items: itemsPayload,
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
    raw_toolbar = payload.get("toolbar_candidates", []) if isinstance(payload, dict) else []
    toolbar: list[dict[str, Any]] = []
    if isinstance(raw_toolbar, list):
        for item in raw_toolbar:
            if not isinstance(item, dict):
                continue
            toolbar.append(
                {
                    "tag": _redact_label(item.get("tag")),
                    "type": _redact_label(item.get("type")),
                    "role": _redact_label(item.get("role")),
                    "aria_label": _redact_label(item.get("aria_label")),
                    "title": _redact_label(item.get("title")),
                    "label_text": _redact_label(item.get("label_text")),
                    "visible": bool(item.get("visible")),
                    "near_body_editor": bool(item.get("near_body_editor")),
                }
            )
    return {
        "file_input_count": len(inputs),
        "inputs": inputs,
        "toolbar_candidates": toolbar[:12],
    }


def sanitize_draft_list_payload(payload: Any) -> dict[str, Any]:
    """只保留草稿项的公开短摘要，不带正文、链接或资源字段。"""

    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            if raw_item.get("unique_edit_entry") is not True:
                continue
            raw_buttons = raw_item.get("button_texts", [])
            button_texts = (
                [_sanitize_draft_action(text) for text in raw_buttons if text][:8]
                if isinstance(raw_buttons, list)
                else []
            )
            try:
                label_length = int(raw_item.get("label_length", 0))
            except (TypeError, ValueError):
                label_length = 0
            label_length = max(0, min(label_length, 80))
            items.append(
                {
                    "tag": _redact_label(raw_item.get("tag")),
                    "role": _redact_label(raw_item.get("role")),
                    "label_present": bool(
                        raw_item.get("label_present") or raw_item.get("label")
                    ),
                    "label_length": label_length,
                    "button_texts": button_texts,
                    "unique_edit_entry": bool(raw_item.get("unique_edit_entry")),
                }
            )
    return {
        "draft_item_count": len(items),
        "draft_items": items,
        "unique_edit_entry_count": sum(
            item["unique_edit_entry"] for item in items
        ),
    }


_DRAFT_ACTION_LABELS = {
    "继续编辑",
    "编辑",
    "删除",
    "打开",
    "查看",
    "continue editing",
    "edit",
    "delete",
    "open",
    "view",
}


def _sanitize_draft_action(value: Any) -> str:
    """将按钮文本收敛为固定动作白名单，避免泄露用户内容。"""

    text = _redact_label(value)
    return text if text in _DRAFT_ACTION_LABELS else "[redacted]"


def classify_draft_entry_payload(payload: Any) -> str:
    """将入口脚本结果转换为稳定的 fail-closed 状态。"""

    if not isinstance(payload, dict):
        return "DRAFT_LIST_ENTRY_MISSING"
    if payload.get("status") == "DRAFT_LIST_ENTRY_CLICKED" and payload.get(
        "entry_count"
    ) == 1:
        return "DRAFT_LIST_ENTRY_CLICKED"
    if payload.get("entry_count"):
        return "DRAFT_LIST_ENTRY_AMBIGUOUS"
    return "DRAFT_LIST_ENTRY_MISSING"


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


def landing_status_for(payload: dict[str, Any]) -> str:
    """将固定落地页的无控件结果转换为对外安全状态。"""

    status = classify_probe_payload(payload)
    if status == "NO_BODY_IMAGE_INPUT":
        return "LANDING_NO_INPUT"
    return status


def safe_page_location(url: str) -> tuple[str, str]:
    """只提取 origin/path，绝不返回 query、fragment 或凭据。"""

    try:
        parsed = urlsplit(str(url or ""))
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "", "/"
    if not parsed.scheme or not hostname:
        return "", parsed.path or "/"
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    origin = f"{parsed.scheme.lower()}://{host}"
    if port is not None:
        origin = f"{origin}:{port}"
    return origin, parsed.path or "/"


def classify_handoff_location(origin: str, path: str) -> str:
    """分类等待期间的安全终止位置。"""

    normalized_path = str(path or "/").lower()
    if any(marker in normalized_path for marker in _LOGIN_PATH_MARKERS):
        return "LOGIN_REQUIRED"
    if any(marker in normalized_path for marker in _CHALLENGE_PATH_MARKERS):
        return "CHALLENGE"
    if origin != CREATOR_ORIGIN:
        return "UNEXPECTED_ORIGIN"
    return ""


def validate_manual_handoff_timeout(seconds: int) -> int:
    """限制人工交接等待时间，避免无界驻留。"""

    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or seconds < 1
        or seconds > MAX_MANUAL_HANDOFF_TIMEOUT_SECONDS
    ):
        raise ProbeError("HANDOFF_TIMEOUT_INVALID")
    return seconds


async def wait_for_draft_list(
    page: Any,
    *,
    timeout_seconds: int = DRAFT_LIST_WAIT_TIMEOUT_SECONDS,
    sleep: Any = asyncio.sleep,
    clock: Any = None,
) -> tuple[str, dict[str, Any]]:
    """单次点击后等待草稿项结构出现并连续两次保持稳定。"""

    timeout = validate_manual_handoff_timeout(timeout_seconds)
    now = clock or asyncio.get_running_loop().time
    deadline = now() + timeout
    last_payload: dict[str, Any] = {}
    last_status = "DRAFT_LIST_NOT_READY"
    previous_fingerprint = ""
    stable_samples = 0
    while True:
        origin, path = safe_page_location(page.url)
        if origin != CREATOR_ORIGIN or path != CREATOR_PUBLISH_PATH:
            return "UNEXPECTED_ORIGIN", {}
        try:
            raw_payload = await page.evaluate(DRAFT_LIST_PROBE_SCRIPT)
        except Exception as exc:  # noqa: BLE001
            raise ProbeError("BROWSER_CONTEXT_CLOSED") from exc
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_items = payload.get("items", [])
        safe_items = [
            item
            for item in raw_items
            if isinstance(item, dict) and item.get("unique_edit_entry") is True
        ] if isinstance(raw_items, list) else []
        if safe_items:
            safe_payload = {**payload, "items": safe_items}
            fingerprint = json.dumps(
                sanitize_draft_list_payload(safe_payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stable_samples = stable_samples + 1 if fingerprint == previous_fingerprint else 1
            previous_fingerprint = fingerprint
            last_payload = safe_payload
            if stable_samples >= 2:
                return "DRAFT_LIST_READY", safe_payload
            last_status = "DRAFT_LIST_NOT_READY"
        else:
            stable_samples = 0
            previous_fingerprint = ""
            last_payload = payload
            last_status = "DRAFT_LIST_UNRECOGNIZED" if (
                raw_items
                or payload.get("status") == "DRAFT_LIST_UNRECOGNIZED"
                or payload.get("list_container_count")
            ) else "DRAFT_LIST_NOT_READY"
        remaining = deadline - now()
        if remaining <= 0:
            return last_status, last_payload
        await sleep(min(MANUAL_HANDOFF_POLL_INTERVAL_SECONDS, remaining))


async def wait_for_manual_handoff(
    page: Any,
    *,
    timeout_seconds: int = DEFAULT_MANUAL_HANDOFF_TIMEOUT_SECONDS,
    sleep: Any = asyncio.sleep,
    clock: Any = None,
) -> str:
    """在同一受控 page 上等待已有编辑器，不执行任何业务动作。"""

    timeout = validate_manual_handoff_timeout(timeout_seconds)
    now = clock or asyncio.get_running_loop().time
    deadline = now() + timeout
    while True:
        try:
            origin, path = safe_page_location(page.url)
            location_status = classify_handoff_location(origin, path)
            if location_status:
                return location_status
            if await page.locator(BODY_EDITOR_SELECTOR).count() > 0:
                return "EDITOR_READY"
        except ProbeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProbeError("BROWSER_CONTEXT_CLOSED") from exc
        remaining = deadline - now()
        if remaining <= 0:
            return "MANUAL_HANDOFF_TIMEOUT"
        await sleep(min(MANUAL_HANDOFF_POLL_INTERVAL_SECONDS, remaining))


_MANUAL_ACTION_REQUIRED_STATUSES = {
    "LANDING_NO_INPUT",
    "MANUAL_HANDOFF_TIMEOUT",
    "LOGIN_REQUIRED",
    "CHALLENGE",
    "DRAFT_LIST_ENTRY_AMBIGUOUS",
    "DRAFT_LIST_ENTRY_MISSING",
    "DRAFT_LIST_NOT_READY",
    "DRAFT_LIST_UNRECOGNIZED",
}


def build_probe_result(
    status: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一构造不含 URL/query/敏感 DOM 内容的结果。"""

    safe_payload = sanitize_probe_payload(payload or {})
    manual_action = manual_action_for(status)
    return {
        "status": status,
        **safe_payload,
        "manual_action_required": status in _MANUAL_ACTION_REQUIRED_STATUSES,
        "manual_action": manual_action,
    }


def build_draft_list_result(
    status: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造草稿箱专用结果，并只输出白名单摘要。"""

    safe_payload = sanitize_draft_list_payload(payload or {})
    if status == "DRAFT_LIST_READY" and safe_payload["draft_item_count"] == 0:
        status = "DRAFT_LIST_UNRECOGNIZED"
    result = build_probe_result(status)
    result.update(safe_payload)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小红书发布页只读 DOM 探测")
    parser.add_argument(
        "--account-id",
        required=True,
        help="已存在且 session_status=VALID 的小红书账号 ID",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--manual-handoff",
        action="store_true",
        help="在同一受控 page 上等待用户导航到已有草稿编辑器",
    )
    mode.add_argument(
        "--probe-draft-list",
        action="store_true",
        help="只读打开唯一草稿箱入口并探测草稿项结构，不点击草稿项",
    )
    parser.add_argument(
        "--handoff-timeout-seconds",
        type=int,
        default=DEFAULT_MANUAL_HANDOFF_TIMEOUT_SECONDS,
        help="人工交接等待秒数（1-600，默认 300）",
    )
    args = parser.parse_args()
    if not 1 <= args.handoff_timeout_seconds <= MAX_MANUAL_HANDOFF_TIMEOUT_SECONDS:
        parser.error(
            "handoff timeout must be between 1 and "
            f"{MAX_MANUAL_HANDOFF_TIMEOUT_SECONDS} seconds"
        )
    return args


def manual_action_for(status: str) -> str:
    if status in {"NO_BODY_IMAGE_INPUT", "LANDING_NO_INPUT"}:
        return (
            "当前仅探测了固定落地页；普通浏览器中的手动草稿不会传递。"
            "需显式使用 --manual-handoff 在同一受控页面等待已有草稿；工具不会自动创建新的创作"
        )
    if status == "BODY_INPUT_WITHOUT_IMAGE_ACCEPT":
        return "正文输入控件缺少 image accept；不冻结 selector，等待真实证据"
    if status == "EDITOR_READY":
        return (
            "同一受控页面已进入已有编辑器；当前仅读取脱敏 DOM，"
            "未点击图片工具栏或打开 file chooser"
        )
    if status == "DRAFT_LIST_READY":
        return "已读取草稿项脱敏摘要；未点击草稿项、未创建新创作、未输入或保存"
    if status == "DRAFT_LIST_ENTRY_MISSING":
        return "未找到唯一可见草稿箱入口；安全停止，不创建新创作或重试"
    if status == "DRAFT_LIST_ENTRY_AMBIGUOUS":
        return "发现多个可见草稿箱入口；安全停止，不猜测或重试"
    if status == "DRAFT_LIST_NOT_READY":
        return "草稿列表未在限时内稳定出现；安全停止，不点击草稿项或重试"
    if status == "DRAFT_LIST_UNRECOGNIZED":
        return "草稿列表结构无法安全识别；安全停止，不猜测或重试"
    if status == "MANUAL_HANDOFF_TIMEOUT":
        return "同一受控页面未在超时内进入已有草稿；未创建、上传或保存"
    if status == "LOGIN_REQUIRED":
        return "已检测到登录状态；安全停止，不自动登录或重试"
    if status == "CHALLENGE":
        return "已检测到验证码或安全挑战；安全停止，不自动处理或重试"
    if status == "UNEXPECTED_ORIGIN":
        return "受控页面离开允许的创作者域名；安全停止，不跟随或重试"
    return ""


async def run_probe(
    account_id: str,
    *,
    manual_handoff: bool = False,
    probe_draft_list: bool = False,
    handoff_timeout_seconds: int = DEFAULT_MANUAL_HANDOFF_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """复用现有 Profile 租约执行一次只读页面探测。"""

    if manual_handoff and probe_draft_list:
        raise ProbeError("PROBE_MODE_CONFLICT")
    if manual_handoff:
        validate_manual_handoff_timeout(handoff_timeout_seconds)
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
            origin, path = safe_page_location(platform.page.url)
            location_status = classify_handoff_location(origin, path)
            if location_status:
                return build_probe_result(location_status)
            if probe_draft_list:
                if origin != CREATOR_ORIGIN or path != CREATOR_PUBLISH_PATH:
                    return build_probe_result("UNEXPECTED_ORIGIN")
                entry_payload = await platform.page.evaluate(DRAFT_LIST_ENTRY_SCRIPT)
                entry_status = classify_draft_entry_payload(entry_payload)
                if entry_status != "DRAFT_LIST_ENTRY_CLICKED":
                    return build_draft_list_result(entry_status, entry_payload)
                draft_status, draft_payload = await wait_for_draft_list(platform.page)
                if draft_status == "UNEXPECTED_ORIGIN":
                    return build_probe_result(draft_status)
                return build_draft_list_result(draft_status, draft_payload)
            if manual_handoff:
                handoff_status = await wait_for_manual_handoff(
                    platform.page,
                    timeout_seconds=handoff_timeout_seconds,
                )
                if handoff_status != "EDITOR_READY":
                    return build_probe_result(handoff_status)
            raw_payload = await platform.page.evaluate(DOM_PROBE_SCRIPT)
            payload = sanitize_probe_payload(raw_payload)
            status = "EDITOR_READY" if manual_handoff else landing_status_for(payload)
            return build_probe_result(status, payload)
    finally:
        await platform.cleanup()


async def _main() -> int:
    args = parse_args()
    try:
        result = await run_probe(
            args.account_id,
            manual_handoff=args.manual_handoff,
            probe_draft_list=args.probe_draft_list,
            handoff_timeout_seconds=args.handoff_timeout_seconds,
        )
    except ProbeError as exc:
        result = build_probe_result(exc.code)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).upper()
        if "PROFILE_IN_USE" in message or "SINGLETON" in message:
            code = "PROFILE_IN_USE"
        elif "TIMEOUT" in message:
            code = "NAVIGATION_TIMEOUT"
        elif "LOGIN" in message or "AUTH" in message:
            code = "LOGIN_REQUIRED"
        elif any(marker in message for marker in ("CAPTCHA", "CHALLENGE", "SECURITY")):
            code = "CHALLENGE"
        elif any(marker in message for marker in ("CLOSED", "TARGETCLOSED", "BROWSER")):
            code = "BROWSER_CONTEXT_CLOSED"
        else:
            code = "PROBE_FAILED"
        result = build_probe_result(code)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
