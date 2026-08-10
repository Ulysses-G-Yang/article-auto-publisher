"""中关村在线（ZOL）创作者中心自动化"""
import asyncio
import re
import time
from urllib.parse import urlparse
from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAccessError,
    SelectorError,
)
from human.simulator import HumanSimulator


class ZOLPlatform(BasePlatform):
    platform_name = "zol"

    CREATOR_HOST = "post.zol.com.cn"
    BLOG_HOST = "blog.zol.com.cn"  # 旧博客入口，仅用于识别历史重定向
    FORUM_HOST = "bbs.zol.com.cn"
    LOGIN_HOST = "service.zol.com.cn"
    CRITICAL_COOKIES = {"last_userid", "lv", "zol_userid", "zol_sid"}

    def __init__(self):
        super().__init__()
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

    async def check_login(self) -> bool:
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
            if self._host(editor_url) == self.CREATOR_HOST:
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

    async def fill_content(self, content_blocks: list, images: list):
        """支持 iframe、textarea、contenteditable 三类正文编辑器并验证文本。"""
        self._require_page_alive("ZOL 填写正文")
        iframe_selectors = [
            ".tox-edit-area iframe",
            "iframe.tox-edit-area__iframe",
            "iframe[title='Rich Text Area']",
            "iframe.ke-edit-iframe",
            ".ke-container iframe",
            "#content_ifr",
            "iframe[id*='content']",
        ]
        editor_frame = None
        editor = None

        for selector in iframe_selectors:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                handle = await locator.element_handle()
                if handle:
                    editor_frame = await handle.content_frame()
                    if editor_frame:
                        editor = editor_frame.locator("body").first
                        break
            except Exception as exc:
                logger.debug("ZOL iframe 探测失败: selector={}, error={}", selector, exc)

        if editor is None:
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
                locator = self.page.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        editor = locator
                        break
                except Exception as exc:
                    logger.debug("ZOL 正文候选选择器失败: selector={}, error={}", selector, exc)

        if editor is None:
            raise SelectorError(f"ZOL_CONTENT_SELECTOR_ERROR: 正文编辑器未找到，当前 URL: {self.page.url}")

        text_parts = [
            block.get("text", "").strip()
            for block in content_blocks
            if block.get("type") in ("text", "heading") and block.get("text", "").strip()
        ]
        tag_name = await editor.evaluate("el => el.tagName.toLowerCase()")
        expected_value = "\n\n".join(text_parts)
        # TinyMCE 的正文实际位于跨域 iframe 内的 body。对 textarea、
        # contenteditable 和 iframe body 统一使用 locator.fill，避免把
        # page.keyboard 的输入错误地发送到标题输入框或父页面。
        if tag_name in ("textarea", "body"):
            await editor.fill(expected_value)
        elif not any(block.get("type") == "image" for block in content_blocks):
            await editor.fill(expected_value)
        else:
            await editor.click()
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            first_text = True
            for block in content_blocks:
                btype = block.get("type")
                block_text = (block.get("text") or "").strip()
                if btype in ("text", "heading") and block_text:
                    if not first_text:
                        await self.page.keyboard.press("Enter")
                        await self.page.keyboard.press("Enter")
                    for index, line in enumerate(block_text.splitlines() or [block_text]):
                        if line:
                            await self.page.keyboard.insert_text(line)
                        if index < len(block_text.splitlines()) - 1:
                            await self.page.keyboard.press("Enter")
                    first_text = False
                elif btype == "image":
                    image_file = next(
                        (
                            img.get("local_path")
                            for img in images
                            if img.get("position_index") == block.get("position")
                        ),
                        None,
                    ) or (images[0].get("local_path") if images else None)
                    if image_file:
                        await self._upload_image(image_file)
                await self.simulator.random_delay(0.3, 1.0)
            await editor.evaluate(
                "el => el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}))"
            )

        if tag_name == "textarea":
            actual_text = await editor.input_value()
        else:
            actual_text = await editor.inner_text()
        missing = [part[:30] for part in text_parts if part not in actual_text]
        if missing:
            raise SelectorError(f"ZOL_CONTENT_VALIDATION_ERROR: 正文输入后验证失败，缺少文本片段: {missing}")
        logger.info("ZOL 正文输入并验证成功: {} 个文本段落", len(text_parts))

    async def _upload_image(self, image_path: str):
        """上传图片到 ZOL 编辑器"""
        try:
            # 点击图片上传按钮
            img_btn_selectors = [
                ".ke-toolbar-icon[title*='图片']",
                ".ke-icon-image",
                "a[title='插入图片']",
                "button[title*='图片']",
            ]
            for sel in img_btn_selectors:
                try:
                    await self.page.click(sel, timeout=3000)
                    await self.simulator.random_delay(0.5, 1.5)
                    break
                except Exception:
                    continue

            # 查找文件上传输入框
            upload_selectors = [
                "input[type='file']",
                ".ke-upload-area input[type='file']",
                "#ke-upload-file",
            ]
            for sel in upload_selectors:
                try:
                    file_input = await self.page.wait_for_selector(sel, timeout=3000)
                    if file_input:
                        await file_input.set_input_files(image_path)
                        await self.simulator.random_delay(2, 4)
                        break
                except Exception:
                    continue

            # 点击确认上传按钮
            confirm_selectors = [
                ".ke-dialog-btn-ok",
                ".upload-btn-confirm",
                "button:has-text('确定')",
                "input[value='确定']",
            ]
            for sel in confirm_selectors:
                try:
                    await self.page.click(sel, timeout=2000)
                    await self.simulator.random_delay(1, 2)
                    break
                except Exception:
                    continue

        except Exception as e:
            logger.warning("ZOL 图片上传失败: {}", e)

    async def select_topic(self, topic: str = "", community: str = "",
                           selection_query: str = "", selection_override: dict = None):
        """选择并验证 ZOL 分类/标签。"""
        self._require_page_alive("ZOL 选择分类或标签")
        if not topic:
            return {"success": True, "selection": {}}

        category_selectors = [
            "#category", "#catselect", "select[name='cate_id']",
            ".category-select", "select[name='category']",
        ]
        for selector in category_selectors:
            select = self.page.locator(selector).first
            try:
                if await select.count() == 0 or not await select.is_visible():
                    continue
                await select.select_option(label=topic)
                selected = await select.locator("option:checked").inner_text()
                if topic not in selected:
                    raise RuntimeError(f"ZOL 分类验证失败: expected={topic}, actual={selected}")
                logger.info("ZOL 分类选择并验证成功: {}", topic)
                return {"success": True, "selection": {"topic": selected}}
            except Exception as exc:
                logger.debug("ZOL 分类选择器失败: selector={}, error={}", selector, exc)

        tag_selector = "input[name='tags'], #tags, .tag-input input"
        tag_input = self.page.locator(tag_selector).first
        try:
            if await tag_input.count() > 0 and await tag_input.is_visible():
                await tag_input.fill(topic)
                actual = await tag_input.input_value()
                if topic not in actual:
                    raise RuntimeError(f"ZOL 标签验证失败: expected={topic}, actual={actual}")
                logger.info("ZOL 标签输入并验证成功: {}", topic)
                return {"success": True, "selection": {"topic": topic}}
        except Exception as exc:
            logger.debug("ZOL 标签输入失败: {}", exc)

        raise SelectorError(f"ZOL_TOPIC_SELECTOR_ERROR: 分类/标签未找到，无法选择: {topic}")

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
