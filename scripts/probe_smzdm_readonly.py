"""只读打开什么值得买已有草稿并输出脱敏编辑器能力结构。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
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
from platforms.smzdm import DRAFTS_URL, SmzdmPlatform


class ProbeError(RuntimeError):
    """只携带稳定错误码。"""


def _sqlite_uri(path: Path) -> str:
    return f"file:///{quote(path.as_posix().lstrip('/'), safe='/:')}?mode=ro"


def _load_account(account_id: str) -> PlatformAccount:
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
            FROM platform_accounts WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
    if row is None:
        raise ProbeError("ACCOUNT_NOT_FOUND")
    if row["platform"] != "smzdm":
        raise ProbeError("ACCOUNT_PLATFORM_MISMATCH")
    if row["status"] != "ACTIVE" or row["session_status"] != "VALID":
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


async def run_probe(account_id: str, title: str) -> dict:
    account = _load_account(account_id)
    platform = SmzdmPlatform(profile_dir=account.profile_path, strict_profile_lock=True)
    lease = AccountProfileLease(account, purpose="SMZDM_READ_ONLY_EDITOR_PROBE")
    try:
        with lease:
            await platform.initialize()
            await platform.page.goto(
                DRAFTS_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await platform.page.wait_for_selector(".draft-list", timeout=20000)
            matches = await platform.page.evaluate(
                """title => Array.from(document.querySelectorAll('.draft-list li'))
                    .map((item, index) => {
                        const titleLink = item.querySelector('a.sub-title');
                        const editLink = Array.from(item.querySelectorAll('a'))
                            .find(link => (link.innerText || '').trim() === '继续编辑');
                        let path = '';
                        try { path = new URL(editLink?.href || '', location.href).pathname; }
                        catch (_) {}
                        return {
                            index,
                            title: (titleLink?.textContent || '').trim(),
                            edit_path: path,
                        };
                    }).filter(item => item.title === title)""",
                title,
            )
            safe_matches = [
                {"index": int(item.get("index", -1)), "edit_path": item.get("edit_path", "")}
                for item in matches
                if isinstance(item, dict)
            ]
            valid = [
                item
                for item in safe_matches
                if str(item["edit_path"]).startswith("/edit/")
            ]
            if not valid:
                return {"status": "DRAFT_NOT_FOUND", "match_count": len(safe_matches)}
            await platform.page.goto(
                f"https://post.smzdm.com{valid[0]['edit_path']}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await platform.page.wait_for_selector("div.ProseMirror", timeout=20000)
            payload = await platform.page.locator("div.ProseMirror").first.evaluate(
                """root => {
                    const keyNames = node => Object.getOwnPropertyNames(node)
                        .filter(key => /pm|prose|tip|vue|react|editor/i.test(key))
                        .map(key => key.slice(0, 80)).slice(0, 40);
                    const shape = node => ({
                        tag: (node.tagName || '').toLowerCase(),
                        class_name: String(node.className || '').slice(0, 160),
                        image_count: node.querySelectorAll
                            ? node.querySelectorAll('img').length
                            : 0,
                        text_length: (node.innerText || '').length,
                        own_keys: keyNames(node),
                    });
                    const descendants = Array.from(root.querySelectorAll('*'));
                    const editor = root.editor || null;
                    const ancestors = [];
                    for (let node = root; node && ancestors.length < 8; node = node.parentElement) {
                        ancestors.push(shape(node));
                    }
                    return {
                        root: shape(root),
                        child_shapes: Array.from(root.children).slice(0, 30).map(shape),
                        tail: root.lastElementChild ? shape(root.lastElementChild) : null,
                        descendant_expandos: descendants.filter(node => keyNames(node).length)
                            .slice(0, 30).map(shape),
                        ancestors,
                        contenteditable: root.getAttribute('contenteditable') || '',
                        insert_paragraph_supported: document.queryCommandSupported
                            ? document.queryCommandSupported('insertParagraph') : false,
                        input_event_supported: typeof InputEvent === 'function',
                        editor_api: {
                            present: !!editor,
                            own_keys: editor ? keyNames(editor) : [],
                            has_commands: !!(editor && editor.commands),
                            command_names: editor && editor.commands
                                ? Object.keys(editor.commands).sort().slice(0, 120) : [],
                            has_chain: !!(editor && typeof editor.chain === 'function'),
                            has_view: !!(editor && editor.view && editor.view.state),
                        },
                    };
                }"""
            )
            location = urlsplit(platform.page.url)
            return {
                "status": "EDITOR_READY",
                "match_count": len(safe_matches),
                "location_path": location.path,
                "editor": payload,
            }
    finally:
        await platform.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="什么值得买已有草稿只读编辑器探测")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run_probe(args.account_id, args.title))
    except ProbeError as exc:
        result = {"status": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "PROBE_FAILED", "exception_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "EDITOR_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
