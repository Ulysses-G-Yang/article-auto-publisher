"""只读验证微博账号，并检查已有头条文章草稿的编辑器结构。

本工具不创建草稿、不输入文字、不选择文件、不保存、不发布。若当前账号没有
可定位的已有草稿，直接返回 ``NO_EXISTING_DRAFT``，不得点击「写文章」。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for candidate in (PROJECT_ROOT, SRC_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# ruff: noqa: E402
from account_sessions.leases import AccountProfileLease
from account_sessions.models import PlatformAccount
from account_sessions.runtime_paths import default_database_path
from platforms.weibo import BODY_SELECTOR, WeiboPlatform

DRAFT_LIST_URL = "https://card.weibo.com/article/v5/editor#/draft"
BODY_DOM_SELECTOR = BODY_SELECTOR.removesuffix(":visible")


class ProbeError(RuntimeError):
    """只携带稳定错误码的探测异常。"""


def _sqlite_uri(path: Path) -> str:
    return f"file:///{quote(path.as_posix().lstrip('/'), safe='/:')}?mode=ro"


def _load_only_account() -> PlatformAccount:
    path = default_database_path()
    if not path.is_file():
        raise ProbeError("ACCOUNT_DATABASE_NOT_FOUND")
    with sqlite3.connect(_sqlite_uri(path), uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT account_id, platform, platform_user_id, display_name,
                   profile_path, is_legacy_profile, status, session_status,
                   persist_login
            FROM platform_accounts
            WHERE platform = 'weibo' AND status = 'ACTIVE'
            ORDER BY created_at
            """
        ).fetchall()
    if len(rows) != 1:
        raise ProbeError("WEIBO_ACCOUNT_NOT_UNIQUE")
    row = rows[0]
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


def _fingerprint_paths(paths: list[str]) -> str:
    return hashlib.sha256("|".join(paths).encode("utf-8")).hexdigest()


async def _identity_evidence(page, stored_id: str) -> dict:
    """只返回昵称候选及其 UID 是否匹配，不返回任何 UID 原值。"""

    raw = await page.evaluate(
        r"""() => {
            const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const anchors = Array.from(document.querySelectorAll('a[href*="/u/"]'))
                .filter(visible).slice(0, 60).map((node) => {
                    const match = (node.getAttribute('href') || '').match(/\/u\/(\d+)/);
                    const rect = node.getBoundingClientRect();
                    return {
                        uid: match ? match[1] : '',
                        text: (node.innerText || '').trim().slice(0, 80),
                        title: (node.getAttribute('title') || '').slice(0, 80),
                        aria_label: (node.getAttribute('aria-label') || '').slice(0, 80),
                        class_name: String(node.className || '').slice(0, 160),
                        parent_class: String(node.parentElement?.className || '').slice(0, 160),
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                    };
                });
            const nick_nodes = Array.from(
                document.querySelectorAll('[class*="_nick_"]')
            ).filter(visible).slice(0, 30).map((node) => ({
                text: (node.innerText || '').trim().slice(0, 80),
                class_name: String(node.className || '').slice(0, 160),
                parent_class: String(node.parentElement?.className || '').slice(0, 160),
            }));
            const configs = [];
            for (const key of ['$CONFIG', '__INITIAL_STATE__', '__WB_STATE__']) {
                const value = window[key];
                if (!value || typeof value !== 'object') continue;
                const candidates = [value, value.user, value.loginUser, value.account]
                    .filter((item) => item && typeof item === 'object');
                for (const item of candidates) {
                    configs.push({
                        source: key,
                        uid: String(item.uid || item.user_id || item.id || ''),
                        display_name: String(
                            item.screen_name || item.nickname || item.nick || item.name || ''
                        ).slice(0, 80),
                    });
                }
            }
            return {anchors, nick_nodes, configs};
        }"""
    )
    if not isinstance(raw, dict):
        return {}
    anchors = []
    for item in raw.get("anchors", []):
        if not isinstance(item, dict):
            continue
        anchors.append(
            {
                "uid_matches": str(item.pop("uid", "")) == str(stored_id or ""),
                **item,
            }
        )
    configs = []
    for item in raw.get("configs", []):
        if not isinstance(item, dict):
            continue
        configs.append(
            {
                "uid_matches": str(item.pop("uid", "")) == str(stored_id or ""),
                **item,
            }
        )
    return {
        "anchors": anchors,
        "nick_nodes": raw.get("nick_nodes", []),
        "configs": configs,
    }


async def run_probe(expected_title: str = "") -> dict:
    account = _load_only_account()
    platform = WeiboPlatform(
        profile_dir=account.profile_path,
        strict_profile_lock=True,
    )
    lease = AccountProfileLease(account, purpose="WEIBO_READ_ONLY_EDITOR_PROBE")
    try:
        with lease:
            await platform.initialize()
            if not await platform.check_login():
                return {
                    "status": "LOGIN_REQUIRED",
                    "display_name": account.display_name,
                    "error_code": str(platform.last_login_error).split(":", 1)[0],
                }
            identity = await platform.fetch_identity_payload()
            identity_evidence = await _identity_evidence(
                platform.page,
                str(account.platform_user_id or ""),
            )
            if not identity.get("ok"):
                return {
                    "status": "IDENTITY_INCOMPLETE",
                    "display_name": account.display_name,
                }
            user_id_matches = str(identity.get("user_id") or "") == str(
                account.platform_user_id or ""
            )
            display_name_matches = (
                str(identity.get("display_name") or "") == account.display_name
            )
            if not user_id_matches:
                return {
                    "status": "ACCOUNT_IDENTITY_MISMATCH",
                    "registered_display_name": account.display_name,
                    "observed_display_name": str(identity.get("display_name") or ""),
                    "user_id_matches": user_id_matches,
                    "display_name_matches": display_name_matches,
                    "identity_evidence": identity_evidence,
                }
            identity_warning = (
                None
                if display_name_matches
                else {
                    "code": "DISPLAY_NAME_CHANGED",
                    "registered_display_name": account.display_name,
                    "observed_display_name": str(identity.get("display_name") or ""),
                }
            )

            await platform.page.goto(
                DRAFT_LIST_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)
            draft_list_state = await platform.page.evaluate(
                """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden';
                    };
                    return {
                        write_candidates: Array.from(
                            document.querySelectorAll('button, a, div, span')
                        ).filter((node) => visible(node)
                            && (node.innerText || '').trim() === '写文章')
                            .slice(0, 20).map((node) => ({
                                tag: node.tagName.toLowerCase(),
                                class_name: String(node.className || '').slice(0, 180),
                                role: node.getAttribute('role') || '',
                                href: node.getAttribute('href') || '',
                                child_count: node.children.length,
                                parent_tag: node.parentElement?.tagName.toLowerCase() || '',
                                parent_class: String(
                                    node.parentElement?.className || ''
                                ).slice(0, 180),
                            })),
                        list_item_count: Array.from(
                            document.querySelectorAll('.list-item')
                        ).filter(visible).length,
                    };
                }"""
            )
            draft_url = await platform.page.evaluate(
                r"""() => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    const exact = anchors
                        .map((node) => node.href || node.getAttribute('href') || '')
                        .find((href) => /#\/draft\/\d+$/.test(href));
                    return exact || '';
                }"""
            )
            if not draft_url:
                blocked_paths: list[str] = []

                async def block_mutations(route, request) -> None:
                    method = str(request.method or "").upper()
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        blocked_paths.append(
                            "/" + str(request.url).split("/", 3)[-1].split("?", 1)[0]
                        )
                        await route.abort("blockedbyclient")
                        return
                    await route.continue_()

                await platform.page.route("**/*", block_mutations)
                try:
                    normalized_expected_title = str(expected_title or "").strip()
                    if normalized_expected_title:
                        expected_card = await platform.page.evaluate(
                            r"""(title) => {
                                const visible = (node) => {
                                    const rect = node.getBoundingClientRect();
                                    const style = getComputedStyle(node);
                                    return rect.width > 0 && rect.height > 0
                                        && style.display !== 'none'
                                        && style.visibility !== 'hidden';
                                };
                                const matches = Array.from(
                                    document.querySelectorAll('.list-item')
                                ).filter((card) => visible(card)
                                    && (card.innerText || '').split(/\r?\n/, 1)[0].trim()
                                        === title);
                                if (matches.length === 1) matches[0].click();
                                return {count: matches.length, clicked: matches.length === 1};
                            }""",
                            normalized_expected_title,
                        )
                        if expected_card.get("clicked"):
                            await asyncio.sleep(3)
                            expected_state = await platform.page.evaluate(
                                """(args) => {
                                    const title = document.querySelector(
                                        "textarea[placeholder='请输入标题']"
                                    );
                                    const body = document.querySelector(args.selector);
                                    return {
                                        title_exact: Boolean(
                                            title && title.value === args.title
                                        ),
                                        body_trimmed_length: body
                                            ? (body.innerText || '').trim().length
                                            : null,
                                        image_count: body
                                            ? body.querySelectorAll('img').length
                                            : null,
                                        h2_count: body
                                            ? body.querySelectorAll('h2').length
                                            : null,
                                        child_shape: body
                                            ? Array.from(body.children).slice(0, 80)
                                                .map((node) => ({
                                                    tag: node.tagName.toLowerCase(),
                                                    images: node.matches('img')
                                                        ? 1
                                                        : node.querySelectorAll('img').length,
                                                    text_length: (
                                                        node.innerText || node.textContent || ''
                                                    ).trim().length,
                                                }))
                                            : [],
                                        url: location.href,
                                    };
                                }""",
                                {
                                    "selector": BODY_DOM_SELECTOR,
                                    "title": normalized_expected_title,
                                },
                            )
                            return {
                                "status": "EXPECTED_DRAFT_FOUND_READ_ONLY",
                                "display_name": account.display_name,
                                "identity_warning": identity_warning,
                                "expected_title_count": expected_card.get("count"),
                                "expected_draft_state": expected_state,
                                "blocked_mutation_paths": sorted(set(blocked_paths)),
                            }
                        if int(expected_card.get("count") or 0) > 1:
                            return {
                                "status": "EXPECTED_DRAFT_AMBIGUOUS",
                                "display_name": account.display_name,
                                "expected_title_count": expected_card.get("count"),
                                "blocked_mutation_paths": sorted(set(blocked_paths)),
                            }

                    write_button = platform.page.get_by_role(
                        "button",
                        name="写文章",
                        exact=True,
                    )
                    write_button_count = await write_button.count()
                    before_write_url = platform.page.url
                    if write_button_count == 1:
                        await write_button.click(timeout=10000)
                        await asyncio.sleep(3)
                    write_click_probe = await platform.page.evaluate(
                        """(selector) => {
                            const title = document.querySelector(
                                "textarea[placeholder='请输入标题']"
                            );
                            const body = document.querySelector(selector);
                            return {
                                url: location.href,
                                title_length: title ? title.value.length : null,
                                body_trimmed_length: body
                                    ? (body.innerText || '').trim().length
                                    : null,
                            };
                        }""",
                        BODY_DOM_SELECTOR,
                    )
                    write_click_probe.update(
                        {
                            "button_count": write_button_count,
                            "before_url": before_write_url,
                        }
                    )
                    await platform.page.goto(
                        "https://card.weibo.com/article/v5/editor#/draft/0",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(4)
                    invalid_probe = await platform.page.evaluate(
                        """(selector) => {
                            const visible = (node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            };
                            return {
                                editor_visible: Array.from(
                                    document.querySelectorAll(selector)
                                ).some(visible),
                                editor_state: (() => {
                                    const title = document.querySelector(
                                        "textarea[placeholder='请输入标题']"
                                    );
                                    const editors = Array.from(
                                        document.querySelectorAll(selector)
                                    ).filter(visible);
                                    const body = editors.length === 1 ? editors[0] : null;
                                    return {
                                        editor_count: editors.length,
                                        title_length: title ? title.value.length : null,
                                        body_text_length: body
                                            ? (body.innerText || '').length
                                            : null,
                                        body_trimmed_length: body
                                            ? (body.innerText || '').trim().length
                                            : null,
                                        body_child_tags: body
                                            ? Array.from(body.children).slice(0, 20)
                                                .map((child) => child.tagName.toLowerCase())
                                            : [],
                                        body_child_count: body ? body.children.length : null,
                                        body_placeholder: body
                                            ? (body.getAttribute('data-placeholder') || '')
                                                .slice(0, 40)
                                            : '',
                                    };
                                })(),
                                controls: Array.from(document.querySelectorAll(
                                    '[role], [title], [aria-label], svg'
                                )).filter((node) => {
                                    if (!visible(node)) return false;
                                    const rect = node.getBoundingClientRect();
                                    return rect.y >= 0 && rect.y < 700;
                                }).slice(0, 160).map((node) => ({
                                    tag: node.tagName.toLowerCase(),
                                    role: (node.getAttribute('role') || '').slice(0, 40),
                                    text: (node.innerText || '').trim().slice(0, 50),
                                    title: (node.getAttribute('title') || '').slice(0, 80),
                                    aria_label: (
                                        node.getAttribute('aria-label') || ''
                                    ).slice(0, 80),
                                    class_name: String(node.className?.baseVal
                                        || node.className || '').slice(0, 180),
                                    parent_class: String(
                                        node.parentElement?.className?.baseVal
                                        || node.parentElement?.className || ''
                                    ).slice(0, 180),
                                    grandparent_class: String(
                                        node.parentElement?.parentElement?.className?.baseVal
                                        || node.parentElement?.parentElement?.className || ''
                                    ).slice(0, 180),
                                    x: Math.round(node.getBoundingClientRect().x),
                                    y: Math.round(node.getBoundingClientRect().y),
                                    svg_paths: Array.from(
                                        node.matches('svg') ? node.querySelectorAll('path')
                                            : node.querySelectorAll('svg path')
                                    ).map((path) => path.getAttribute('d') || ''),
                                })),
                                buttons: Array.from(document.querySelectorAll('button'))
                                    .filter(visible).slice(0, 80).map((button) => ({
                                        text: (button.innerText || '').trim().slice(0, 40),
                                        title: (button.getAttribute('title') || '').slice(0, 80),
                                        aria_label: (
                                            button.getAttribute('aria-label') || ''
                                        ).slice(0, 80),
                                        svg_paths: Array.from(
                                            button.querySelectorAll('svg path')
                                        ).map((path) => path.getAttribute('d') || ''),
                                    })),
                                file_inputs: Array.from(
                                    document.querySelectorAll('input[type=file]')
                                ).map((input) => ({
                                    accept: String(input.accept || '').slice(0, 120),
                                    multiple: Boolean(input.multiple),
                                    cover_ancestor: Boolean(
                                        input.closest('[class*=cover], [data-cover]')
                                    ),
                                })),
                            };
                        }""",
                        BODY_DOM_SELECTOR,
                    )
                    format_trigger = await platform.page.evaluate(
                        """() => {
                            const visible = (node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && rect.y >= 0 && rect.y < 700
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            };
                            const matches = Array.from(
                                document.querySelectorAll('div, span')
                            ).filter((node) => visible(node)
                                && (node.innerText || '').trim() === '正文'
                                && node.classList.contains('wb-cursor-pointer'));
                            if (matches.length !== 1) {
                                return {
                                    clicked: false,
                                    count: matches.length,
                                    candidates: matches.map((node) => {
                                        const rect = node.getBoundingClientRect();
                                        return {
                                            tag: node.tagName.toLowerCase(),
                                            role: node.getAttribute('role') || '',
                                            class_name: String(node.className || '')
                                                .slice(0, 180),
                                            parent_tag: node.parentElement?.tagName
                                                .toLowerCase() || '',
                                            parent_class: String(
                                                node.parentElement?.className || ''
                                            ).slice(0, 180),
                                            child_count: node.children.length,
                                            x: Math.round(rect.x),
                                            y: Math.round(rect.y),
                                            width: Math.round(rect.width),
                                            height: Math.round(rect.height),
                                        };
                                    }),
                                };
                            }
                            matches[0].click();
                            return {
                                clicked: true,
                                count: 1,
                                tag: matches[0].tagName.toLowerCase(),
                                class_name: String(matches[0].className || '').slice(0, 180),
                            };
                        }"""
                    )
                    await asyncio.sleep(0.5)
                    format_options = await platform.page.evaluate(
                        """() => {
                            const visible = (node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            };
                            const labels = new Set([
                                '正文', '标题 1', '标题 2', '标题 3',
                                '标题 4', '标题 5', '标题 6',
                            ]);
                            return Array.from(document.querySelectorAll(
                                'div, span, [role=option], [role=menuitem]'
                            )).filter((node) => visible(node)
                                && labels.has((node.innerText || '').trim()))
                                .slice(0, 80).map((node) => ({
                                tag: node.tagName.toLowerCase(),
                                role: (node.getAttribute('role') || '').slice(0, 40),
                                text: (node.innerText || '').trim().slice(0, 100),
                                class_name: String(node.className || '').slice(0, 180),
                                parent_class: String(
                                    node.parentElement?.className || ''
                                ).slice(0, 180),
                                child_count: node.children.length,
                            }));
                        }"""
                    )
                    invalid_probe["format_trigger"] = format_trigger
                    invalid_probe["format_options"] = format_options
                    await platform.page.keyboard.press("Escape")
                    toolbar_items = await platform.page.evaluate(
                        """() => {
                            const toolbar = document.querySelector(
                                '.main-editor-toolbar'
                            );
                            if (!toolbar) return [];
                            const visible = (node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            };
                            const nodes = [];
                            for (const svg of toolbar.querySelectorAll('svg')) {
                                const target = svg.closest(
                                    '.svg-icon-wrapper, [class*="cursor-pointer"]'
                                );
                                if (!target || !visible(target)
                                    || nodes.includes(target)) continue;
                                const rect = target.getBoundingClientRect();
                                if (rect.x < 1030) continue;
                                nodes.push(target);
                            }
                            return nodes.map((node) => {
                                const rect = node.getBoundingClientRect();
                                return {
                                    x: Math.round(rect.x),
                                    y: Math.round(rect.y),
                                    center_x: Math.round(rect.x + rect.width / 2),
                                    center_y: Math.round(rect.y + rect.height / 2),
                                    class_name: String(node.className || '')
                                        .slice(0, 180),
                                    svg_paths: Array.from(
                                        node.querySelectorAll('svg path')
                                    ).map((path) => path.getAttribute('d') || ''),
                                };
                            });
                        }"""
                    )
                    hover_labels = []
                    for item in toolbar_items:
                        await platform.page.mouse.move(
                            int(item["center_x"]),
                            int(item["center_y"]),
                        )
                        await asyncio.sleep(0.45)
                        labels = await platform.page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                '.n-popover, [role=tooltip]'
                            )).filter((node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            }).map((node) => (node.innerText || '').trim())
                                .filter(Boolean).slice(0, 8)"""
                        )
                        paths = [str(value) for value in item.pop("svg_paths", [])]
                        hover_labels.append(
                            {
                                "x": item["x"],
                                "y": item["y"],
                                "class_name": item["class_name"],
                                "svg_fingerprint": (
                                    _fingerprint_paths(paths) if paths else ""
                                ),
                                "labels": labels,
                            }
                        )
                    invalid_probe["toolbar_hover_labels"] = hover_labels
                    image_trigger = await platform._find_body_image_trigger()
                    await image_trigger.click(timeout=5000)
                    await asyncio.sleep(0.75)
                    invalid_probe["body_image_panel"] = await platform.page.evaluate(
                        """() => {
                            const visible = (node) => {
                                const rect = node.getBoundingClientRect();
                                const style = getComputedStyle(node);
                                return rect.width > 0 && rect.height > 0
                                    && style.display !== 'none'
                                    && style.visibility !== 'hidden';
                            };
                            return {
                                file_inputs: Array.from(document.querySelectorAll(
                                    'input[type=file]'
                                )).map((input) => ({
                                    accept: String(input.accept || '').slice(0, 160),
                                    multiple: Boolean(input.multiple),
                                    visible: visible(input),
                                    parent_class: String(
                                        input.parentElement?.className || ''
                                    ).slice(0, 180),
                                    ancestor_class: String(
                                        input.parentElement?.parentElement?.className || ''
                                    ).slice(0, 180),
                                })),
                                layers: Array.from(document.querySelectorAll(
                                    '.n-popover, .n-modal, .n-dialog, [role=dialog]'
                                )).filter(visible).map((node) => ({
                                    class_name: String(node.className || '')
                                        .slice(0, 180),
                                    text: (node.innerText || '').trim().slice(0, 500),
                                })).slice(0, 20),
                            };
                        }"""
                    )
                finally:
                    await platform.page.unroute("**/*", block_mutations)
                for button in invalid_probe.get("buttons", []):
                    paths = [str(item) for item in button.pop("svg_paths", [])]
                    button["svg_path_count"] = len(paths)
                    button["svg_fingerprint"] = (
                        _fingerprint_paths(paths) if paths else ""
                    )
                for control in invalid_probe.get("controls", []):
                    paths = [str(item) for item in control.pop("svg_paths", [])]
                    control["svg_path_count"] = len(paths)
                    control["svg_fingerprint"] = (
                        _fingerprint_paths(paths) if paths else ""
                    )
                return {
                    "status": "NO_EXISTING_DRAFT",
                    "display_name": account.display_name,
                    "identity_warning": identity_warning,
                    "identity_evidence": identity_evidence,
                    "draft_list_state": draft_list_state,
                    "write_click_probe": write_click_probe,
                    "invalid_draft_probe": invalid_probe,
                    "blocked_mutation_paths": sorted(set(blocked_paths)),
                }
            await platform.page.goto(
                str(draft_url),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await platform.page.wait_for_selector(
                BODY_DOM_SELECTOR,
                state="visible",
                timeout=20000,
            )
            structure = await platform.page.evaluate(
                """(selector) => {
                    const editor = document.querySelector(selector);
                    if (!editor) return null;
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden';
                    };
                    const buttons = Array.from(document.querySelectorAll('button'))
                        .filter(visible)
                        .map((button) => ({
                            text: (button.innerText || '').trim().slice(0, 40),
                            title: (button.getAttribute('title') || '').slice(0, 80),
                            aria_label: (button.getAttribute('aria-label') || '').slice(0, 80),
                            svg_paths: Array.from(button.querySelectorAll('svg path'))
                                .map((path) => path.getAttribute('d') || ''),
                        }));
                    const file_inputs = Array.from(
                        document.querySelectorAll('input[type=file]')
                    ).map((input) => ({
                        accept: String(input.accept || '').slice(0, 120),
                        multiple: Boolean(input.multiple),
                        cover_ancestor: Boolean(
                            input.closest('[class*=cover], [data-cover]')
                        ),
                    }));
                    const editor_children = Array.from(editor.children).slice(0, 80)
                        .map((node) => ({
                            tag: node.tagName.toLowerCase(),
                            images: node.matches('img')
                                ? 1 : node.querySelectorAll('img').length,
                        }));
                    return {buttons, file_inputs, editor_children};
                }""",
                BODY_DOM_SELECTOR,
            )
            if not isinstance(structure, dict):
                raise ProbeError("WEIBO_EDITOR_STRUCTURE_UNAVAILABLE")
            buttons = []
            for button in structure.get("buttons", []):
                paths = [str(item) for item in button.pop("svg_paths", [])]
                buttons.append(
                    {
                        **button,
                        "svg_path_count": len(paths),
                        "svg_fingerprint": _fingerprint_paths(paths) if paths else "",
                    }
                )
            return {
                "status": "VERIFIED_READ_ONLY",
                "display_name": account.display_name,
                "identity_warning": identity_warning,
                "identity_evidence": identity_evidence,
                "buttons": buttons,
                "file_inputs": structure.get("file_inputs", []),
                "editor_children": structure.get("editor_children", []),
            }
    finally:
        await platform.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-title",
        default="",
        help="只读打开标题精确匹配且唯一的既有草稿，并仅返回结构摘要",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(run_probe(args.expected_title))
    except ProbeError as exc:
        result = {"status": str(exc)}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") in {
        "VERIFIED_READ_ONLY",
        "EXPECTED_DRAFT_FOUND_READ_ONLY",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
