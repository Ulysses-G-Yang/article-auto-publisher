"""中关村在线（ZOL）博客平台自动化"""
import asyncio

from platforms.base import BasePlatform
from human.simulator import HumanSimulator


class ZOLPlatform(BasePlatform):
    platform_name = "zol"

    async def check_login(self) -> bool:
        """检查是否已登录 ZOL——导航到个人中心用已保存的 Cookie 验证

        注意：ZOL 登录后写入的 cookie 是 `last_userid` / `lv`（不是 zol_userid），
        旧逻辑误判导致已登录被识别成未登录。现以 last_userid 为权威信号。
        """
        try:
            await self.page.goto("https://my.zol.com.cn/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
            is_logged_in = await self.page.evaluate("""
                () => {
                    // 检查页面是否有登录后的用户信息
                    const userName = document.querySelector('.user-name, .header-user, .login-info, .nickname');
                    const logoutLink = document.querySelector('a[href*="logout"]');
                    if (userName || logoutLink) return true;
                    // 检查 Cookie 中的登录标记（ZOL 实际写入 last_userid / lv / zol_userid）
                    const c = document.cookie || '';
                    return c.includes('last_userid') || c.includes('lv') || c.includes('zol_userid') || c.includes('zol_sid');
                }
            """)
            return is_logged_in
        except Exception:
            return False

    async def _check_login_current_page(self) -> bool:
        """检查当前页面是否已登录（不跳转，用于登录等待循环）"""
        try:
            is_logged_in = await self.page.evaluate("""
                () => {
                    const url = window.location.href;
                    if (url.includes('my.zol.com.cn') && !url.includes('service.zol.com.cn/user/login')) {
                        return true;
                    }
                    if (url.includes('blog.zol.com.cn')) {
                        const userName = document.querySelector('.user-name, .header-user, .login-info, .nickname');
                        const logoutLink = document.querySelector('a[href*="logout"]');
                        if (userName || logoutLink) return true;
                    }
                    const c = document.cookie || '';
                    return c.includes('last_userid') || c.includes('lv') || c.includes('zol_userid') || c.includes('zol_sid');
                }
            """)
            return is_logged_in
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

        print("\n" + "=" * 60)
        print("请在打开的浏览器中手动登录 中关村在线 (ZOL)")
        print("可以用手机扫码或输入账号密码")
        print("登录成功后 Cookie 将自动保存，下次无需重复登录")
        print("=" * 60 + "\n")

        # 等待用户完成登录——不调 evaluate 避免触发刷新
        max_wait = 120  # 2分钟
        for i in range(max_wait):
            # 用 locator 检测页面跳转（说明登录成功跳到首页）
            try:
                url = self.page.url
                # 登录成功 = 离开登录页
                if "service.zol.com.cn/user/login" not in url and "login" not in url.lower():
                    # 进一步验证有用户信息
                    has_user = await self.page.locator(".user-name, .header-user, [class*='user-info']").count()
                    if has_user > 0:
                        await asyncio.sleep(2)
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
                        print("ZOL 登录成功！")
                        return
            except Exception:
                pass
            await asyncio.sleep(3)

        raise TimeoutError("登录超时，请重试")

    async def navigate_to_editor(self):
        """导航到博客编辑器"""
        await self.page.goto("https://blog.zol.com.cn/post.php?act=add", wait_until="domcontentloaded")
        await self.simulator.random_delay(2, 4)
        await self.simulator.simulate_scroll(self.page, scroll_times=2)

    async def fill_title(self, title: str):
        """填写博客标题"""
        # ZOL 标题输入框 - 多种可能的选择器
        selectors = [
            "#title",
            "input[name='title']",
            ".title-input input",
            ".blog-title input",
        ]
        for sel in selectors:
            try:
                element = await self.page.wait_for_selector(sel, timeout=3000, state="visible")
                if element:
                    await self.simulator.click_element(self.page, sel)
                    await self.simulator.type_text(self.page, sel, title[:50])
                    return
            except Exception:
                continue

        # 兜底：直接找输入框
        await self.simulator.type_text(self.page, "input[type='text']", title[:50])

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文内容"""
        # ZOL 使用 KindEditor 或类似编辑器
        # 尝试多种方式定位编辑器
        editor_frame = None
        editor_selectors = [
            "iframe.ke-edit-iframe",
            ".ke-container iframe",
            "#content_ifr",
            "iframe[id*='content']",
        ]

        for sel in editor_selectors:
            try:
                frame_element = await self.page.wait_for_selector(sel, timeout=3000)
                if frame_element:
                    editor_frame = await frame_element.content_frame()
                    break
            except Exception:
                continue

        content_area = editor_frame or self.page
        body_selector = "body" if editor_frame else (
            "div.ke-edit, div.ke-content, .editor-content, #content, textarea[name='content']"
        )

        # 逐块插入内容
        for block in content_blocks:
            if block.get("type") == "text" and block.get("text"):
                text = block["text"]
                para = f"<p>{text}</p>"

                if editor_frame:
                    await self.simulator.random_delay()
                    # 使用参数传递避免 JS 注入
                    await editor_frame.evaluate(
                        "(html) => { document.body.innerHTML += html; }",
                        para,
                    )
                else:
                    # 尝试粘贴或输入
                    try:
                        element = await content_area.wait_for_selector(body_selector, timeout=2000)
                        if element:
                            await element.click()
                            await self.simulator.paste_text(content_area, text)
                    except Exception:
                        pass

            elif block.get("type") == "image":
                # 插入图片 - ZOL 编辑器通常有图片上传按钮
                image_file = None
                for img in images:
                    if img.get("position_index") == block.get("position"):
                        image_file = img.get("local_path")
                        break
                if not image_file and images:
                    image_file = images[0].get("local_path") if images else None

                if image_file:
                    await self._upload_image(image_file)

            elif block.get("type") == "heading" and block.get("text"):
                text = block["text"]
                if editor_frame:
                    await editor_frame.evaluate(
                        "(html) => { document.body.innerHTML += html; }",
                        f"<h3>{text}</h3>",
                    )

            await self.simulator.random_delay(0.3, 1.0)

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
            print(f"图片上传失败: {e}")

    async def select_topic(self, topic: str):
        """选择博客分类"""
        try:
            # ZOL 博客分类选择
            category_selectors = [
                "#category", "#catselect", "select[name='cate_id']",
                ".category-select", "select[name='category']",
            ]
            for sel in category_selectors:
                try:
                    select = await self.page.wait_for_selector(sel, timeout=3000)
                    if select:
                        await select.select_option(label=topic)
                        await self.simulator.random_delay()
                        return
                except Exception:
                    continue

            # 搜索关键词匹配的下拉选项
            options = await self.page.query_selector_all("select option")
            for opt in options:
                text = await opt.text_content()
                if topic in text:
                    value = await opt.get_attribute("value")
                    parent = await opt.evaluate("el => el.parentElement.value = arguments[0]", value)
                    await self.simulator.random_delay()
                    return

        except Exception as e:
            print(f"话题选择失败: {e}")

        # 填写标签（备选方案）
        try:
            tag_input = await self.page.wait_for_selector(
                "input[name='tags'], #tags, .tag-input input", timeout=3000
            )
            if tag_input:
                await self.simulator.type_text(self.page, "input[name='tags'], #tags, .tag-input input", topic)
        except Exception:
            pass

    async def save_draft(self, title: str = "") -> str:
        """保存草稿"""
        # ZOL 保存草稿按钮
        draft_selectors = [
            "button:has-text('保存草稿')",
            "input[value='保存��稿']",
            ".draft-btn",
            "#save_draft",
            "a:has-text('草稿')",
        ]

        for sel in draft_selectors:
            try:
                await self.page.click(sel, timeout=3000)
                await self.simulator.random_delay(2, 5)

                # 检查是否成功跳转
                current_url = self.page.url
                if "draft" in current_url.lower() or "post" in current_url.lower():
                    return current_url

                # 检查页面提示
                success = await self.page.evaluate("""
                    () => {
                        const msgs = document.querySelectorAll('.success, .msg-success, .alert-success');
                        return msgs.length > 0;
                    }
                """)
                if success:
                    return current_url
                break
            except Exception:
                continue

        # 未匹配到保存按钮或无成功信号 → 如实返回空串，交由 publish() 判定为失败
        return ""
