"""小黑盒（Xiaoheihe）平台自动化"""
import asyncio

from platforms.base import BasePlatform
from human.simulator import HumanSimulator


class XiaoheihePlatform(BasePlatform):
    platform_name = "xiaoheihe"

    # 真实选择器（经 DOM 抓取确认，非猜测）：
    #  - 登录入口按钮：右上角头像 `button.nav-user-trigger`
    #  - 二维码容器：`div.qrcode-in-card`
    #  - 头像：`img.nav-user-avatar`
    # 登录态判断依据：未登录时头像是默认游客图（地址含 `icon_83.5`），
    # 登录后变为用户真实头像。这是最可靠的信号，不依赖会变的 class。
    LOGIN_TRIGGER = "button.nav-user-trigger"
    QR_SELECTOR = "div.qrcode-in-card"
    GUEST_AVATAR_MARKER = "icon_83.5"  # 小黑盒默认游客头像地址特征

    # ---- 编辑器真实选择器（2026-07-31 浏览器实时 DOM 抓取确认）----
    PUBLISH_BTN = ".publish-btn"                                  # 首页右上「发布内容」
    TAB_ARTICLE = "div.slide-tab__tab-label"                       # 类型面板：发布图文/发布文章/发布视频
    TITLE_FIELD = ".editor-title__container [contenteditable='true'], .editor-title__container .ProseMirror"
    BODY_FIELD = ".article__edit-content--inner [contenteditable='true'], .article__edit-content--inner .ProseMirror"
    SAVE_DRAFT_BTN = "button.editor-publish__save-draft"           # 保存草稿
    DRAFT_BOX_BTN = "button.editor-publish__btn.sub-btn.margin-left"  # 草稿箱
    PUBLISH_NOW_BTN = "button.editor-publish__btn.main-btn"        # 发布

    async def _is_logged_in_dom(self) -> bool:
        """读取当前页 DOM + 登录态 cookie 判断登录态（不依赖头像图片，避免误判）

        关键修正：之前用「头像图片地址是否含 icon_83.5 默认游客图」判断登录态，
        但用户登录后若未设自定义头像，小黑盒返回的就是默认图，导致已登录被误判为游客。
        现改为以「登录态 cookie」为权威信号：heybox_id / nickname / pkey 是登录后
        才有且非 httponly 的 cookie，游客态不存在这些。
        """
        # 1. 权威信号：登录态 cookie（可在 JS 中读取，游客态不会有）
        try:
            cookie_signal = await self.page.evaluate("""() => {
                const c = document.cookie || '';
                return c.includes('heybox_id=') || c.includes('nickname=') || c.includes('pkey=');
            }""")
            if cookie_signal:
                return True
        except Exception:
            pass
        # 2. 兜底：头像为非默认游客图（已换成用户真实头像）→ 已登录
        try:
            avatar = self.page.locator(
                "img.nav-user-avatar, img[class*='avatar'], .nav-user img"
            ).first
            if await avatar.count() > 0:
                src = await avatar.get_attribute("src") or ""
                if self.GUEST_AVATAR_MARKER in src:
                    return False
                if src:
                    return True
        except Exception:
            pass
        # 3. 兜底：存在用户名/昵称元素也算已登录
        try:
            if await self.page.locator(".nav-user-name, .user-name, [class*=nickname]").count() > 0:
                return True
        except Exception:
            pass
        return False

    async def check_login(self) -> bool:
        """检查小黑盒登录状态——以登录态 cookie 为权威信号。

        注意：x_xhh_tokenid 是未登录也存在的埋点 cookie，不能用作登录态判断。
        游客态不会有 heybox_id / nickname / pkey 等登录 cookie。
        """
        try:
            await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded", timeout=15000)
            # 多等一会儿，确保 SPA 完成 hydration、cookie 已注入页面上下文
            await asyncio.sleep(4)
            return await self._is_logged_in_dom()
        except Exception:
            return False

    async def login(self):
        """打开首页，点击右上角头像弹出二维码，等待扫码登录"""
        # 1. 访问首页
        await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # 2. 点击右上角头像，弹出登录二维码面板
        try:
            await self.page.click(self.LOGIN_TRIGGER, timeout=8000)
        except Exception as e:
            print(f"点击用户入口失败，尝试重新打开: {e}")
            await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            try:
                await self.page.click(self.LOGIN_TRIGGER, timeout=8000)
            except Exception as e2:
                print(f"仍未找到登录入口: {e2}")

        await asyncio.sleep(2)

        # 3. 确认二维码已出现
        has_qr = await self.page.locator(self.QR_SELECTOR).count()
        if has_qr == 0:
            print("警告：未检测到二维码，请手动点击右上角头像打开扫码面板")

        # 4. 显示提示
        await self.page.evaluate("""
            () => {
                const div = document.createElement('div');
                div.id = 'wb-login-hint';
                div.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#0d6efd;color:#fff;padding:12px 24px;border-radius:8px;font-size:16px;z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);text-align:center;';
                div.innerHTML = '二维码已显示 — 请用小黑盒 App 扫码<br><small>登录成功后工具自动关闭</small>';
                document.body.appendChild(div);
            }
        """)

        print("\n" + "=" * 60)
        print("小黑盒登录二维码已显示")
        print("请用手机 App 扫描右上角头像弹出的二维码")
        print("=" * 60 + "\n")

        # 5. 等待登录完成：头像从默认游客图变为真实头像是成功信号
        max_wait = 120  # 2分钟
        for i in range(max_wait):
            if await self._is_logged_in_dom():
                print("小黑盒登录成功！")
                await self.page.evaluate("""
                    () => {
                        const hint = document.getElementById('wb-login-hint');
                        if (hint) {
                            hint.style.background = '#198754';
                            hint.innerHTML = '✓ 登录成功！<br><small>3秒后关闭此窗口...</small>';
                        }
                    }
                """)
                await asyncio.sleep(3)
                return
            await asyncio.sleep(3)  # 每 3 秒检查一次，不频繁干扰页面

        raise TimeoutError("登录超时，请重试")

    async def navigate_to_editor(self):
        """导航到小黑盒「文章」编辑器（真实路径：首页 → 发布内容 → 发布文章）

        关键修正：旧代码直接 goto /app/bbs/post，但该地址会 302 跳转到
        /app/bbs/home 首页信息流，编辑器从未真正打开，导致后续 fill/save
        全部在首页上做无用功，save_draft 还把首页/搜索页 URL 当草稿 URL 返回，
        造成「显示成功但草稿箱里找不到」的假成功。
        正确路径：点击右上「发布内容」(.publish-btn) 弹出类型面板，再点
        「发布文章」(div.slide-tab__tab-label)，才进入真正的编辑器，URL 形如
        /creator/editor/draft/article/local_xxx（该 URL 本身即草稿地址）。
        """
        self._editor_url = ""

        # 先回首页，确保从干净状态进入
        await self.page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded")
        await self.simulator.random_delay(2, 4)

        # 点击「发布内容」
        try:
            await self.page.click(self.PUBLISH_BTN, timeout=10000)
        except Exception as e:
            print(f"点击发布内容失败，尝试创作中心兜底: {e}")
            try:
                await self.page.goto("https://www.xiaoheihe.cn/creator", wait_until="domcontentloaded")
                await self.simulator.random_delay(2, 4)
                await self.page.click(self.PUBLISH_BTN, timeout=10000)
            except Exception:
                pass
        await self.simulator.random_delay(2, 3)

        # 点击「发布文章」类型标签
        try:
            await self.page.locator(self.TAB_ARTICLE, has_text="发布文章").first.click(timeout=8000)
        except Exception as e:
            print(f"点击发布文章失败，回退到发布图文: {e}")
            try:
                await self.page.locator(self.TAB_ARTICLE, has_text="发布图文").first.click(timeout=8000)
            except Exception:
                pass
        await self.simulator.random_delay(3, 5)

        # 等待编辑器标题框出现（真实标题框：.editor-title__container 内的 contenteditable ProseMirror）
        try:
            await self.page.wait_for_selector(self.TITLE_FIELD, timeout=15000)
        except Exception as e:
            print(f"等待编辑器标题框超时: {e}")

        # 记录编辑器 URL（同时是草稿 URL，供 save_draft/publish_now 回退定位）
        self._editor_url = self.page.url

    async def fill_title(self, title: str):
        """填写文章标题（真实字段：.editor-title__container 内的 contenteditable ProseMirror）"""
        try:
            el = await self.page.wait_for_selector(self.TITLE_FIELD, timeout=10000, state="visible")
            if el:
                await el.click()
                await self.simulator.random_delay(0.3, 0.8)
                # ProseMirror 直接键盘输入最稳
                await self.page.keyboard.type(title[:50], delay=30)
                await self.simulator.random_delay(0.5, 1.0)
                return
        except Exception as e:
            print(f"填写标题失败: {e}")

        # 兜底：页面上第一个 contenteditable
        try:
            el = await self.page.query_selector("[contenteditable='true']")
            if el:
                await el.click()
                await self.page.keyboard.type(title[:50], delay=30)
        except Exception:
            pass

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文（真实字段：.article__edit-content--inner 内的 contenteditable ProseMirror）"""
        editor = None
        try:
            editor = await self.page.wait_for_selector(self.BODY_FIELD, timeout=10000, state="visible")
        except Exception as e:
            print(f"等待正文编辑区超时，尝试通用选择器: {e}")
            try:
                editor = await self.page.query_selector("[contenteditable='true']")
            except Exception:
                pass

        if not editor:
            print("未找到正文编辑区")
            return

        await editor.click()
        await self.simulator.random_delay(0.3, 0.8)

        # 逐块写入正文（ProseMirror 用键盘输入最稳，按换行分段）
        for block in content_blocks:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                text = block["text"].strip()
                if not text:
                    continue
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.type(line.strip(), delay=20)
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                        await self.simulator.random_delay(0.1, 0.3)
                await self.page.keyboard.press("Enter")
                await self.simulator.random_delay(0.3, 0.6)
            elif btype == "heading" and block.get("text"):
                await self.page.keyboard.type(block["text"].strip(), delay=20)
                await self.page.keyboard.press("Enter")
                await self.simulator.random_delay(0.3, 0.6)
            elif btype == "image":
                img_path = block.get("local_path")
                if not img_path and images:
                    # 按 position 匹配，否则取第一张
                    for img in images:
                        if img.get("position_index") == block.get("position"):
                            img_path = img.get("local_path")
                            break
                    if not img_path:
                        img_path = images[0].get("local_path")
                if img_path:
                    await self._upload_image(img_path)
                    await self.simulator.random_delay(0.5, 1.0)

    async def _upload_image(self, image_path: str):
        """上传图片到小黑盒文章编辑器（best-effort：工具栏图片按钮 + 文件选择框）"""
        try:
            upload_btn_selectors = [
                "button:has-text('图片')",
                ".editor-toolbar [class*='image']",
                "[data-action='upload-image']",
                ".toolbar-icon-image",
                "button[title*='图片']",
            ]
            clicked = False
            for sel in upload_btn_selectors:
                try:
                    btn = await self.page.wait_for_selector(sel, timeout=3000)
                    if btn:
                        await btn.click()
                        clicked = True
                        await self.simulator.random_delay(0.5, 1.5)
                        break
                except Exception:
                    continue

            if not clicked:
                print("未找到图片上传按钮，跳过图片")
                return

            # 文件上传
            file_input = await self.page.wait_for_selector("input[type='file']", timeout=5000)
            if file_input:
                await file_input.set_input_files(image_path)
                await self.simulator.random_delay(3, 6)
                try:
                    await self.page.wait_for_selector(".upload-success, img[src*='upload']", timeout=15000)
                except Exception:
                    pass
        except Exception as e:
            print(f"小黑盒图片上传失败: {e}")

    async def select_topic(self, topic: str):
        """选择社区和话题（真实按钮：添加社区 / 添加话题，弹窗选择，不会跳走）

        关键修正：旧代码用 `text=选择社区` 去搜「社区」并点击搜索结果链接，会直接
        跳转到社区/搜索页，把编辑器带偏；save_draft 随后在搜索页取到 URL 当草稿
        URL，造成假成功。新版点击编辑器内的「添加社区/添加话题」弹窗选择，选择后
        页面仍停留在编辑器；若不慎跳走，save_draft 会回退到编辑器 URL 再保存。
        """
        if not topic:
            return

        # 1. 添加社区
        try:
            add_community = self.page.locator("button:has-text('添加社区')").first
            if await add_community.count() > 0:
                await add_community.click(timeout=5000)
                await self.simulator.random_delay(1, 2)
                search = await self.page.wait_for_selector(
                    ".modal input, [class*='dialog'] input, input[placeholder*='社区'], input[placeholder*='搜索']",
                    timeout=5000,
                )
                if search:
                    await search.fill(topic[:10])
                    await self.simulator.random_delay(1, 2)
                    first = await self.page.wait_for_selector(
                        ".modal .community-item:first-child, [class*='dialog'] li:first-child, .search-result-item:first-child",
                        timeout=5000,
                    )
                    if first:
                        await first.click()
                        await self.simulator.random_delay(0.5, 1.0)
                try:
                    await self.page.click("button:has-text('确定'), button:has-text('完成')", timeout=3000)
                except Exception:
                    pass
        except Exception as e:
            print(f"添加社区失败: {e}")
        # 关闭可能残留的社区选择弹窗，避免遮罩挡住后续操作
        await self._dismiss_overlays()

        # 2. 添加话题
        try:
            add_topic = self.page.locator("button:has-text('添加话题')").first
            if await add_topic.count() > 0:
                await add_topic.click(timeout=5000)
                await self.simulator.random_delay(1, 2)
                search = await self.page.wait_for_selector(
                    ".modal input, [class*='dialog'] input, input[placeholder*='话题'], input[placeholder*='搜索']",
                    timeout=5000,
                )
                if search:
                    await search.fill(topic[:10])
                    await self.simulator.random_delay(1, 2)
                    first = await self.page.wait_for_selector(
                        ".modal .topic-item:first-child, [class*='dialog'] li:first-child, .search-result-item:first-child",
                        timeout=5000,
                    )
                    if first:
                        await first.click()
                        await self.simulator.random_delay(0.5, 1.0)
                try:
                    await self.page.click("button:has-text('确定'), button:has-text('完成')", timeout=3000)
                except Exception:
                    pass
        except Exception as e:
            print(f"添加话题失败: {e}")

        # 关闭可能残留的弹窗遮罩，避免带偏后续保存
        await self._dismiss_overlays()

        # 话题选择是弹窗，不应跳走；若页面偏离编辑器则回退
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()

    async def _dismiss_overlays(self):
        """关闭可能残留的弹窗遮罩（如社区/话题选择未正常关闭时遗留的 mask），避免遮挡按钮点击。"""
        try:
            await self.page.keyboard.press("Escape")
            await self.simulator.random_delay(0.5, 1.0)
            mask = self.page.locator(".hb-cpt__mask-wrapper, .modal-mask, [class*='mask'], .modal").first
            if await mask.count() > 0:
                try:
                    await mask.click(timeout=2000)
                except Exception:
                    pass
                await self.simulator.random_delay(0.5, 1.0)
        except Exception:
            pass

    async def _back_to_editor(self):
        """若页面偏离了编辑器（被话题选择等带偏），回到编辑器 URL"""
        if getattr(self, "_editor_url", ""):
            try:
                await self.page.goto(self._editor_url, wait_until="domcontentloaded")
                await self.simulator.random_delay(2, 4)
            except Exception:
                pass

    async def save_draft(self, title: str = "") -> str:
        """保存草稿并返回草稿箱 URL；保存失败返回空串（绝不以当前页 URL 冒充成功）

        真实保存按钮：button.editor-publish__save-draft
        保存后去「草稿箱」(button.editor-publish__btn.sub-btn.margin-left) 验证标题是否出现，
        以草稿箱真实存在该草稿作为成功判据（直接回应「账号/草稿箱里都找不到」的投诉）。
        """
        # 若在编辑器外（被话题选择等带偏），先回到编辑器
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()

        if "creator/editor" not in (self.page.url or ""):
            print("保存草稿失败：不在编辑器页面")
            return ""

        # 点击真实保存草稿按钮
        try:
            # close leftover modal mask if any (e.g. community/topic picker left open)
            await self._dismiss_overlays()
            await self.page.click(self.SAVE_DRAFT_BTN, timeout=8000)
        except Exception as e:
            print(f"点击保存草稿按钮失败: {e}")
            return ""

        await self.simulator.random_delay(2, 4)

        # 验证：进入草稿箱，确认标题存在
        return await self._verify_draft_in_drafts(title)

    async def _verify_draft_in_drafts(self, title: str) -> str:
        """前往草稿箱，确认刚写的草稿存在，返回草稿箱 URL；不存在返回空串"""
        try:
            draft_box = self.page.locator(self.DRAFT_BOX_BTN, has_text="草稿箱").first
            if await draft_box.count() > 0:
                await draft_box.click(timeout=5000)
                await self.simulator.random_delay(3, 5)
            else:
                # 直接导航草稿箱
                await self.page.goto("https://www.xiaoheihe.cn/creator/draft", wait_until="domcontentloaded")
                await self.simulator.random_delay(3, 5)
        except Exception as e:
            print(f"打开草稿箱失败: {e}")
            return ""

        drafts_url = self.page.url

        if title:
            keyword = title[:12]
            try:
                found = await self.page.evaluate("(kw) => (document.body.innerText || '').includes(kw)", keyword)
                if found:
                    return drafts_url
                # 再等一会，草稿可能异步出现
                await self.simulator.random_delay(3, 5)
                found = await self.page.evaluate("(kw) => (document.body.innerText || '').includes(kw)", keyword)
                if found:
                    return drafts_url
            except Exception:
                pass
            print(f"草稿箱未找到标题包含「{keyword}」的草稿")
            return ""

        # 无标题可校验，乐观返回草稿箱 URL
        return drafts_url

    async def publish_now(self, title: str = "") -> str:
        """点击「发布」按钮真正发布文章，返回发布后的文章 URL；失败返回空串"""
        if "creator/editor" not in (self.page.url or ""):
            await self._back_to_editor()
        if "creator/editor" not in (self.page.url or ""):
            return ""

        try:
            btn = self.page.locator(self.PUBLISH_NOW_BTN, has_text="发布").first
            if await btn.count() == 0:
                btn = self.page.locator("span.editor-publish__button-content", has_text="发布").first
            if await btn.count() == 0:
                return ""
            # close leftover modal mask if any
            await self._dismiss_overlays()
            await btn.click(timeout=8000)
        except Exception as e:
            print(f"点击发布失败: {e}")
            return ""

        await self.simulator.random_delay(3, 6)

        # 可能弹出确认框
        try:
            confirm = self.page.locator("button:has-text('确定'), button:has-text('确认发布')").first
            if await confirm.count() > 0:
                await confirm.click(timeout=3000)
                await self.simulator.random_delay(3, 6)
        except Exception:
            pass

        post_url = self.page.url
        try:
            success = await self.page.evaluate("""() => {
                const t = document.body.innerText || '';
                return t.includes('发布成功') || t.includes('发布完成') || t.includes('已发布');
            }""")
            if success:
                return post_url
            if "creator/editor" not in post_url:
                return post_url
        except Exception:
            pass
        # 无法确认，返回空（视为仅存草稿）
        return ""
