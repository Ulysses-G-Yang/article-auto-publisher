"""中关村在线（ZOL）博客平台自动化"""
import asyncio
from loguru import logger

from platforms.base import BasePlatform
from human.simulator import HumanSimulator


class ZOLPlatform(BasePlatform):
    platform_name = "zol"

    async def check_login(self) -> bool:
        """检查是否能进入真实 ZOL 博客编辑器。

        个人中心 Cookie 存在并不等于博客编辑器可用；过期或域不完整的
        Cookie 可能仍能打开 ``my.zol.com.cn``，但编辑器会重定向到论坛登录页。
        因此这里同时验证编辑器 URL、编辑器 DOM、页面登录提示和 HttpOnly Cookie。
        """
        try:
            await self.page.goto(
                "https://blog.zol.com.cn/post.php?act=add",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(3)
            page_state = await self.page.evaluate("""
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                    };
                    return {
                        url: window.location.href,
                        hasUser: visible(document.querySelector('.user-name, .header-user, .login-info, .nickname')),
                        hasLogout: visible(document.querySelector('a[href*="logout"]')),
                        hasLoginForm: [
                            '#J_LoginUser', '#J_LoginPsw', '#J_LoginBtn',
                        'a[href*="login"]', '.login-btn', '.login-entry'
                        ].some(selector => visible(document.querySelector(selector))),
                    };
                }
            """)
            editor_probe = await self.page.locator(
                "#title, input[name='title'], .title-input input, .blog-title input, "
                "textarea[name='content'], iframe.ke-edit-iframe, .ke-container iframe, "
                "#content_ifr, iframe[id*='content'], [contenteditable='true']"
            ).count()
            current_url = page_state.get("url") or self.page.url or ""
            if page_state.get("hasLoginForm"):
                return False
            if "blog.zol.com.cn" not in current_url or editor_probe == 0:
                return False
            cookie_names = set()
            if self.context:
                cookies = await self.context.cookies([
                    "https://my.zol.com.cn/",
                    "https://blog.zol.com.cn/",
                ])
                cookie_names = {item.get("name", "") for item in cookies}
            cookie_logged_in = bool(cookie_names.intersection({
                "last_userid", "lv", "zol_userid", "zol_sid",
            }))
            return bool(
                editor_probe > 0
                and (page_state.get("hasUser") or page_state.get("hasLogout") or cookie_logged_in)
            )
        except Exception as exc:
            logger.warning("ZOL 登录态检测失败: url={}, error={}", getattr(self.page, "url", ""), exc)
            return False

    async def _check_login_current_page(self) -> bool:
        """检查当前页面是否已登录（不跳转，用于登录等待循环）"""
        try:
            page_state = await self.page.evaluate("""
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                    };
                    return {
                        url: window.location.href,
                        hasUser: visible(document.querySelector('.user-name, .header-user, .login-info, .nickname')),
                        hasLogout: visible(document.querySelector('a[href*="logout"]')),
                        hasLoginForm: [
                            '#J_LoginUser', '#J_LoginPsw', '#J_LoginBtn',
                            'a[href*="login"]', '.login-btn', '.login-entry'
                        ].some(selector => visible(document.querySelector(selector))),
                    };
                }
            """)
            if page_state.get("hasLoginForm"):
                return False
            cookie_names = set()
            if self.context:
                cookies = await self.context.cookies([
                    "https://my.zol.com.cn/",
                    "https://blog.zol.com.cn/",
                ])
                cookie_names = {item.get("name", "") for item in cookies}
            return bool(page_state.get("hasUser") or page_state.get("hasLogout") or cookie_names.intersection({
                "last_userid", "lv", "zol_userid", "zol_sid",
            }))
        except Exception:
            return False

    async def login(self):
        """打开登录页，等待用户手动完成登录"""
        await self.page.goto("https://my.zol.com.cn/", wait_until="domcontentloaded")
        await self.simulator.random_delay(1, 2)

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

        # 等待用户完成登录——不调 evaluate 避免触发刷新
        max_wait = 120  # 2分钟
        for i in range(max_wait):
            # 用 locator 检测页面跳转（说明登录成功跳到首页）
            try:
                url = self.page.url
                if await self._check_login_current_page():
                    await self.page.evaluate("""
                        () => {
                            const hint = document.getElementById('wb-login-hint');
                            if (hint) {
                                hint.style.background = '#198754';
                                hint.innerHTML = '登录成功！<br><small>3秒后关闭此窗口...</small>';
                            }
                        }
                    """)
                    await asyncio.sleep(3)
                    logger.info("ZOL 登录成功")
                    return
            except Exception:
                pass
            await asyncio.sleep(3)

        raise TimeoutError("登录超时，请重试")

    async def navigate_to_editor(self):
        """导航到博客编辑器"""
        await self.page.goto("https://blog.zol.com.cn/post.php?act=add", wait_until="domcontentloaded")
        await self.simulator.random_delay(2, 4)
        if "blog.zol.com.cn" not in (self.page.url or ""):
            raise RuntimeError(f"ZOL 编辑器跳转失败，当前 URL: {self.page.url}")
        probe = await self.page.locator(
            "#title, input[name='title'], .title-input input, .blog-title input, "
            "textarea[name='content'], iframe.ke-edit-iframe, .ke-container iframe, "
            "#content_ifr, iframe[id*='content'], [contenteditable='true']"
        ).count()
        if probe == 0:
            raise RuntimeError(f"ZOL 编辑器结构探测失败，当前 URL: {self.page.url}")
        await self.simulator.simulate_scroll(self.page, scroll_times=2)

    async def fill_title(self, title: str):
        """填写并验证博客标题，不再静默吞掉定位/输入失败。"""
        expected = (title or "")[:50]
        selectors = [
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
                if actual.strip() != expected.strip():
                    raise RuntimeError(
                        f"ZOL 标题验证失败: expected={expected!r}, actual={actual!r}"
                    )
                logger.info("ZOL 标题输入并验证成功: {}", expected[:30])
                return
            except Exception as exc:
                logger.debug("ZOL 标题候选选择器失败: selector={}, error={}", selector, exc)

        raise RuntimeError("ZOL 标题输入框未找到或输入后校验失败")

    async def fill_content(self, content_blocks: list, images: list):
        """支持 iframe、textarea、contenteditable 三类正文编辑器并验证文本。"""
        iframe_selectors = [
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
            raise RuntimeError(f"ZOL 正文编辑器未找到，当前 URL: {self.page.url}")

        text_parts = [
            block.get("text", "").strip()
            for block in content_blocks
            if block.get("type") in ("text", "heading") and block.get("text", "").strip()
        ]
        tag_name = await editor.evaluate("el => el.tagName.toLowerCase()")
        if tag_name == "textarea":
            expected_value = "\n\n".join(text_parts)
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
            raise RuntimeError(f"ZOL 正文输入后验证失败，缺少文本片段: {missing}")
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

        raise RuntimeError(f"ZOL 分类/标签未找到，无法选择: {topic}")

    async def save_draft(self, title: str = "") -> str:
        """保存草稿并在草稿箱验证真实标题。"""
        if "blog.zol.com.cn" not in (self.page.url or "") or "post.php" not in (self.page.url or ""):
            logger.error("ZOL 保存草稿失败：当前页面不是博客编辑器，url={}", self.page.url)
            return ""
        # ZOL 保存草稿按钮
        draft_selectors = [
            "button:has-text('保存草稿')",
            "input[value='保存草稿']",
            ".draft-btn",
            "#save_draft",
            "a:has-text('草稿')",
        ]

        for sel in draft_selectors:
            try:
                await self.page.click(sel, timeout=3000)
                await self.simulator.random_delay(2, 5)
                draft_url = self.platform_cfg.get("draft_url", "https://blog.zol.com.cn/post.php?act=draft")
                await self.page.goto(draft_url, wait_until="domcontentloaded", timeout=15000)
                await self.simulator.random_delay(2, 4)
                keyword = (title or "")[:30]
                found = await self.page.evaluate(
                    "(kw) => (document.body.innerText || '').includes(kw)", keyword
                ) if keyword else False
                if found:
                    logger.info("ZOL 草稿验证成功: {}", keyword)
                    return self.page.url
                logger.warning("ZOL 草稿箱未找到标题: {}", keyword)
                return ""
            except Exception as exc:
                logger.debug("ZOL 保存草稿候选按钮失败: selector={}, error={}", sel, exc)
                continue

        # 未匹配到保存按钮或无成功信号 → 如实返回空串，交由 publish() 判定为失败
        return ""
