"""从已打开的平台会话中提取最小账号身份，不返回 Cookie 值。"""

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

from account_sessions.errors import AccountIdentityError


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    platform_user_id: str
    display_name: str


def _clean(value: object) -> str:
    text = str(value or "")
    # 小黑盒历史 Cookie 仍可能使用 JavaScript escape 的 ``%u4E2D`` 形式；
    # urllib 只认识 UTF-8 百分号编码，需先把 %uXXXX 转回 Unicode。
    text = re.sub(
        r"%u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    text = unquote_plus(text).strip().strip('"\'')
    return " ".join(text.split())[:255]


async def extract_identity(platform) -> AccountIdentity:
    if platform.platform_name == "xiaoheihe":
        return await _extract_xiaoheihe(platform)
    if platform.platform_name == "zol":
        return await _extract_zol(platform)
    raise AccountIdentityError("不支持的平台身份提取")


async def _cookie_map(context) -> dict[str, str]:
    cookies = await context.cookies()
    return {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in cookies
    }


async def _extract_xiaoheihe(platform) -> AccountIdentity:
    cookies = await _cookie_map(platform.context)
    user_id = _clean(cookies.get("heybox_id"))
    display_name = _clean(cookies.get("nickname"))
    if not display_name:
        display_name = _clean(
            await platform.page.evaluate(
                """() => {
                    const selectors = [
                        '.nav-user-name', '.user-name', '[class*="nickname"]',
                        '[class*="user-name"]'
                    ];
                    for (const selector of selectors) {
                        const node = document.querySelector(selector);
                        const text = node && (node.textContent || '').trim();
                        if (text) return text;
                    }
                    return '';
                }"""
            )
        )
    if not user_id or not display_name:
        raise AccountIdentityError("小黑盒已登录，但无法同时确认账号 ID 和真实昵称")
    return AccountIdentity(user_id, display_name)


async def _extract_zol(platform) -> AccountIdentity:
    cookies = await _cookie_map(platform.context)
    user_id = _clean(
        cookies.get("zol_userid")
        or cookies.get("userId")
        or cookies.get("last_userid")
    )
    display_name = _clean(cookies.get("userName"))
    api_identity = await platform.page.evaluate(
        """async () => {
            try {
                const response = await fetch(
                    'https://open-api.zol.com.cn/api/v1/creator.user.getinfo',
                    { credentials: 'include' }
                );
                const payload = await response.json().catch(() => ({}));
                const data = payload && payload.data ? payload.data : payload;
                return {
                    user_id: data && (data.userId || data.user_id || data.uid || ''),
                    display_name: data && (
                        data.userName || data.username || data.nickname || data.nickName || ''
                    ),
                };
            } catch (_) {
                return { user_id: '', display_name: '' };
            }
        }"""
    )
    if isinstance(api_identity, dict):
        user_id = _clean(api_identity.get("user_id")) or user_id
        display_name = _clean(api_identity.get("display_name")) or display_name
    if not display_name:
        display_name = _clean(
            await platform.page.evaluate(
                """() => {
                    const selectors = ['.user-name', '.header-user', '.nickname'];
                    for (const selector of selectors) {
                        const node = document.querySelector(selector);
                        const text = node && (node.textContent || '').trim();
                        if (text) return text;
                    }
                    return '';
                }"""
            )
        )
    if not user_id or not display_name:
        raise AccountIdentityError("ZOL 已登录，但无法同时确认账号 ID 和真实昵称")
    return AccountIdentity(user_id, display_name)
