"""中关村在线（ZOL）创作者中心自动化"""
import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAccessError,
    SelectorError,
)
from platforms.content_validation import ensure_valid_content, safe_media_error


class ZOLPlatform(BasePlatform):
    platform_name = "zol"

    CREATOR_HOST = "post.zol.com.cn"
    BLOG_HOST = "blog.zol.com.cn"  # 旧博客入口，仅用于识别历史重定向
    FORUM_HOST = "bbs.zol.com.cn"
    LOGIN_HOST = "service.zol.com.cn"
    CRITICAL_COOKIES = {"last_userid", "lv", "zol_userid", "zol_sid"}
    IMAGE_BUTTON = "button[title='图片上传'], button[aria-label='图片上传']"
    IMAGE_MODAL = ".ant-modal-wrap:visible"
    TOPIC_BUTTON = "button:has-text('选择话题')"
    TOPIC_MODAL = ".ant-modal-wrap:visible"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (urlparse(url or "").hostname or "").lower()
        except Exception:
            return ""

    @classmethod
    def _is_blog_editor_url(cls, url: str) -> bool:
        parsed = urlparse(url or "")
        legacy_editor = (
            parsed.hostname == cls.BLOG_HOST
            and parsed.path.endswith("/post.php")
            and "act=add" in (parsed.query or "")
        )
        creator_editor = (
            parsed.hostname == cls.CREATOR_HOST
            and parsed.path.rstrip("/") == "/v2/create/article"
        )
        return legacy_editor or creator_editor

    @classmethod
    def _is_login_url(cls, url: str) -> bool:
        parsed = urlparse(url or "")
        if parsed.hostname == cls.LOGIN_HOST:
            return "login" in parsed.path.lower()
        if parsed.hostname == cls.CREATOR_HOST:
            return parsed.path.rstrip("/") in ("", "/v2/login")
        if parsed.hostname == "my.zol.com.cn":
            # 登录成功后 ZOL 可能跳到 my.zol.cn/{username}；只有根路径仍视为
            # 入口页，不能把已登录的个人中心路径误判成登录页。
            return parsed.path in ("", "/")
        return False

    async def _valid_cookie_names(self) -> set[str]:
        """只返回未过期的关键 Cookie 名称，不记录 Cookie 值。"""
        if not self.context:
            return set()
        try:
            # 不限制 URL，避免遗漏 service/blog 域名上的 HttpOnly 会话 Cookie。
            cookies = await self.context.cookies()
        except TypeError:
            # 兼容旧版测试替身或 Playwright 兼容实现。
            cookies = await self.context.cookies([
                "https://my.zol.com.cn/",
                "https://service.zol.com.cn/",
                "https://blog.zol.com.cn/",
                "https://bbs.zol.com.cn/",
            ])
        now = time.time()
        return {
            item.get("name", "")
            for item in cookies
            if item.get("name", "") in self.CRITICAL_COOKIES
            and (
                item.get("expires", -1) == -1
                or item.get("expires", 0) > now
            )
        }

    async def _bridge_creator_cookies(self):
        """把同一登录会话补到创作者中心使用的 .zol.com.cn 域。"""
        if not self.context:
            return 0
        try:
            cookies = await self.context.cookies()
        except TypeError:
            cookies = await self.context.cookies([
                "https://service.zol.com.cn/",
                "https://post.zol.com.cn/",
                "https://open-api.zol.com.cn/",
            ])

        auth_names = {
            "zol_sid", "zol_userid", "zol_check", "zol_cipher",
            "toKen", "userName", "userId", "last_userid",
        }
        existing = {
            (item.get("name", ""), item.get("domain", "").lower())
            for item in cookies
        }
        now = time.time()
        clones = []
        for item in cookies:
            domain = (item.get("domain") or "").lower()
            name = item.get("name", "")
            expires = item.get("expires", -1)
            if domain != ".zol.com" or name not in auth_names:
                continue
            if expires not in (-1, None) and expires <= now:
                continue
            if (name, ".zol.com.cn") in existing:
                continue
            clone = {
                key: item[key]
                for key in (
                    "name", "value", "path", "expires",
                    "httpOnly", "secure", "sameSite",
                )
                if key in item
            }
            clone["domain"] = ".zol.com.cn"
            clones.append(clone)

        if clones:
            await self.context.add_cookies(clones)
            logger.info(
                "ZOL 创作者中心会话域兼容完成: cloned_cookie_count={}",
                len(clones),
            )
        return len(clones)

    async def _read_page_state(self) -> dict:
        self._require_page_alive("ZOL 页面状态探测")
        return await self.page.evaluate("""
            () => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && el.offsetParent !== null;
                };
                const bodyText = document.body ? (document.body.innerText || '') : '';
                const lowerText = bodyText.toLowerCase();
                return {
                    url: window.location.href,
                    hasUser: visible(document.querySelector('.user-name, .header-user, .login-info, .nickname')),
                    hasLogout: visible(document.querySelector('a[href*="logout"]')),
                    hasLoginForm: [
                        '#J_LoginUser', '#J_LoginPsw', '#J_LoginBtn',
                        'input[type="password"]', '.login-form', '.login-box',
                        '#login .ant-tabs'
                    ].some(selector => visible(document.querySelector(selector))),
                    hasSecurityChallenge: [
                        '验证码', '安全验证', '风险验证', '异常登录', '滑块验证', 'captcha'
                    ].some(marker => bodyText.includes(marker) || lowerText.includes(marker)),
                };
            }
        """)

    async def _editor_probe_count(self) -> int:
        self._require_page_alive("ZOL 编辑器结构探测")
        return await self.page.locator(
            "#title, input[name='title'], .title-input input, .blog-title input, "
            "textarea[name='content'], iframe.ke-edit-iframe, .ke-container iframe, "
            "#content_ifr, iframe[id*='content'], [contenteditable='true'], "
            "#article-editor input.main-title, .tox-edit-area iframe, "
            ".tox-edit-area [contenteditable='true'], .mce-content-body"
        ).count()

    async def _creator_api_login_state(self) -> dict:
        """验证创作者中心后端会话，而不是只看前端路由和缓存用户信息。"""
        self._require_page_alive("ZOL 创作者中心 API 登录态探测")
        try:
            return await self.page.evaluate("""
                async () => {
                    try {
                        const response = await fetch(
                            'https://open-api.zol.com.cn/api/v1/creator.user.getinfo',
                            { credentials: 'include' }
                        );
                        const payload = await response.json().catch(() => ({}));
                        const data = payload && payload.data ? payload.data : payload;
                        return {
                            ok: response.ok,
                            errcode: payload && payload.errcode,
                            has_user: Boolean(data && (data.userId || data.user_id)),
                        };
                    } catch (error) {
                        return { ok: false, error: String(error || 'request failed') };
                    }
                }
            """)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: ZOL 创作者中心 API 探测时页面已关闭"
                ) from exc
            return {"ok": False, "error": str(exc)}

    async def check_login(self, *, allow_cookie_bridge: bool = True) -> bool:
        """检查是否能进入真实 ZOL 创作者中心编辑器。

        个人中心 Cookie 存在并不等于博客编辑器可用；过期或域不完整的
        Cookie 可能仍能打开 ``my.zol.com.cn``，但编辑器会重定向到论坛登录页。
        因此这里同时验证编辑器 URL、编辑器 DOM、页面登录提示和 HttpOnly Cookie。
        """
        try:
            self.last_login_error = ""
            self._require_page_alive("ZOL 登录态检测")
            editor_url = self.platform_cfg.get(
                "editor_url", "https://post.zol.com.cn/v2/create/article"
            )
            if allow_cookie_bridge and self._host(editor_url) == self.CREATOR_HOST:
                await self._bridge_creator_cookies()
            await self.page.goto(
                editor_url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(3)
            page_state = await self._read_page_state()
            editor_probe = await self._editor_probe_count()
            current_url = page_state.get("url") or self.page.url or ""
            if self._host(current_url) == self.FORUM_HOST:
                self.last_login_error = (
                    "ZOL_BLOG_EDITOR_REDIRECT: 当前会话被重定向到论坛，"
                    f"无法进入博客编辑器，当前 URL: {current_url}"
                )
                logger.warning(self.last_login_error)
                return False
            if self._host(current_url) == self.CREATOR_HOST and current_url.rstrip("/").endswith("/v2/403"):
                self.last_login_error = "ZOL_CREATOR_ACCESS_DENIED: ZOL 创作者中心拒绝当前账号访问"
                logger.warning(self.last_login_error)
                return False
            if page_state.get("hasSecurityChallenge"):
                self.last_login_error = "ZOL_SECURITY_CHALLENGE: 页面要求完成安全验证"
                logger.warning(self.last_login_error)
                return False
            if page_state.get("hasLoginForm") or self._is_login_url(current_url):
                self.last_login_error = "LOGIN_REQUIRED: ZOL 博客编辑器需要重新登录"
                return False
            if not self._is_blog_editor_url(current_url) or editor_probe == 0:
                self.last_login_error = (
                    "ZOL_EDITOR_UNAVAILABLE: 当前页面不是可用的博客编辑器，"
                    f"url={current_url}, selector_count={editor_probe}"
                )
                logger.warning(self.last_login_error)
                return False
            if self._host(current_url) == self.CREATOR_HOST:
                api_state = await self._creator_api_login_state()
                if not api_state.get("ok") or api_state.get("errcode") not in (0, None) or not api_state.get("has_user"):
                    self.last_login_error = (
                        "ZOL_CREATOR_SESSION_INVALID: 创作者中心编辑器可见，"
                        f"但后端会话无效，errcode={api_state.get('errcode', 'unknown')}"
                    )
                    logger.warning(self.last_login_error)
                    return False
                logger.info(
                    "ZOL 创作者中心编辑器登录态验证成功: url={}, selector_count={}, api_session=ok",
                    current_url,
                    editor_probe,
                )
                return True
            cookie_names = await self._valid_cookie_names()
            if not (page_state.get("hasUser") or page_state.get("hasLogout") or cookie_names):
                self.last_login_error = "LOGIN_REQUIRED: ZOL 编辑器没有可验证的登录信号"
                return False
            logger.info(
                "ZOL 博客编辑器登录态验证成功: url={}, selector_count={}, cookie_names={}",
                current_url,
                editor_probe,
                sorted(cookie_names),
            )
            return True
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 登录态检测时页面已关闭") from exc
            self.last_login_error = f"ZOL_LOGIN_CHECK_ERROR: {exc}"
            logger.warning("ZOL 登录态检测失败: url={}, error={}", getattr(self.page, "url", ""), exc)
            return False

    async def _check_login_current_page(self) -> bool:
        """检查当前页面是否已登录（不跳转，用于登录等待循环）"""
        try:
            page_state = await self._read_page_state()
            current_url = page_state.get("url") or self.page.url or ""
            if page_state.get("hasLoginForm") or page_state.get("hasSecurityChallenge"):
                return False
            if self._is_login_url(current_url):
                return False
            # my.zol.cn/{username}/ 是公开个人主页，未登录也能访问；页面中的
            # .nickname 不能证明当前 Profile 已认证。个人中心只有出现退出入口
            # 时才作为登录信号，避免扫码未完成却被误判为成功。
            host = self._host(current_url)
            if host == "my.zol.cn" and not page_state.get("hasLogout"):
                return False
            return bool(
                page_state.get("hasLogout")
                or (host not in {self.LOGIN_HOST, "my.zol.cn"} and page_state.get("hasUser"))
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 登录等待页面已关闭") from exc
            return False

    async def login(self):
        """打开登录页，等待用户手动完成登录"""
        self._require_page_alive("ZOL 打开登录页")
        # 当前 ZOL 投稿使用创作者中心；旧 service 登录页只建立通用会话，
        # 扫码后仍可能无法进入创作者中心，因此不再作为默认入口。
        login_url = self.platform_cfg.get(
            "login_url",
            "https://post.zol.com.cn/v2/login",
        )
        await self.page.goto(login_url, wait_until="domcontentloaded")
        await self.simulator.random_delay(1, 2)

        if self._host(login_url) == self.CREATOR_HOST:
            # 创作者中心默认是“账号登录”，二维码位于 APP 扫码标签的跨域 iframe 中。
            qr_tab = self.page.get_by_role("tab", name="APP扫码登录", exact=True).first
            if await qr_tab.count() == 0 or not await qr_tab.is_visible():
                raise SelectorError("ZOL_QR_TAB_NOT_FOUND: 未找到创作者中心 APP 扫码登录标签")
            await qr_tab.click(timeout=5000)
            qr_frame_locator = self.page.locator(
                "iframe[src*='siteLogin.php'][src*='loginType=qrcode']"
            ).first
            if await qr_frame_locator.count() == 0:
                raise SelectorError("ZOL_QR_IFRAME_NOT_FOUND: 创作者中心二维码 iframe 未生成")
            await qr_frame_locator.wait_for(state="visible", timeout=8000)
            qr_frame = self.page.frame_locator(
                "iframe[src*='siteLogin.php'][src*='loginType=qrcode']"
            )
            qr_image = qr_frame.locator("img[src*='getQrAddr.php']").first
            await qr_image.wait_for(state="visible", timeout=8000)
            logger.info("ZOL 创作者中心已打开并确认 APP 手机扫码登录二维码")
        else:
            # 兼容旧配置：旧页面默认是短信登录，必须点击可见切换按钮。
            try:
                qr_panel = self.page.locator(".code-login").first
                if not await qr_panel.is_visible():
                    qr_toggle = self.page.locator("#tel-login .login-change").first
                    if await qr_toggle.count() == 0 or not await qr_toggle.is_visible():
                        raise SelectorError("ZOL_QR_TOGGLE_NOT_FOUND: 未找到可见的扫码切换按钮")
                    await qr_toggle.click(timeout=5000)
                    await self.simulator.random_delay(0.5, 1.5)

                qr_panel = self.page.locator(".code-login").first
                qr_nodes = self.page.locator(
                    ".code-login #output img, .code-login #output canvas, .code-login #output svg"
                )
                if not await qr_panel.is_visible():
                    raise SelectorError("ZOL_QR_NOT_RENDERED: 扫码面板可见但二维码未生成")
                await qr_nodes.first.wait_for(state="visible", timeout=5000)
                logger.info("ZOL 旧登录页已切换并确认手机扫码安全登录界面")
            except Exception as exc:
                if isinstance(exc, SelectorError):
                    raise
                logger.warning("ZOL 旧扫码页面切换/探测失败: {}", exc)

        # 在浏览器页面中显示提示
        await self.page.evaluate("""
            () => {
                const div = document.createElement('div');
                div.id = 'wb-login-hint';
                div.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#0d6efd;color:#fff;padding:12px 24px;border-radius:8px;font-size:16px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.3);text-align:center;';
                div.innerHTML = '请在下方完成登录（扫码或输入账号密码）<br><small>登录成功后工具将自动关闭此页面</small>';
                document.body.appendChild(div);
            }
        """)

        logger.info("ZOL 登录窗口已打开，请在浏览器中完成扫码或账号密码登录")

        # 等待用户完成登录。离开扫码页后必须立即验证博客编辑器，不能只凭
        # last_userid/lv 等可长期存在的 Cookie 或首页昵称判断登录成功。
        # 30 次 × 3 秒 = 90 秒超时；用户关掉浏览器时立即中止，不空转。
        max_iters = 30
        login_page_url = self.page.url or login_url
        for _ in range(max_iters):
            if self.page.is_closed():
                raise RuntimeError("用户关闭了浏览器窗口，登录中止")
            current_url = self.page.url or ""
            left_login_page = (
                current_url != login_page_url
                and not self._is_login_url(current_url)
            )
            current_page_has_auth_signal = await self._check_login_current_page()
            if left_login_page or current_page_has_auth_signal:
                logger.info("ZOL 登录页已跳转，开始验证博客编辑器: url={}", current_url)
                if await self.check_login():
                    logger.info("ZOL 登录并进入博客编辑器验证成功")
                    return
                reason = getattr(self, "last_login_error", "") or (
                    "LOGIN_REQUIRED: ZOL 登录后未能验证博客编辑器"
                )
                raise RuntimeError(reason)
            await asyncio.sleep(3)

        raise LoginRequiredError("LOGIN_REQUIRED: ZOL 登录超时（90 秒），请重新扫码或检查安全验证")

    async def navigate_to_editor(self):
        """导航到博客编辑器"""
        self._require_page_alive("ZOL 打开博客编辑器")
        editor_url = self.platform_cfg.get(
            "editor_url", "https://post.zol.com.cn/v2/create/article"
        )
        try:
            await self.page.goto(
                editor_url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 编辑器导航时页面已关闭") from exc
            raise
        await self.simulator.random_delay(2, 4)
        self._require_page_alive("ZOL 验证博客编辑器")
        current_url = self.page.url or ""
        host = self._host(current_url)
        if host == self.FORUM_HOST:
            raise PlatformAccessError(
                "ZOL_BLOG_EDITOR_REDIRECT: ZOL 博客编辑器跳转到论坛，"
                f"当前 URL: {current_url}"
            )
        if self._is_login_url(current_url):
            raise LoginRequiredError(
                f"LOGIN_REQUIRED: ZOL 编辑器导航后仍在登录页，当前 URL: {current_url}"
            )
        if not self._is_blog_editor_url(current_url):
            raise PlatformAccessError(
                f"ZOL_EDITOR_ROUTE_ERROR: ZOL 编辑器跳转失败，当前 URL: {current_url}"
            )
        probe = await self._editor_probe_count()
        if probe == 0:
            raise SelectorError(f"ZOL_EDITOR_SELECTOR_ERROR: 编辑器结构探测失败，当前 URL: {current_url}")
        await self._safe_simulate_scroll(scroll_times=2, stage="ZOL 编辑器初始滚动")

    async def fill_title(self, title: str):
        """填写并验证博客标题，不再静默吞掉定位/输入失败。"""
        self._require_page_alive("ZOL 填写标题")
        expected = re.sub(r"\s+", " ", (title or "").strip())
        max_length = int(self.platform_cfg.get("title_max_length", 35))
        if len(expected) < 5:
            raise SelectorError("ZOL_TITLE_VALIDATION_ERROR: 标题至少需要 5 个字")
        if len(expected) > max_length:
            raise SelectorError(
                f"ZOL_TITLE_VALIDATION_ERROR: 标题超过当前平台限制 {max_length} 个字，"
                f"实际 {len(expected)} 个字"
            )
        selectors = [
            "input.main-title",
            ".main-title",
            "input[placeholder*='请输入文章标题']",
            "#title",
            "input[name='title']",
            ".title-input input",
            ".blog-title input",
            "textarea[name='title']",
            "[contenteditable='true'][data-placeholder*='标题']",
        ]
        for selector in selectors:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() == 0 or not await locator.is_visible():
                    continue
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name not in ("input", "textarea") and not await locator.get_attribute("contenteditable"):
                    # .main-title 是当前 Ant Design 输入框的外层 span，
                    # 真正可编辑节点由后面的 placeholder 选择器定位。
                    continue
                if tag_name in ("input", "textarea"):
                    await locator.fill(expected)
                    actual = await locator.input_value()
                else:
                    await locator.click()
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.insert_text(expected)
                    await locator.evaluate(
                        "el => el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}))"
                    )
                    actual = await locator.inner_text()
                actual_normalized = re.sub(r"\s+", " ", actual.strip())
                if actual_normalized != expected:
                    raise RuntimeError(
                        f"ZOL 标题验证失败: expected={expected!r}, actual={actual!r}"
                    )
                logger.info("ZOL 标题输入并验证成功: {}", expected[:30])
                return
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 填写标题时页面已关闭") from exc
                logger.debug("ZOL 标题候选选择器失败: selector={}, error={}", selector, exc)

        raise SelectorError("ZOL_TITLE_SELECTOR_ERROR: ZOL 标题输入框未找到或输入后校验失败")

    async def _is_visible_locator(self, locator) -> bool:
        """只接受当前可见节点，避免把旧 iframe/body 当成编辑器。"""

        try:
            if await locator.count() == 0 or not await locator.is_visible():
                return False
            aria_hidden = await locator.get_attribute("aria-hidden")
            return aria_hidden != "true"
        except AttributeError:
            # 兼容极简测试替身；真实 Playwright Locator 始终支持这些方法。
            try:
                return await locator.count() > 0 and await locator.is_visible()
            except Exception:
                return False

    async def _is_visible_editable(self, locator, *, iframe_body: bool = False) -> bool:
        """验证节点可见且可编辑；验证失败不得以 focus/DOM click 绕过。"""

        if not await self._is_visible_locator(locator):
            return False
        try:
            disabled = await locator.get_attribute("disabled")
            readonly = await locator.get_attribute("readonly")
            contenteditable = await locator.get_attribute("contenteditable")
            if disabled is not None or readonly is not None:
                return False
            if contenteditable is not None and contenteditable.lower() == "false":
                return False
        except AttributeError:
            contenteditable = None

        is_editable = getattr(locator, "is_editable", None)
        if callable(is_editable):
            try:
                return bool(await is_editable())
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: ZOL 验证正文编辑器时页面已关闭"
                    ) from exc
                return False

        try:
            tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: ZOL 读取正文节点时页面已关闭"
                ) from exc
            return False
        if contenteditable is not None:
            return contenteditable.lower() in {"", "true"}
        # Playwright 的真实 Locator 会走 is_editable()；以下仅兼容没有
        # is_editable/get_attribute 的极简替身，不改变真实页面的 fail-closed。
        return tag_name in {"input", "textarea", "div"} or (
            iframe_body and tag_name == "body"
        )

    async def _first_visible_editable(self, locator, *, iframe_body: bool = False):
        """从所有候选中选择第一个当前可见、可编辑节点，而非盲取 first。"""

        count = await locator.count()
        for index in range(min(count, 30)):
            candidate = locator.nth(index)
            if await self._is_visible_editable(candidate, iframe_body=iframe_body):
                return candidate
        return None

    async def _resolve_content_editor(self):
        """定位当前可编辑正文，并显式返回 iframe/textarea/contenteditable 策略。"""

        self._require_page_alive("ZOL 定位正文编辑器")
        iframe_selectors = [
            ".tox-edit-area iframe",
            "iframe.tox-edit-area__iframe",
            "iframe[title='Rich Text Area']",
            "iframe.ke-edit-iframe",
            ".ke-container iframe",
            "#content_ifr",
            "iframe[id*='content']",
        ]
        for selector in iframe_selectors:
            locator = self.page.locator(selector)
            try:
                count = await locator.count()
                for index in range(min(count, 30)):
                    iframe = locator.nth(index)
                    if not await self._is_visible_locator(iframe):
                        continue
                    handle = await iframe.element_handle()
                    if not handle:
                        continue
                    editor_frame = await handle.content_frame()
                    if not editor_frame:
                        continue
                    editor = await self._first_visible_editable(
                        editor_frame.locator("body"),
                        iframe_body=True,
                    )
                    if editor is not None:
                        return editor, "iframe"
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: ZOL 定位 iframe 正文编辑器时页面已关闭"
                    ) from exc
                logger.debug(
                    "ZOL iframe 探测失败: selector={}, error_type={}",
                    selector,
                    type(exc).__name__,
                )

        editor_selectors = [
            ".mce-content-body",
            "#tinymce",
            "textarea[name='content']",
            "#content",
            "div.ke-edit",
            "div.ke-content",
            ".editor-content",
            "[contenteditable='true']",
        ]
        for selector in editor_selectors:
            locator = self.page.locator(selector)
            try:
                editor = await self._first_visible_editable(locator)
                if editor is None:
                    continue
                tag_name = await editor.evaluate("el => el.tagName.toLowerCase()")
                return editor, "textarea" if tag_name == "textarea" else "contenteditable"
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: ZOL 定位正文编辑器时页面已关闭"
                    ) from exc
                logger.debug(
                    "ZOL 正文候选选择器失败: selector={}, error_type={}",
                    selector,
                    type(exc).__name__,
                )

        raise SelectorError(
            f"ZOL_CONTENT_SELECTOR_ERROR: 正文编辑器未找到，当前 URL: {self.page.url}"
        )

    async def _read_content_editor_text(self, editor, editor_kind: str) -> str:
        """按明确策略读取编辑器范围内正文，不读取页面或 HTML。"""
        try:
            if editor_kind == "textarea":
                return await editor.input_value()
            if editor_kind in {"iframe", "contenteditable"}:
                return await editor.inner_text()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: ZOL 读取正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError(
                f"ZOL_CONTENT_READ_ERROR: {editor_kind} 正文读取失败"
            ) from exc
        raise SelectorError(f"ZOL_CONTENT_READ_ERROR: 未知编辑器策略 {editor_kind!r}")

    async def _dismiss_editor_overlays(self) -> None:
        """收拢会拦截编辑器点击的悬浮层（如草稿保存提示框）。

        2026-08-17 实测：输入内容后编辑器上方出现
        ``.editor-draft-tip-box`` 悬浮提示，拦截 pointer events 导致
        editor.click() 超时（原报错：draft-tip-box 拦截）。先尝试点掉
        其关闭按钮，否则强制隐藏，保证后续点击落在编辑器上。
        """

        try:
            dismissed = await self.page.evaluate(
                """() => {
                    const tips = Array.from(
                        document.querySelectorAll('.editor-draft-tip-box')
                    );
                    if (!tips.length) return true;
                    for (const tip of tips) {
                        const closeBtn = tip.querySelector(
                            '[class*="close" i], .el-icon-close, [aria-label*="关闭" i]'
                        );
                        if (closeBtn) { closeBtn.click(); continue; }
                        tip.style.display = 'none';
                    }
                    return true;
                }"""
            )
            if dismissed:
                await self.simulator.random_delay(0.3, 0.8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("ZOL 悬浮层收拢失败（继续尝试点击）: {}", exc)

    async def _click_editor(self, editor=None):
        """点击当前编辑器；旧 iframe/body 或遮罩拦截时只允许安全回退。"""

        await self._dismiss_editor_overlays()
        current, editor_kind = await self._resolve_content_editor()
        try:
            await current.click(timeout=5000)
            return current, editor_kind
        except Exception as first_error:
            if self._exception_means_browser_closed(first_error):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: ZOL 点击正文编辑器时页面已关闭"
                ) from first_error

            # 图片弹窗可能刚刚重建 TinyMCE iframe；重新解析一次，禁止把
            # 传入的旧 locator 当作当前正文节点继续 focus。
            fresh, fresh_kind = await self._resolve_content_editor()
            try:
                await fresh.click(timeout=5000)
                return fresh, fresh_kind
            except Exception as second_error:
                if self._exception_means_browser_closed(second_error):
                    raise BrowserLifecycleError(
                        "BROWSER_CONTEXT_CLOSED: ZOL 点击正文编辑器时页面已关闭"
                    ) from second_error

                # 已通过 visible + editable + 当前 frame/body 校验后，才允许
                # 用 focus 作为遮罩场景的有限回退；必须确认 activeElement，
                # 否则按不可交互失败，绝不吞掉点击校验。
                try:
                    await fresh.focus()
                    active = await fresh.evaluate(
                        "el => document.activeElement === el"
                    )
                except Exception as focus_error:
                    if self._exception_means_browser_closed(focus_error):
                        raise BrowserLifecycleError(
                            "BROWSER_CONTEXT_CLOSED: ZOL 聚焦正文编辑器时页面已关闭"
                        ) from focus_error
                    raise SelectorError(
                        "ZOL_CONTENT_EDITOR_NOT_INTERACTABLE: 当前正文节点无法聚焦"
                    ) from second_error
                if not active:
                    raise SelectorError(
                        "ZOL_CONTENT_EDITOR_NOT_INTERACTABLE: 当前正文节点未获得焦点"
                    ) from second_error
                logger.debug(
                    "ZOL 正文 click 被遮罩拦截，已在当前可编辑节点安全 focus: "
                    "first_error_type={}, second_error_type={}",
                    type(first_error).__name__,
                    type(second_error).__name__,
                )
                return fresh, fresh_kind

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文、插入图片，并验证文字和图片数量。"""
        editor, editor_kind = await self._resolve_content_editor()

        text_parts = [
            block.get("text", "").strip()
            for block in content_blocks
            if block.get("type") in ("text", "heading") and block.get("text", "").strip()
        ]
        expected_value = "\n\n".join(text_parts)
        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        uploaded_images = 0
        failed_images = []
        has_images = expected_images > 0

        # 没有图片时保留原 fill 快速路径；有图片时仍按原始块顺序输入，
        # 规范化只用于读回比较，不得改变冻结内容的写入文本或排版。
        if not has_images:
            await self._dismiss_editor_overlays()
            await editor.fill(expected_value)
        else:
            editor, editor_kind = await self._click_editor(editor)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            previous_kind = None
            for block in content_blocks:
                btype = block.get("type")
                block_text = (block.get("text") or "").strip()
                if btype in ("text", "heading") and block_text:
                    editor, editor_kind = await self._click_editor(editor)
                    await self.page.keyboard.press("Control+End")
                    if previous_kind == "text":
                        await self.page.keyboard.press("Enter")
                        await self.page.keyboard.press("Enter")
                    elif previous_kind == "image":
                        await self.page.keyboard.press("Enter")
                    block_lines = block_text.splitlines() or [block_text]
                    for index, line in enumerate(block_lines):
                        if line:
                            await self.page.keyboard.insert_text(line)
                        if index < len(block_lines) - 1:
                            await self.page.keyboard.press("Enter")
                    previous_kind = "text"
                elif btype == "image":
                    editor, editor_kind = await self._click_editor(editor)
                    await self.page.keyboard.press("Control+End")
                    image_file = next(
                        (
                            img.get("local_path")
                            for img in images
                            if img.get("position_index") == block.get("position")
                        ),
                        None,
                    ) or (images[0].get("local_path") if images else None)
                    if image_file:
                        upload_result = await self._upload_image(image_file) or {}
                        if upload_result.get("success"):
                            uploaded_images += 1
                        else:
                            failed_images.append({
                                "filename": Path(str(image_file)).name,
                                "error": safe_media_error(
                                    upload_result.get("error"),
                                    fallback="ZOL 图片上传失败",
                                ),
                                "error_code": (
                                    upload_result.get("error_code")
                                    or "ZOL_IMAGE_UPLOAD_FAILED"
                                ),
                            })
                    else:
                        failed_images.append({
                            "filename": "",
                            "error": "文章图片块没有对应本地文件",
                            "error_code": "ZOL_IMAGE_FILE_MISSING",
                        })
                    previous_kind = "image"
                # 块与块之间放慢节奏，降低风控敏感度
                await self.simulator.random_delay(1.5, 3.0)
            editor, editor_kind = await self._resolve_content_editor()
            await editor.evaluate(
                "el => el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}))"
            )

        # 图片弹窗可能替换 iframe/body 节点，必须重新解析编辑器再读取。
        editor, editor_kind = await self._resolve_content_editor()
        actual_text = await self._read_content_editor_text(editor, editor_kind)
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="ZOL",
            phase="输入及图片处理后",
        )
        if expected_images == 0:
            media_status = "not_required"
            media_error = None
            media_error_code = None
        elif uploaded_images == expected_images:
            media_status = "completed"
            media_error = None
            media_error_code = None
        elif uploaded_images == 0:
            media_status = "failed"
            media_error = f"{expected_images} 张图片全部上传失败"
            media_error_code = "ZOL_IMAGES_ALL_FAILED"
        else:
            media_status = "partial"
            media_error = f"{expected_images - uploaded_images} 张图片上传失败"
            media_error_code = "ZOL_IMAGES_PARTIAL"

        logger.info(
            "ZOL 正文验证成功: text_parts={}, images={}/{}, media_status={}",
            expected_count,
            uploaded_images,
            expected_images,
            media_status,
        )
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
            "media_error_code": media_error_code,
        }
    async def _editor_image_count(self) -> int:
        """只统计 ZOL 正文编辑器 iframe 内的图片。"""
        self._require_page_alive("ZOL 统计编辑器图片")
        for selector in ("#editor_ifr", "iframe.tox-edit-area__iframe"):
            try:
                iframe = self.page.locator(selector).first
                if await iframe.count() > 0:
                    return await self.page.frame_locator(selector).locator("body img").count()
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 统计图片时页面已关闭") from exc
                logger.debug("ZOL iframe 图片统计失败: selector={}, error={}", selector, exc)
        try:
            return await self.page.locator(
                ".mce-content-body img, #tinymce img, [contenteditable='true'] img"
            ).count()
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 统计图片时页面已关闭") from exc
            return 0

    async def _upload_image(self, image_path: str):
        """通过真实 ZOL 图片弹窗上传一张图片并验证 iframe 图片数量。"""
        image_name = Path(str(image_path)).name
        if not image_path or not os.path.isfile(str(image_path)):
            return {
                "success": False,
                "error_code": "ZOL_IMAGE_FILE_MISSING",
                "error": f"图片文件不存在: {image_name}",
            }

        before_count = await self._editor_image_count()
        try:
            self._require_page_alive("ZOL 打开图片弹窗")
            # 上一次上传即使已经插入图片，Ant Design 弹窗也可能仍在做关闭动画；
            # 先收拢残留弹窗，避免它拦截下一次工具栏点击。
            await self._close_image_modal()
            button = self.page.locator(self.IMAGE_BUTTON).first
            if await button.count() == 0 or not await button.is_visible():
                return {
                    "success": False,
                    "error_code": "ZOL_IMAGE_UPLOAD_CONTROL_NOT_FOUND",
                    "error": "未找到 ZOL 图片上传按钮",
                }
            await button.click(timeout=5000)
            modal = self.page.locator(self.IMAGE_MODAL).last
            await modal.wait_for(state="visible", timeout=5000)

            file_input = modal.locator(".local_upload input[type='file']").first
            if await file_input.count() == 0:
                file_input = modal.locator("input[type='file']").first
            if await file_input.count() == 0:
                return {
                    "success": False,
                    "error_code": "ZOL_IMAGE_UPLOAD_CONTROL_NOT_FOUND",
                    "error": "图片弹窗中未找到本地上传控件",
                }
            await file_input.set_input_files(str(Path(image_path).resolve()))
            await self.page.wait_for_timeout(300)

            insert_button = modal.get_by_role("button", name="插入编辑器", exact=True)
            if await insert_button.count() == 0:
                insert_button = modal.locator("button:has-text('插入编辑器')").first
            if await insert_button.count() == 0:
                return {
                    "success": False,
                    "error_code": "ZOL_IMAGE_UPLOAD_FAILED",
                    "error": "图片上传后未出现插入编辑器按钮",
                }
            ready_deadline = asyncio.get_running_loop().time() + 15
            while not await insert_button.is_enabled():
                if asyncio.get_running_loop().time() >= ready_deadline:
                    return {
                        "success": False,
                        "error_code": "ZOL_IMAGE_UPLOAD_FAILED",
                        "error": "图片上传后插入编辑器按钮在 15 秒内仍不可用",
                    }
                await asyncio.sleep(0.5)
            await insert_button.click(timeout=5000)

            deadline = asyncio.get_running_loop().time() + 15
            after_count = before_count
            while asyncio.get_running_loop().time() < deadline:
                after_count = await self._editor_image_count()
                if after_count > before_count:
                    await self._close_image_modal(modal)
                    logger.info(
                        "ZOL 图片上传并验证成功: filename={}, before={}, after={}",
                        image_name,
                        before_count,
                        after_count,
                    )
                    return {
                        "success": True,
                        "filename": image_name,
                        "before_count": before_count,
                        "after_count": after_count,
                    }
                await asyncio.sleep(0.5)
            return {
                "success": False,
                "error_code": "ZOL_IMAGE_UPLOAD_VERIFY_FAILED",
                "error": f"图片插入后编辑器数量未增加: before={before_count}, after={after_count}",
            }
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 图片上传时页面已关闭") from exc
            logger.warning(
                "ZOL 图片上传失败: filename={}, error_type={}",
                image_name,
                type(exc).__name__,
            )
            return {
                "success": False,
                "error_code": "ZOL_IMAGE_UPLOAD_FAILED",
                "error": safe_media_error(exc, fallback="ZOL 图片上传失败"),
            }
        finally:
            # 所有失败分支都必须关闭弹窗，否则后续图片和正文键盘输入会被遮罩截获。
            await self._close_image_modal()

    async def _close_image_modal(self, modal=None):
        """关闭 ZOL 图片弹窗；只处理弹窗，不触碰 Cookie/Profile。"""
        try:
            active = modal or self.page.locator(self.IMAGE_MODAL).last
            if await active.count() == 0 or not await active.is_visible():
                return
            close = active.locator("button[aria-label='Close'], .ant-modal-close").first
            if await close.count() > 0:
                await close.click(force=True)
            else:
                cancel = active.get_by_role("button", name="取 消", exact=True).first
                if await cancel.count() > 0:
                    await cancel.click(force=True)
            try:
                await active.wait_for(state="hidden", timeout=3000)
            except Exception:
                # 关闭动画偶尔不触发 hidden，下一次按钮点击前仍会再次执行兜底关闭。
                logger.debug("ZOL 图片弹窗未在预期时间内隐藏")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 关闭图片弹窗时页面已关闭") from exc
            logger.debug("ZOL 关闭图片弹窗失败: {}", exc)

    @staticmethod
    def _normalize_topic(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    @classmethod
    def _topic_queries(cls, topic: str = "", selection_query: str = "") -> list[str]:
        """生成有限的实时搜索词，不把整篇关键词 JSON 直接输入平台。"""
        queries = []

        def add(value: str):
            normalized = re.sub(r"\s+", " ", str(value or "").strip())
            if normalized and normalized not in queries:
                queries.append(normalized[:50])

        add(topic)
        raw = re.sub(r"[，、,。；;|/]+", " ", str(selection_query or ""))
        add(raw)
        for part in re.split(r"\s+", raw):
            if len(part.strip()) >= 2:
                add(part)
        return queries[:6]

    @classmethod
    def _pick_topic_candidate(
        cls,
        candidates: list[str],
        query: str,
    ) -> str:
        """只接受精确或唯一包含匹配，避免把搜索词本身当候选。"""
        unique = []
        seen = set()
        for candidate in candidates:
            value = re.sub(r"\s+", " ", str(candidate or "").strip())
            key = cls._normalize_topic(value)
            if value and key not in seen:
                seen.add(key)
                unique.append(value)
        normalized_query = cls._normalize_topic(query)
        exact = [item for item in unique if cls._normalize_topic(item) == normalized_query]
        if len(exact) == 1:
            return exact[0]
        contained = [
            item for item in unique
            if normalized_query
            and (
                normalized_query in cls._normalize_topic(item)
                or cls._normalize_topic(item) in normalized_query
            )
        ]
        return contained[0] if len(contained) == 1 else ""

    async def _close_topic_modal(self):
        try:
            modal = self.page.locator(self.TOPIC_MODAL).last
            if await modal.count() and await modal.is_visible():
                close = modal.locator("button[aria-label='Close'], .ant-modal-close").first
                if await close.count():
                    await close.click(force=True)
                else:
                    cancel = modal.get_by_role("button", name="取 消", exact=True).first
                    if await cancel.count():
                        await cancel.click(force=True)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 关闭话题弹窗时页面已关闭") from exc

    async def _topic_candidates(self, modal) -> list[str]:
        items = modal.locator(".list .item .title")
        candidates = []
        for index in range(await items.count()):
            value = re.sub(r"\s+", " ", (await items.nth(index).inner_text()).strip())
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    async def _verify_topic_selected(self, expected: str) -> bool:
        tags = self.page.locator("span.ant-tag, .ant-tag")
        target = self._normalize_topic(expected)
        for index in range(await tags.count()):
            try:
                if not await tags.nth(index).is_visible():
                    continue
                actual = self._normalize_topic(await tags.nth(index).inner_text())
                if actual == target:
                    return True
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 验证话题时页面已关闭") from exc
        return False

    async def select_topic(self, topic: str = "", community: str = "",
                           selection_query: str = "", selection_override: dict = None):
        """实时搜索 ZOL 话题并验证编辑器中的真实标签。"""
        self._require_page_alive("ZOL 选择话题")
        override = selection_override or {}
        explicit_topic = str(override.get("topic") or topic or "").strip()
        queries = self._topic_queries(explicit_topic, selection_query)
        if not queries:
            return {"success": True, "selection": {}, "selection_status": "not_required"}

        button = self.page.locator(self.TOPIC_BUTTON).filter(has_text="选择话题").first
        if await button.count() == 0 or not await button.is_visible():
            return {
                "success": False,
                "needs_selection": True,
                "error_code": "ZOL_SELECTION_CONTROL_NOT_FOUND",
                "error": "未找到 ZOL 选择话题按钮",
                "selection": {"kind": "topic", "query": queries[0], "candidates": []},
            }

        all_candidates = []
        try:
            await button.click(timeout=5000)
            modal = self.page.locator(self.TOPIC_MODAL).last
            await modal.wait_for(state="visible", timeout=5000)
            search = modal.locator("input[placeholder='搜索话题']").first
            if await search.count() == 0:
                return {
                    "success": False,
                    "needs_selection": True,
                    "error_code": "ZOL_SELECTION_CONTROL_NOT_FOUND",
                    "error": "ZOL 话题弹窗中未找到搜索框",
                    "selection": {"kind": "topic", "query": queries[0], "candidates": []},
                }

            for query in queries:
                await search.fill(query)
                await self.page.wait_for_timeout(800)
                candidates = await self._topic_candidates(modal)
                for candidate in candidates:
                    if candidate not in all_candidates:
                        all_candidates.append(candidate)
                selected = self._pick_topic_candidate(candidates, query)
                if not selected:
                    continue

                item = modal.locator(".list .item .title").filter(has_text=selected).first
                if await item.count() == 0:
                    continue
                await item.click(timeout=5000)
                confirm = modal.get_by_role("button", name="确 定", exact=True).first
                if await confirm.count() == 0:
                    return {
                        "success": False,
                        "needs_selection": True,
                        "error_code": "ZOL_SELECTION_CONTROL_NOT_FOUND",
                        "error": "ZOL 话题弹窗中未找到确定按钮",
                        "selection": {"kind": "topic", "query": query, "candidates": all_candidates},
                    }
                await confirm.click(timeout=5000)
                await modal.wait_for(state="hidden", timeout=5000)
                if not await self._verify_topic_selected(selected):
                    return {
                        "success": False,
                        "needs_selection": True,
                        "error_code": "ZOL_SELECTION_VERIFY_FAILED",
                        "error": f"ZOL 话题已点击但页面未出现真实标签: {selected}",
                        "selection": {"kind": "topic", "query": query, "candidates": all_candidates},
                    }
                logger.info("ZOL 话题选择并验证成功: query={}, topic={}", query, selected)
                return {
                    "success": True,
                    "selection": {"kind": "topic", "topic": selected, "query": query},
                    "selection_status": "completed",
                }
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 话题选择时页面已关闭") from exc
            logger.warning("ZOL 话题选择失败: error={}", exc)
            await self._close_topic_modal()
            return {
                "success": False,
                "needs_selection": True,
                "error_code": "ZOL_SELECTION_VERIFY_FAILED",
                "error": f"ZOL 话题选择流程失败: {exc}",
                "selection": {"kind": "topic", "query": queries[0], "candidates": all_candidates},
            }

        await self._close_topic_modal()
        error_code = "ZOL_SELECTION_NO_CANDIDATE" if not all_candidates else "ZOL_SELECTION_AMBIGUOUS"
        return {
            "success": False,
            "needs_selection": True,
            "error_code": error_code,
            "error": (
                "ZOL 未找到可自动确认的话题候选"
                if not all_candidates
                else "ZOL 话题候选不唯一，等待人工选择"
            ),
            "selection": {
                "kind": "topic",
                "query": queries[0],
                "candidates": all_candidates[:30],
            },
        }

    async def save_draft(self, title: str = "") -> str:
        """保存草稿并在草稿箱验证真实标题。"""
        self._require_page_alive("ZOL 保存草稿")
        current_url = self.page.url or ""
        if not self._is_blog_editor_url(current_url):
            logger.error("ZOL 保存草稿失败：当前页面不是博客编辑器，url={}", self.page.url)
            return ""
        if self._host(current_url) == self.CREATOR_HOST:
            draft_selectors = [
                ".foot-item:has-text('存草稿')",
                ".foot-item-text:has-text('存草稿')",
                "button:has-text('存草稿')",
                "[role='button']:has-text('存草稿')",
                "button:has-text('保存草稿')",
            ]
            draft_url = self.platform_cfg.get(
                "draft_url", "https://post.zol.com.cn/v2/manage/works/draft"
            )
        else:
            # 兼容旧博客编辑器。
            draft_selectors = [
                "button:has-text('保存草稿')",
                "input[value='保存草稿']",
                ".draft-btn",
                "#save_draft",
                "a:has-text('草稿')",
            ]
            draft_url = self.platform_cfg.get(
                "draft_url", "https://blog.zol.com.cn/post.php?act=draft"
            )

        for sel in draft_selectors:
            try:
                await self.page.click(sel, timeout=3000)
                await self.simulator.random_delay(2, 5)
                await self.page.goto(draft_url, wait_until="domcontentloaded", timeout=15000)
                await self.simulator.random_delay(2, 4)
                self._require_page_alive("ZOL 验证草稿箱")
                draft_page_url = self.page.url or ""
                creator_draft = (
                    self._host(draft_page_url) == self.CREATOR_HOST
                    and "/v2/manage/works/draft" in draft_page_url
                )
                legacy_draft = (
                    self._host(draft_page_url) == self.BLOG_HOST
                    and "post.php" in draft_page_url
                )
                if not (creator_draft or legacy_draft):
                    raise PlatformAccessError(
                        f"ZOL_DRAFT_ROUTE_ERROR: 草稿箱跳转到非博客页面，当前 URL: {draft_page_url}"
                    )
                keyword = (title or "")[:30]
                found = await self.page.evaluate(
                    "(kw) => (document.body.innerText || '').includes(kw)", keyword
                ) if keyword else False
                if found:
                    logger.info("ZOL 草稿验证成功: {}", keyword)
                    return draft_page_url
                logger.warning("ZOL 草稿箱未找到标题: {}", keyword)
                return ""
            except Exception as exc:
                if self._exception_means_browser_closed(exc):
                    raise BrowserLifecycleError("BROWSER_CONTEXT_CLOSED: ZOL 保存草稿时页面已关闭") from exc
                logger.debug("ZOL 保存草稿候选按钮失败: selector={}, error={}", sel, exc)
                continue

        # 未匹配到保存按钮或无成功信号 → 如实返回空串，交由 publish() 判定为失败
        return ""
