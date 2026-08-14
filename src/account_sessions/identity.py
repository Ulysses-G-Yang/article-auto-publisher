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
    if platform.platform_name == "zhihu":
        return await _extract_zhihu(platform)
    if platform.platform_name == "douyin":
        return await _extract_douyin(platform)
    if platform.platform_name == "xiaohongshu":
        return await _extract_xiaohongshu(platform)
    raise AccountIdentityError("不支持的平台身份提取")


async def _cookie_map(context) -> dict[str, str]:
    cookies = await context.cookies()
    return {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in cookies
    }


async def _extract_xiaoheihe(platform) -> AccountIdentity:
    # 权威优先：restore_login 捕获/重放的同源平台身份（ID+昵称同时确认）。
    # 小黑盒首页导航不渲染昵称，cookie 里也没有昵称，只有该接口返回真实昵称。
    fetcher = getattr(platform, "fetch_identity_payload", None)
    if callable(fetcher):
        payload = await fetcher()
        if isinstance(payload, dict) and payload.get("ok"):
            user_id = _clean(payload.get("user_id"))
            display_name = _clean(payload.get("display_name"))
            if user_id and display_name:
                return AccountIdentity(user_id, display_name)
    # 回退：cookie（user_heybox_id 为现行命名，heybox_id 兼容旧名）+ DOM 昵称。
    cookies = await _cookie_map(platform.context)
    user_id = _clean(
        cookies.get("user_heybox_id") or cookies.get("heybox_id")
    )
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


async def _extract_zhihu(platform) -> AccountIdentity:
    """只采用已经过同源身份 API 验证的稳定 ID 与昵称。"""

    payload = getattr(platform, "_identity_payload", None)
    if not isinstance(payload, dict) or not payload.get("ok"):
        payload = await platform.fetch_identity_payload()
    user_id = _clean(payload.get("user_id")) if isinstance(payload, dict) else ""
    display_name = (
        _clean(payload.get("display_name")) if isinstance(payload, dict) else ""
    )
    if not user_id or not display_name:
        raise AccountIdentityError("知乎已登录，但身份 API 未同时确认账号 ID 和昵称")
    return AccountIdentity(user_id, display_name)


async def _extract_douyin(platform) -> AccountIdentity:
    """只采用创作者首页捕获的同源身份（昵称 + 稳定 ID 同时确认）。"""

    fetcher = getattr(platform, "fetch_identity_payload", None)
    payload = {}
    if callable(fetcher):
        payload = await fetcher()
    user_id = _clean(payload.get("user_id")) if isinstance(payload, dict) else ""
    display_name = (
        _clean(payload.get("display_name")) if isinstance(payload, dict) else ""
    )
    if not user_id or not display_name:
        raise AccountIdentityError("抖音已登录，但无法同时确认账号 ID 和真实昵称")
    return AccountIdentity(user_id, display_name)


async def _extract_xiaohongshu(platform) -> AccountIdentity:
    """只采用创作服务平台捕获的同源身份（昵称 + 稳定 ID 同时确认）。"""

    fetcher = getattr(platform, "fetch_identity_payload", None)
    payload = {}
    if callable(fetcher):
        payload = await fetcher()
    user_id = _clean(payload.get("user_id")) if isinstance(payload, dict) else ""
    display_name = (
        _clean(payload.get("display_name")) if isinstance(payload, dict) else ""
    )
    if not user_id or not display_name:
        raise AccountIdentityError("小红书已登录，但无法同时确认账号 ID 和真实昵称")
    return AccountIdentity(user_id, display_name)
