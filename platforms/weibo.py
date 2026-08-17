"""微博账号会话适配器。

登录态与身份验证链路（真实扫码 → 跳转回首页 → 身份捕获/DOM 确认），
内容投递能力尚未接入：所有投递方法显式拒绝。

真实登录页（https://passport.weibo.com/sso/signin?entry=miniblog...）：
- 「扫描二维码登录」为默认 Tab，二维码为约 140x140 的 img（v2.qr.weibo.cn）。
- 登录成功信号：扫码确认后页面从 passport 跳转回 weibo.com。
- 身份接口带签名/cookie 约束，裸 fetch 不可靠；采用「捕获页面自身响应」
  模式 + 首页 DOM 兜底，与小黑盒/小红书一致。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from platforms.base import (
    BasePlatform,
    BrowserLifecycleError,
    LoginRequiredError,
    PlatformAutomationError,
    SelectorError,
)
from platforms.content_validation import ensure_valid_content, safe_media_error

LOGIN_URL = (
    "https://passport.weibo.com/sso/signin?entry=miniblog"
    "&source=miniblog&disp=popup&url=https%3A%2F%2Fweibo.com%2F"
)
HOME_URL = "https://weibo.com/"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class WeiboPlatform(BasePlatform):
    """微博账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "weibo"
    # 游客也有 SUB/SUBP/WBPSESS；SCF/ALF/SSOLoginState 仅登录后存在。
    SESSION_COOKIE_NAMES = frozenset({"SCF", "ALF", "SSOLoginState"})
    LOGIN_POLL_ATTEMPTS = 40
    LOGIN_POLL_INTERVAL_SECONDS = 3

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_login_error = ""
        self._identity_payload: dict[str, str | int | bool] | None = None

    async def initialize(self):
        await super().initialize()
        self._identity_payload = None

    # ==================== 登录态与身份 ====================

    async def _has_session_cookie_signal(self) -> bool:
        """登录会话 cookie（SCF/ALF/SSOLoginState）作为登录成功信号。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://weibo.com/",
                "https://www.weibo.com/",
                "https://weibo.cn/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def _on_home(self) -> bool:
        """是否已离开 passport 并处于微博首页（登录成功信号）。"""

        try:
            url = self.page.url or ""
            return "passport.weibo.com" not in url and "weibo.com" in url
        except Exception:
            return False

    async def check_login(self) -> bool:
        """只读验证现有 Profile；首页身份确认成功才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("微博登录态检测")
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                identity = await self.fetch_identity_payload()
                if identity.get("ok"):
                    return True
                self.last_login_error = (
                    "WEIBO_IDENTITY_MISSING: 微博会话存在但身份未确认"
                )
                return False
            self.last_login_error = "LOGIN_REQUIRED: 微博账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "WEIBO_LOGIN_CHECK_ERROR: 微博登录态验证失败"
            return False

    async def login(self):
        """打开微博扫码登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("微博打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img')
                    );
                    return imgs.some((i) => {
                        const src = i.src || '';
                        return src.includes('qr.weibo.cn')
                            && i.naturalWidth > 100 && i.naturalWidth < 260;
                    });
                }""",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("微博等待登录")
            if await self._on_home() or await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 微博登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'wb-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#ff8200;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用微博 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回微博首页同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "weibo.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "logininfo", "profile", "account")
                        )
                    ):
                        payload = await response.json()
                        found = self._extract_identity_from_json(payload)
                        if found and self._identity_payload is None:
                            self._identity_payload = {
                                "ok": True,
                                "user_id": found[0],
                                "display_name": found[1],
                            }
                except Exception:  # noqa: BLE001
                    pass

            self.page.on("response", _on_response)
            try:
                await self.page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                for _ in range(8):
                    if self._identity_payload is not None:
                        break
                    await asyncio.sleep(1)
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博身份捕获时页面已关闭"
                ) from exc
            logger.warning("微博身份捕获失败: {}", exc)

        # DOM 兜底：头像链接 /u/<uid> + 顶部用户区昵称（登录后首页）
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                dom = await self.page.evaluate(
                    """() => {
                        const uidLink = document.querySelector('a[href*="/u/"]');
                        const uidMatch = uidLink
                            ? (uidLink.getAttribute('href') || '').match(/\\/u\\/(\\d+)/)
                            : null;
                        const nickEl = document.querySelector('[class*="_nick_"]');
                        let nickname = nickEl
                            ? (nickEl.innerText || '').trim()
                            : '';
                        if (nickname.startsWith('@')) {
                            nickname = '';
                        }
                        return {
                            user_id: uidMatch ? uidMatch[1] : '',
                            display_name: nickname,
                        };
                    }"""
                )
                if (
                    isinstance(dom, dict)
                    and dom.get("user_id")
                    and dom.get("display_name")
                ):
                    self._identity_payload = {
                        "ok": True,
                        "user_id": str(dom["user_id"]),
                        "display_name": str(dom["display_name"]),
                    }
            except Exception:  # noqa: BLE001
                pass

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

    @staticmethod
    def _extract_identity_from_json(payload) -> tuple[str, str] | None:
        """递归扫描响应 JSON，寻找同时含昵称与 ID 的最小身份对象。"""

        if not isinstance(payload, dict):
            return None
        found: list[tuple[str, str]] = []

        def walk(node) -> None:
            if isinstance(node, dict):
                name = next(
                    (str(node[key]) for key in IDENTITY_NAME_KEYS if node.get(key)),
                    "",
                )
                uid = next(
                    (str(node[key]) for key in IDENTITY_UID_KEYS if node.get(key)),
                    "",
                )
                if name and uid:
                    found.append((uid, name))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(payload)
        return found[0] if found else None

    # ==================== 草稿投递链路 ====================

    @staticmethod
    def _not_implemented(operation: str):
        raise PlatformNotImplementedError(
            f"PLATFORM_NOT_IMPLEMENTED: 微博{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开微博头条文章编辑器：草稿箱视图 → 真实点击「写文章」。

        2026-08 实测：必须用真实鼠标点击「写文章」才会创建草稿并切换到
        ``#/draft/{id}`` 视图（JS 模拟点击不会触发导航，导致保存草稿时
        id 为空、服务端返回参数错误）。创建后标题/正文字段即可填写。
        """
        self._require_page_alive("微博打开编辑器")
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_function(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const txt = (el.innerText || '').trim();
                        return txt === '写文章' && el.children.length <= 3;
                    });
                }""",
                timeout=15000,
            )
            write_btn = self.page.get_by_text("写文章", exact=True).first
            await write_btn.click(timeout=10000)
            # 等待切换到已创建的草稿视图 #/draft/{id}
            await self.page.wait_for_function(
                """() => /#\\/draft\\/\\d+/.test(location.hash)""",
                timeout=20000,
            )
            await self.page.wait_for_selector(
                "textarea[placeholder='请输入标题']",
                state="visible",
                timeout=20000,
            )
            try:
                await self.page.wait_for_selector(
                    ".wb-editor-spin, .n-spin-body",
                    state="detached",
                    timeout=20000,
                )
            except Exception:
                pass
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博头条文章编辑器未找到标题输入框") from exc

    async def fill_title(self, title: str):
        """填写微博头条文章标题（textarea，placeholder「请输入标题」，0/32）。"""

        self._require_page_alive("微博填写标题")
        title_field = self.page.locator("textarea[placeholder='请输入标题']").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            # 直接 fill（不依赖 click，避免加载遮罩拦截命中）
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博填写标题时页面已关闭"
                ) from exc
            logger.error("微博标题填写失败: {}", exc)
            raise SelectorError("微博标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文：键盘逐段写入 TipTap 编辑器，回读并有序校验。"""

        self._require_page_alive("微博填写正文")
        # 草稿视图可能含隐藏的编辑器实例，必须取可见的那个
        editor = self.page.locator("div.tiptap.ProseMirror:visible").first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            try:
                await editor.click(timeout=5000)
            except Exception:
                await editor.evaluate("(el) => el.focus()")
            focused = await self.page.evaluate(
                """() => {
                    const el = document.activeElement;
                    return el ? el.isContentEditable : false;
                }"""
            )
            if not focused:
                raise RuntimeError("正文编辑器未能获得焦点")
            # 标题填充可能触发编辑器重渲染，等它稳定再输入
            await self.simulator.random_delay(1, 2)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("微博正文编辑器未找到或无法聚焦") from exc

        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass
        await self.simulator.random_delay(0.3, 0.8)

        first_text = True
        for block in content_blocks:
            btype = block.get("type")
            if btype in ("text", "heading") and block.get("text"):
                text = str(block["text"]).strip()
                if not text:
                    continue
                if not first_text:
                    await self.page.keyboard.press("Enter")
                lines = text.splitlines() or [text]
                for i, line in enumerate(lines):
                    if line.strip():
                        await self.page.keyboard.insert_text(line.strip())
                    if i < len(lines) - 1:
                        await self.page.keyboard.press("Enter")
                first_text = False

        actual_text = await editor.inner_text()
        expected_count = ensure_valid_content(
            content_blocks,
            actual_text,
            platform="微博",
            phase="输入后",
        )
        logger.info("微博正文文字输入并验证成功: {} 个文本段落", expected_count)

        expected_images = sum(
            1 for block in content_blocks if block.get("type") == "image"
        )
        uploaded_images = 0
        failed_images = []
        for block in content_blocks:
            if block.get("type") == "image":
                img_path = block.get("local_path")
                if not img_path and images:
                    for img in images:
                        if img.get("position_index") == block.get("position"):
                            img_path = img.get("local_path")
                            break
                    if not img_path:
                        img_path = images[0].get("local_path")
                if img_path:
                    upload_result = await self._upload_image(img_path) or {}
                    if upload_result.get("success"):
                        uploaded_images += 1
                    else:
                        failed_images.append(
                            {
                                "filename": Path(str(img_path)).name,
                                "error": safe_media_error(
                                    upload_result.get("error"),
                                    fallback="图片上传失败",
                                ),
                            }
                        )
                    await self.simulator.random_delay(0.2, 0.5)
                else:
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )

        actual_text = await editor.inner_text()
        ensure_valid_content(
            content_blocks,
            actual_text,
            platform="微博",
            phase="图片处理后",
        )
        logger.info("微博正文输入并最终验证成功: {} 个文本段落", expected_count)

        if expected_images == 0:
            media_status = "not_required"
            media_error = None
        elif uploaded_images == expected_images:
            media_status = "completed"
            media_error = None
        elif uploaded_images == 0:
            media_status = "failed"
            media_error = f"{expected_images} 张图片全部上传失败"
        else:
            media_status = "partial"
            media_error = f"{expected_images - uploaded_images} 张图片上传失败"

        if failed_images:
            logger.warning(
                "微博图片处理结果: expected={}, uploaded={}, failed={}",
                expected_images,
                uploaded_images,
                len(failed_images),
            )

        cover_result: dict | None = None
        if uploaded_images > 0:
            cover_result = await self.set_cover() or {}
            if not cover_result.get("success"):
                logger.warning("微博封面设置失败: {}", cover_result.get("error"))

        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
            "cover": cover_result,
        }

    async def _upload_image(self, image_path: str) -> dict:
        """通过头条文章编辑器的文件控件上传图片；以编辑器内图片数量增加为判据。"""

        self._require_page_alive("微博上传图片")
        try:
            file_inputs = self.page.locator("input[type=file]")
            if await file_inputs.count() == 0:
                return {"success": False, "error": "微博图片上传控件未找到"}
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    '.tiptap img, .ProseMirror img'
                ).length"""
            )
            await file_inputs.first.set_input_files(str(image_path), timeout=15000)
            after = before
            for _ in range(10):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        '.tiptap img, .ProseMirror img'
                    ).length"""
                )
                if after > before:
                    break
            if after <= before:
                return {"success": False, "error": "上传后编辑器图片数量未增加"}
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def set_cover(self) -> dict:
        """从正文图片中选择第一张设为文章封面（微博封面必须来自正文图）。

        2026-08 真实验收结论：**自动化环境下正文插图不可用**——编辑器
        无 input[type=file] 控件、insert 卡片菜单无图片入口、合成 drop
        事件不插入图片，判定为微博 Web 对自动化上传的限制。正文无图则
        封面弹窗无图可选，set_cover 如实失败、绝不假成功；用户手动粘贴
        /拖拽插图后可手动设置封面。
        """

        self._require_page_alive("微博设置封面")
        clicked = await self.page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find(el => {
                    const t = (el.innerText || '').trim();
                    return t === '设置文章封面' && el.children.length === 0;
                });
                if (!target) return 'not-found';
                const clickable = target.closest(
                    'button, [role="button"], [class*="btn" i], [class*="click" i], label, a'
                );
                if (clickable) { clickable.click(); return 'clicked'; }
                target.click();
                return 'clicked';
            }"""
        )
        if clicked != "clicked":
            return {"success": False, "error": "微博「设置文章封面」按钮未找到"}
        await self.simulator.random_delay(1, 2)

        # 弹窗中选择第一张正文图片（缩略图/图片容器）
        picked = await self.page.evaluate(
            """() => {
                const dialog = document.querySelector('.n-dialog');
                if (!dialog) return 'no-dialog';
                const imgs = Array.from(dialog.querySelectorAll('img'));
                if (imgs.length === 0) return 'no-images';
                const container = imgs[0].closest(
                    '[class*="item" i], [class*="pic" i], label'
                );
                const target = container || imgs[0];
                target.click();
                return 'picked';
            }"""
        )
        await self.simulator.random_delay(1, 2)
        if picked != "picked":
            return {"success": False, "error": "微博封面弹窗未找到正文图片"}

        # 点「下一步」（有图片时弹窗按钮应为「下一步」）
        next_clicked = await self.page.evaluate(
            """() => {
                const dialog = document.querySelector('.n-dialog');
                if (!dialog) return 'no-dialog';
                const buttons = Array.from(dialog.querySelectorAll('button'));
                const target = buttons.find(b =>
                    ['下一步', '确定', '完成', '使用该图'].includes((b.innerText || '').trim()));
                if (target) { target.click(); return 'clicked'; }
                return 'no-next-btn';
            }"""
        )
        await self.simulator.random_delay(2, 3)

        # 成功判据：弹窗关闭
        dialog_open = await self.page.evaluate(
            "() => !!document.querySelector('.n-dialog')"
        )
        if dialog_open:
            return {
                "success": False,
                "error": f"微博封面设置弹窗未关闭（pick={picked} next={next_clicked}）",
            }
        logger.info("微博封面已从正文首图设置完成")
        return {"success": True, "error": ""}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """微博保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

        if not (topic or community or selection_query):
            return {
                "success": True,
                "selection_status": "not_required",
                "selection": {},
            }
        return {
            "success": False,
            "needs_selection": True,
            "error_code": "TOPIC_SELECTION_NOT_IMPLEMENTED",
            "error": "微博话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """点击「保存草稿」，以「草稿箱标题出现」为成功判据。

        2026-08 实测：保存接口可能返回 code 100000（geetest 风控软提示），
        但保存实际生效（草稿卡片标题更新）。因此以草稿箱列表出现标题
        关键字为准；接口 code 仅作日志参考，不据此判失败。

        安全约束（2026-08-17 修复）：**绝不触发公开发布**——
        1. 「保存草稿」按钮精确匹配（不做模糊 includes，避免命中发布相关按钮）；
        2. 点击后监听发布接口（/publish/ 等），一旦捕获到发布请求立即失败；
        3. 草稿箱验证只认 #/draft 草稿箱列表，不把已发布内容当作草稿。
        """
        self._require_page_alive("微博保存草稿")
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                url = response.url
                method = response.request.method
                if "draft/save" in url and method in ("POST", "PUT"):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["code"] = body.get("code")
                    except Exception:
                        pass
                if (
                    "/publish" in url
                    or "/article/publish" in url
                    or (method in ("POST", "PUT") and "publish" in url.lower())
                ):
                    # 一旦出现发布请求，立即标记为误发布，绝不放行
                    captured["published"] = True
                    captured["publish_url"] = url[:200]
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    // 精确匹配「保存草稿」文本（去空白后完全相等），
                    // 不做 includes，避免命中「发布/下一步」等危险按钮。
                    const target = nodes.find((el) =>
                        (el.innerText || '').replace(/\\s+/g, '') === '保存草稿');
                    if (target) target.click();
                }"""
            )
            await self.simulator.random_delay(2, 4)
            for _ in range(10):
                if captured.get("status") or captured.get("published"):
                    break
                await asyncio.sleep(1)
            # 给发布/保存响应一个收敛窗口，避免 status 先到、publish 后到被漏检
            await self.simulator.random_delay(1, 2)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if captured.get("published"):
            logger.error(
                "微博保存草稿时检测到发布请求，立即失败（绝不误发布）: {}",
                captured.get("publish_url"),
            )
            return ""

        if not captured.get("status"):
            logger.error("微博保存草稿未产生任何保存请求")
            return ""

        # 回到草稿箱列表，按标题关键字验证草稿卡片（成功判据）
        try:
            await self.page.goto(
                "https://card.weibo.com/article/v5/editor#/draft",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            keyword = str(title or "").strip()[:12]
            if not keyword:
                logger.error("微博草稿验证缺少标题关键字")
                return ""
            found = False
            for _ in range(3):
                await self.simulator.random_delay(3, 5)
                found = bool(
                    await self.page.evaluate(
                        "(kw) => (document.body.innerText || '').includes(kw)",
                        keyword,
                    )
                )
                if found:
                    break
            if not found:
                logger.error(
                    "微博草稿箱未找到标题包含「{}」的草稿（接口 code={}）",
                    keyword,
                    captured.get("code"),
                )
                return ""
            logger.info(
                "微博草稿验证成功: 草稿箱出现标题「{}」，接口 status={} code={}",
                keyword,
                captured.get("status"),
                captured.get("code"),
            )
            return "https://card.weibo.com/article/v5/editor#/draft"
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 微博验证草稿时页面已关闭"
                ) from exc
            logger.error("微博草稿验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
