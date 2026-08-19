"""百家号账号与内容管理页的只读结构探测。

本工具只复用已登记的 VALID 账号 Profile，导航到百家号内容管理页并读取
脱敏后的控件结构。它不会输入、选择文件、创建或保存草稿，也不会点击发布。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for candidate in (PROJECT_ROOT, SRC_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# ruff: noqa: E402
from account_sessions.leases import AccountProfileLease
from account_sessions.models import PlatformAccount
from account_sessions.runtime_paths import default_database_path
from platforms.baijiahao import BaijiahaoPlatform

MANAGE_URL = "https://baijiahao.baidu.com/builder/rc/manage"
ALLOWED_ORIGIN = "https://baijiahao.baidu.com"
IDENTITY_KEY_MARKERS = (
    "account",
    "author",
    "baijiahao",
    "creator",
    "name",
    "nick",
    "profile",
    "uid",
    "user",
)


class ProbeError(RuntimeError):
    """只携带稳定错误码的只读探测异常。"""


def _sqlite_uri(path: Path) -> str:
    return f"file:///{quote(path.as_posix().lstrip('/'), safe='/:')}?mode=ro"


def _load_account(account_id: str, *, allow_error_state: bool = False) -> PlatformAccount:
    path = default_database_path()
    if not path.is_file():
        raise ProbeError("ACCOUNT_DATABASE_NOT_FOUND")
    with sqlite3.connect(_sqlite_uri(path), uri=True) as connection:
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
    if row is None:
        raise ProbeError("ACCOUNT_NOT_FOUND")
    if row["platform"] != "baijiahao":
        raise ProbeError("ACCOUNT_PLATFORM_MISMATCH")
    if row["status"] != "ACTIVE" or (
        row["session_status"] != "VALID" and not allow_error_state
    ):
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


def _safe_location(value: str) -> dict[str, str]:
    parsed = urlsplit(str(value or ""))
    origin = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}"
    return {"origin": origin, "path": parsed.path or "/"}


PROBE_SCRIPT = r"""() => {
    const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    const visible = (element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const actionMarkers = [
        ['继续编辑', 'CONTINUE_EDIT'], ['保存草稿', 'SAVE_DRAFT'],
        ['存草稿', 'SAVE_DRAFT'], ['选择封面', 'COVER_PICKER'],
        ['内容管理', 'CONTENT_MANAGE'], ['草稿箱', 'DRAFT_TAB'],
        ['作品管理', 'WORKS_MANAGE'], ['草稿', 'DRAFT_TAB'],
        ['封面', 'COVER'], ['图片', 'IMAGE'],
        ['图文', 'ARTICLE'], ['修改', 'MODIFY'], ['编辑', 'EDIT'],
        ['发布', 'PUBLISH'],
    ];
    const allowedMarker = (value) => {
        const text = compact(value);
        const found = actionMarkers.find(([label]) => text.includes(label));
        return found ? found[1] : '';
    };
    const walker = document.createTreeWalker(
        document.body || document.documentElement,
        NodeFilter.SHOW_TEXT,
    );
    const textParents = [];
    let node = walker.nextNode();
    while (node) {
        const parent = node.parentElement;
        if (parent && visible(parent) && allowedMarker(node.nodeValue)) {
            textParents.push(parent);
        }
        node = walker.nextNode();
    }
    const actions = Array.from(new Set(textParents)).map((leaf) => {
        const element = leaf.closest(
            'button, a, [role="button"], [role="link"], [tabindex], li'
        ) || leaf;
        const marker = allowedMarker(leaf.textContent);
        let path = '';
        if (element.href) {
            try {
                const parsed = new URL(element.href, location.href);
                if (parsed.origin === location.origin) {
                    path = parsed.pathname.replace(
                        /\b(?:[0-9a-f]{8,}|\d{6,})\b/gi,
                        '{id}',
                    );
                }
            } catch (_) { /* no-op */ }
        }
        return {
            tag: element.tagName.toLowerCase(),
            role: element.getAttribute('role') || '',
            marker,
            path,
            visible: true,
        };
    }).filter(Boolean).slice(0, 40);
    const inputs = Array.from(document.querySelectorAll('input')).map((element) => ({
        type: element.getAttribute('type') || '',
        accept: element.getAttribute('accept') || '',
        multiple: element.hasAttribute('multiple'),
        visible: visible(element),
        aria_label: compact(element.getAttribute('aria-label')),
        placeholder: compact(element.getAttribute('placeholder')),
    })).filter((item) => item.type === 'file').slice(0, 20);
    const editors = Array.from(document.querySelectorAll('[contenteditable="true"]')).map(
        (element) => ({
            tag: element.tagName.toLowerCase(),
            visible: visible(element),
            aria_label: compact(element.getAttribute('aria-label')),
            placeholder: compact(
                element.getAttribute('placeholder') || element.getAttribute('data-placeholder')
            ),
        })
    ).slice(0, 20);
    const toolbarControls = Array.from(document.querySelectorAll(
        'button, [role="button"], [tabindex]'
    )).filter(visible).map((element) => {
        const rect = element.getBoundingClientRect();
        const svg = element.querySelector('svg');
        return {
            tag: element.tagName.toLowerCase(),
            class_name: compact(
                typeof element.className === 'string' ? element.className : ''
            ),
            aria_label: compact(element.getAttribute('aria-label')),
            title: compact(element.getAttribute('title')),
            tooltip: compact(
                element.getAttribute('data-tooltip')
                || element.getAttribute('data-title')
            ),
            marker: allowedMarker(element.innerText || element.textContent),
            has_svg: Boolean(svg),
            svg_class: compact(
                svg && typeof svg.className.baseVal === 'string'
                    ? svg.className.baseVal : ''
            ),
            x: Math.round(rect.x),
            y: Math.round(rect.y),
        };
    }).filter((item) => item.y < 220).slice(0, 40);
    const spatialControls = Array.from(document.querySelectorAll('body *')).filter(
        (element) => {
            if (!visible(element)) return false;
            const rect = element.getBoundingClientRect();
            return rect.y >= 70 && rect.y <= 190 && rect.x < 950
                && (element.querySelector('svg') || element.matches('svg, i'));
        }
    ).map((element) => {
        const rect = element.getBoundingClientRect();
        return {
            tag: element.tagName.toLowerCase(),
            class_name: compact(
                typeof element.className === 'string' ? element.className : ''
            ),
            aria_label: compact(element.getAttribute('aria-label')),
            title: compact(element.getAttribute('title')),
            marker: allowedMarker(element.innerText || element.textContent),
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
        };
    }).slice(0, 80);
    const imageIconCandidates = Array.from(document.querySelectorAll('[class]')).filter(
        (element) => visible(element) && /(?:tupian|image|picture)/i.test(
            typeof element.className === 'string' ? element.className : ''
        )
    ).map((element) => {
        const rect = element.getBoundingClientRect();
        return {
            tag: element.tagName.toLowerCase(),
            class_name: compact(element.className),
            parent_class_name: compact(
                element.parentElement
                && typeof element.parentElement.className === 'string'
                    ? element.parentElement.className : ''
            ),
            x: Math.round(rect.x),
            y: Math.round(rect.y),
        };
    }).slice(0, 30);
    return {
        actions,
        inputs,
        editors,
        toolbar_controls: toolbarControls,
        spatial_controls: spatialControls,
        image_icon_candidates: imageIconCandidates,
        document_state: document.readyState,
        body_child_count: document.body ? document.body.children.length : 0,
        body_text_length: document.body ? compact(document.body.innerText).length : 0,
        script_count: document.scripts.length,
        hash_state: location.hash ? 'present' : 'absent',
    };
}"""

CLICK_MARKER_SCRIPT = r"""(marker) => {
    const labels = {
        CONTENT_MANAGE: '内容管理',
        WORKS_MANAGE: '作品管理',
        DRAFT_TAB: '草稿',
    };
    const expected = labels[marker];
    if (!expected) return {status: 'MARKER_NOT_ALLOWED', count: 0};
    const compact = (value) => String(value || '').replace(/\s+/g, '').trim();
    const visible = (element) => element.getClientRects().length > 0;
    const walker = document.createTreeWalker(
        document.body || document.documentElement,
        NodeFilter.SHOW_TEXT,
    );
    const leaves = [];
    let node = walker.nextNode();
    while (node) {
        if (node.parentElement && visible(node.parentElement)
                && compact(node.nodeValue) === expected) {
            leaves.push(node.parentElement);
        }
        node = walker.nextNode();
    }
    const targets = Array.from(new Set(leaves.map((leaf) => leaf.closest(
        'button, a, [role="button"], [role="link"], [tabindex], li'
    ) || leaf))).filter(visible);
    if (targets.length !== 1) {
        return {
            status: targets.length ? 'MARKER_AMBIGUOUS' : 'MARKER_MISSING',
            count: targets.length,
        };
    }
    targets[0].click();
    return {status: 'MARKER_CLICKED', count: 1};
}"""


def _sanitize(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "actions": [],
            "inputs": [],
            "editors": [],
            "toolbar_controls": [],
            "spatial_controls": [],
            "image_icon_candidates": [],
            "document_state": "unknown",
            "body_child_count": 0,
            "body_text_length": 0,
            "script_count": 0,
            "hash_state": "unknown",
        }
    return {
        "actions": payload.get("actions", [])[:40]
        if isinstance(payload.get("actions"), list)
        else [],
        "inputs": payload.get("inputs", [])[:20]
        if isinstance(payload.get("inputs"), list)
        else [],
        "editors": payload.get("editors", [])[:20]
        if isinstance(payload.get("editors"), list)
        else [],
        "toolbar_controls": payload.get("toolbar_controls", [])[:40]
        if isinstance(payload.get("toolbar_controls"), list)
        else [],
        "spatial_controls": payload.get("spatial_controls", [])[:80]
        if isinstance(payload.get("spatial_controls"), list)
        else [],
        "image_icon_candidates": payload.get("image_icon_candidates", [])[:30]
        if isinstance(payload.get("image_icon_candidates"), list)
        else [],
        "document_state": str(payload.get("document_state") or "unknown"),
        "body_child_count": int(payload.get("body_child_count") or 0),
        "body_text_length": int(payload.get("body_text_length") or 0),
        "script_count": int(payload.get("script_count") or 0),
        "hash_state": str(payload.get("hash_state") or "unknown"),
    }


def _identity_shape(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 8 or len(candidates) >= 120:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                clean_key = str(key)[:80]
                child_path = f"{path}.{clean_key}" if path else clean_key
                lowered = clean_key.lower()
                if any(marker in lowered for marker in IDENTITY_KEY_MARKERS):
                    candidates.append(
                        {
                            "path": child_path[:240],
                            "type": type(item).__name__,
                            "length": len(item) if isinstance(item, (str, list, dict)) else 0,
                        }
                    )
                walk(item, child_path, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                walk(item, f"{path}[{index}]", depth + 1)

    walk(payload, "", 0)
    return candidates


def _matching_value_paths(payload: Any, expected: object) -> list[str]:
    needle = str(expected or "").strip()
    if not needle:
        return []
    matches: list[str] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 8 or len(matches) >= 20:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(item, child_path, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:40]):
                walk(item, f"{path}[{index}]", depth + 1)
        elif str(value or "").strip() == needle:
            matches.append(path[:240])

    walk(payload, "", 0)
    return matches


async def run_probe(
    account_id: str,
    *,
    click_marker: str | None = None,
    screenshot: bool = False,
    editor: bool = False,
    open_editor_menu: str | None = None,
    identity_structure: bool = False,
) -> dict[str, Any]:
    account = _load_account(account_id, allow_error_state=identity_structure)
    platform = BaijiahaoPlatform(
        profile_dir=account.profile_path,
        strict_profile_lock=True,
    )
    lease = AccountProfileLease(account, purpose="BAIJIAHAO_READ_ONLY_PROBE")
    identity_responses: list[dict[str, Any]] = []

    async def on_response(response) -> None:
        if not identity_structure or len(identity_responses) >= 80:
            return
        try:
            parsed = urlsplit(response.url)
            if parsed.hostname not in {"baijiahao.baidu.com", "baidu.com"}:
                return
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            payload = await response.json()
            id_matches = _matching_value_paths(payload, account.platform_user_id)
            name_matches = _matching_value_paths(payload, account.display_name)
            if parsed.path == "/user-ui/cms/settingInfo":
                data = payload.get("data") if isinstance(payload, dict) else None
                data_keys = [
                    {
                        "key": str(key)[:80],
                        "type": type(item).__name__,
                        "length": len(item)
                        if isinstance(item, (str, list, dict))
                        else 0,
                    }
                    for key, item in (data.items() if isinstance(data, dict) else [])
                ]
                identity_responses.append(
                    {
                        "path": parsed.path[:240],
                        "method": response.request.method,
                        "status": int(response.status),
                        "data_keys": data_keys[:120],
                        "stored_id_match_paths": id_matches,
                        "stored_name_match_paths": name_matches,
                    }
                )
            elif id_matches or name_matches:
                identity_responses.append(
                    {
                        "path": parsed.path[:240],
                        "method": response.request.method,
                        "status": int(response.status),
                        "stored_id_match_paths": id_matches,
                        "stored_name_match_paths": name_matches,
                    }
                )
        except Exception:  # noqa: BLE001
            return

    try:
        with lease:
            await platform.initialize()
            if identity_structure:
                platform.page.on("response", on_response)
            cookie_signal_before = await platform._has_session_cookie_signal()
            login_valid = await platform.check_login()
            cookie_signal_after = await platform._has_session_cookie_signal()
            if not login_valid:
                return {
                    "status": "LOGIN_REQUIRED",
                    "cookie_signal_before": cookie_signal_before,
                    "cookie_signal_after": cookie_signal_after,
                    "login_error_code": str(platform.last_login_error).split(":", 1)[0],
                    "identity_responses": identity_responses,
                }
            if editor:
                await platform.navigate_to_editor()
            else:
                await platform.page.goto(
                    MANAGE_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            await asyncio.sleep(5)
            location = _safe_location(platform.page.url)
            if location["origin"] != ALLOWED_ORIGIN:
                raise ProbeError("UNEXPECTED_ORIGIN")
            click_result: dict[str, Any] | None = None
            if click_marker:
                raw_click = await platform.page.evaluate(
                    CLICK_MARKER_SCRIPT,
                    click_marker,
                )
                click_result = raw_click if isinstance(raw_click, dict) else {}
                if click_result.get("status") == "MARKER_CLICKED":
                    await asyncio.sleep(5)
            if open_editor_menu:
                selectors = {
                    "INSERT": ".edui-for-bjhInsertionDrawer",
                    "FORMAT": ".edui-for-customfontsize",
                    "IMAGE": ".edui-for-insertimage",
                }
                menu = platform.page.locator(selectors[open_editor_menu] + ":visible")
                if await menu.count() != 1:
                    raise ProbeError("EDITOR_MENU_AMBIGUOUS")
                await menu.click()
                await asyncio.sleep(1)
            payload = _sanitize(await platform.page.evaluate(PROBE_SCRIPT))
            frames = []
            for frame in platform.page.frames:
                frame_payload: dict[str, Any] = {
                    "location": _safe_location(frame.url),
                }
                try:
                    frame_payload["structure"] = _sanitize(
                        await frame.evaluate(PROBE_SCRIPT)
                    )
                except Exception:  # noqa: BLE001
                    frame_payload["structure"] = {"status": "UNREADABLE"}
                frames.append(frame_payload)
            screenshot_path = ""
            if screenshot:
                target = Path(tempfile.gettempdir()) / "articleops-baijiahao-probe.png"
                await platform.page.screenshot(path=str(target), full_page=False)
                screenshot_path = str(target.resolve())
            return {
                "status": "OK",
                "location": location,
                "frame_locations": frames,
                "click_result": click_result,
                "screenshot_path": screenshot_path,
                "identity_responses": identity_responses,
                "cookie_signal_before": cookie_signal_before,
                "cookie_signal_after": cookie_signal_after,
                **payload,
            }
    finally:
        if identity_structure and platform.page is not None:
            try:
                platform.page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass
        await platform.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="百家号只读页面结构探测")
    parser.add_argument("--account-id", required=True)
    parser.add_argument(
        "--click-marker",
        choices=("CONTENT_MANAGE", "WORKS_MANAGE", "DRAFT_TAB"),
        help="仅点击一个唯一的只读导航入口；不允许其他动作",
    )
    parser.add_argument(
        "--open-editor-menu",
        choices=("INSERT", "FORMAT", "IMAGE"),
        help="只展开一个编辑器菜单，不选择菜单项",
    )
    parser.add_argument(
        "--editor",
        action="store_true",
        help="只读打开新建图文编辑器并读取结构，不输入或保存",
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="将当前视口截图写入系统临时目录，仅供本地人工核对",
    )
    parser.add_argument(
        "--identity-structure",
        action="store_true",
        help="仅记录同源 JSON 的候选键名、类型与长度，不记录字段值",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    try:
        result = await run_probe(
            args.account_id,
            click_marker=args.click_marker,
            screenshot=args.screenshot,
            editor=args.editor,
            open_editor_menu=args.open_editor_menu,
            identity_structure=args.identity_structure,
        )
    except ProbeError as exc:
        result = {"status": str(exc)}
    except Exception as exc:  # noqa: BLE001
        message = str(exc).upper()
        if "PROFILE" in message or "SINGLETON" in message:
            status = "PROFILE_IN_USE"
        elif "LOGIN" in message or "AUTH" in message:
            status = "LOGIN_REQUIRED"
        elif "TIMEOUT" in message:
            status = "NAVIGATION_TIMEOUT"
        else:
            status = "PROBE_FAILED"
        result = {
            "status": status,
            "exception_type": type(exc).__name__,
            "error_code": str(getattr(exc, "error_code", "") or "")[:80],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
