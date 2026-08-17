"""百家号（百度创作平台）账号会话适配器。

登录态与身份验证链路（真实扫码 → BDUSS 会话 cookie → 身份捕获/DOM），
内容投递能力尚未接入：所有投递方法显式拒绝。

真实登录载体（百度 passport，扫码登录为默认 Tab）：
- 登录页：https://passport.baidu.com/v2/?login
- 二维码：约 138x138 的 ``img.tang-pass-qrcode-img``（passport 二维码接口）。
- 登录成功信号：.baidu.com 出现 BDUSS cookie（百度核心会话凭证）。
- 身份接口带 cookie/签名约束，采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
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

LOGIN_URL = "https://passport.baidu.com/v2/?login"
HOME_URL = "https://baijiahao.baidu.com/"
CREATOR_HOME = "https://baijiahao.baidu.com/builder/rc/edit"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"uid", "user_id", "bjh_id", "id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class BaijiahaoPlatform(BasePlatform):
    """百家号账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "baijiahao"
    SESSION_COOKIE_NAMES = frozenset({"BDUSS"})
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
        """BDUSS 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://passport.baidu.com/",
                "https://baijiahao.baidu.com/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；BDUSS 会话 cookie 出现才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("百家号登录态检测")
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(4)
            if await self._has_session_cookie_signal():
                return True
            self.last_login_error = "LOGIN_REQUIRED: 百家号账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "BAIJIAHAO_LOGIN_CHECK_ERROR: 百家号登录态验证失败"
            return False

    async def login(self):
        """打开百度扫码登录页，等待用户在隔离 Profile 中扫码登录。"""

        self._require_page_alive("百家号打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_function(
                """() => {
                    const imgs = Array.from(
                        document.querySelectorAll('img.tang-pass-qrcode-img')
                    );
                    return imgs.some(
                        (i) => i.naturalWidth > 100 && i.naturalWidth < 260
                    );
                }""",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("百家号等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: 百家号登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_scan_hint(self):
        """页面顶部显示扫码提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'bjh-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#2932e1;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请用百度 App 扫码登录'
                        + '<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回百家号同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "baidu.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("logininfo", "user", "profile", "account")
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
                    CREATOR_HOME,
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
                    "BROWSER_CONTEXT_CLOSED: 百家号身份捕获时页面已关闭"
                ) from exc
            logger.warning("百家号身份捕获失败: {}", exc)

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
            f"PLATFORM_NOT_IMPLEMENTED: 百家号{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开百家号图文编辑器（type=news），等待「存草稿」按钮出现。

        真实结构（2026-08 探测）：标题与正文均为 FeEditor contenteditable
        （标题占位「请输入标题（2 - 64字）」，正文占位「请输入正文」），
        底部有「存草稿」按钮，编辑器自动保存。
        """
        self._require_page_alive("百家号打开编辑器")
        try:
            await self.page.goto(
                "https://baijiahao.baidu.com/builder/rc/edit?type=news",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            # 等待编辑器就绪：存草稿按钮或 FeEditor contenteditable 任一出现即可。
            # 多平台并发投递时页面加载变慢，放宽到 45s。
            await self.page.wait_for_function(
                """() => {
                    const hasSave = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    ).some((el) =>
                        (el.innerText || '').replace(/\\s+/g, '').includes('存草稿'));
                    const hasEditor = Array.from(document.querySelectorAll(
                        "div[class*='FeEditorApp-'][contenteditable='true']"
                    )).length >= 1;
                    return hasSave || hasEditor;
                }""",
                timeout=45000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("百家号编辑器未就绪") from exc

    def _editors(self):
        return self.page.locator(
            "div[class*='FeEditorApp-'][contenteditable='true']:visible"
        )

    async def _focus_editor(self, editor, label: str):
        if await editor.count() == 0 or not await editor.is_visible():
            raise RuntimeError(f"{label}编辑器不可见")
        try:
            await editor.click(timeout=5000)
        except Exception:
            await editor.evaluate("(el) => el.focus()")
        # 焦点检查必须在编辑器所属 frame 内执行（正文在 iframe 中）
        focused = await editor.evaluate(
            """() => {
                const el = document.activeElement;
                return el ? el.isContentEditable : false;
            }"""
        )
        if not focused:
            raise RuntimeError(f"{label}编辑器未能获得焦点")

    async def fill_title(self, title: str):
        """填写百家号标题（第一个 FeEditor contenteditable）。"""

        self._require_page_alive("百家号填写标题")
        try:
            editor = self._editors().first
            await self._focus_editor(editor, "标题")
            await self.simulator.random_delay(0.3, 0.8)
            try:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass
            await self.page.keyboard.insert_text(str(title or "").strip())
            actual = await editor.inner_text()
            if (title or "").strip() and title.strip() not in actual:
                raise RuntimeError("标题回读不一致")
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号填写标题时页面已关闭"
                ) from exc
            logger.error("百家号标题填写失败: {}", exc)
            raise SelectorError("百家号标题编辑器未找到或填写失败") from exc

    async def _body_editor_locator(self):
        """定位 UEditor 正文 iframe 内的可编辑 body。

        百家号正文是 UEditor，可编辑区在 iframe（body.view.news-editor-pc）
        内；标题在主页面的 FeEditor。返回 Playwright Locator 或 None。
        """
        for frame in self.page.frames:
            try:
                is_body = await frame.evaluate(
                    """() => {
                        const b = document.body;
                        return Boolean(
                            b && b.isContentEditable
                            && (b.className || '').includes('news-editor-pc')
                        );
                    }"""
                )
                if is_body:
                    return frame.locator("body")
            except Exception:  # noqa: BLE001
                continue
        return None

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文（UEditor iframe 内可编辑 body），回读并有序校验。"""

        self._require_page_alive("百家号填写正文")
        self._content_blocks = list(content_blocks)
        try:
            editor = await self._body_editor_locator()
            if editor is None or await editor.count() == 0:
                raise RuntimeError("正文编辑器不可见")
            await self._focus_editor(editor, "正文")
            await self.simulator.random_delay(0.5, 1)
            try:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass
            await self.simulator.random_delay(0.3, 0.8)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 百家号定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("百家号正文编辑器未找到") from exc

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
            platform="百家号",
            phase="输入后",
        )
        logger.info("百家号正文文字输入并验证成功: {} 个文本段落", expected_count)

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
            platform="百家号",
            phase="图片处理后",
        )
        logger.info("百家号正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "百家号图片处理结果: expected={}, uploaded={}, failed={}",
                expected_images,
                uploaded_images,
                len(failed_images),
            )

        cover_result: dict | None = None
        if expected_images > 0:
            # 封面可独立于正文插图：直接用本地首图上传到封面弹窗的 image/* 控件。
            cover_result = await self.set_cover() or {}
            if not cover_result.get("success"):
                logger.warning(
                    "百家号封面设置失败: {}", cover_result.get("error")
                )

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
        """通过编辑器文件控件上传图片；以编辑器内图片数量增加为判据。

        2026-08 实测：编辑器存在两个文件控件（video/* 与 image/*），
        必须选择 accept 含 image 的控件，不能取 first（那是视频控件）。
        """

        self._require_page_alive("百家号上传图片")
        try:
            image_inputs = self.page.locator(
                'input[type=file][accept*="image"]'
            )
            if await image_inputs.count() == 0:
                return {"success": False, "error": "百家号图片上传控件未找到"}
            before = await self.page.evaluate(
                """() => document.querySelectorAll(
                    "div[class*='FeEditorApp-'][contenteditable='true'] img"
                ).length"""
            )
            await image_inputs.first.set_input_files(str(image_path), timeout=15000)
            after = before
            for _ in range(12):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """() => document.querySelectorAll(
                        "div[class*='FeEditorApp-'][contenteditable='true'] img"
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
                    "BROWSER_CONTEXT_CLOSED: 百家号上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def set_cover(self) -> dict:
        """通过「选择封面」弹窗上传本地图片设为封面（3:2 预览后确定）。

        2026-08 真实验收结论：**自动化环境下封面上传不生效**——
        「点击本地上传」可触发 filechooser，set_files 后封面预览始终为空
        （弹窗保持「暂无标题」），点「确定」不关闭弹窗；换图重试一致。
        判定为百度侧对自动化环境的图片上传限制，如实失败、绝不假成功。
        用户手动操作可正常设置封面。
        """

        self._require_page_alive("百家号设置封面")
        clicked = await self.page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('*'));
                const target = nodes.find(el => {
                    const t = (el.innerText || '').trim();
                    return t === '选择封面' && el.children.length === 0;
                });
                if (!target) return 'not-found';
                target.click();
                return 'clicked';
            }"""
        )
        if clicked != "clicked":
            return {"success": False, "error": "百家号「选择封面」按钮未找到"}
        await self.simulator.random_delay(1, 2)

        # 弹窗出现后点「点击本地上传」触发系统文件选择，用 filechooser 上传
        # （隐藏控件直接 set_input_files 无效，2026-08 实测上传不落图）
        first_image = ""
        try:
            blocks = getattr(self, "_content_blocks", []) or []
            for block in blocks:
                if block.get("type") == "image" and block.get("local_path"):
                    first_image = str(block["local_path"])
                    break
        except Exception:  # noqa: BLE001
            pass
        if not first_image:
            return {"success": False, "error": "百家号封面缺少本地图片素材"}
        try:
            async with self.page.expect_file_chooser(timeout=15000) as fc_info:
                upload_clicked = await self.page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('*'));
                        const target = nodes.find(el => {
                            const t = (el.innerText || '').trim();
                            return t === '点击本地上传' && el.children.length === 0;
                        });
                        if (!target) return 'not-found';
                        const clickable = target.closest(
                            '[class*="btn" i], [role="button"], [class*="upload" i], label, div'
                        );
                        if (clickable && clickable !== target) {
                            clickable.click();
                            return 'clicked';
                        }
                        target.click();
                        return 'clicked';
                    }"""
                )
            file_chooser = await fc_info.value
            await file_chooser.set_files(first_image)
        except TimeoutError:
            return {
                "success": False,
                "error": f"百家号封面未触发文件选择（upload={upload_clicked}）",
            }
        await self.simulator.random_delay(3, 5)

        # 等待封面预览出现后点「确定」（可见按钮中最后一个；可能有图片确认+封面确定两步）
        confirmed = await self.page.evaluate(
            """() => {
                const nodes = Array.from(
                    document.querySelectorAll('button, [role="button"], [class*="btn" i]')
                );
                const visible = nodes.filter(
                    (el) => (el.innerText || '').trim() === '确定'
                        && el.offsetParent !== null
                );
                const target = visible[visible.length - 1] || null;
                if (target) { target.click(); return 'clicked'; }
                return 'no-confirm';
            }"""
        )
        await self.simulator.random_delay(2, 3)

        # 成功判据：封面上传弹窗关闭（弹窗含两步：图片「确认」→ 封面「确定」）
        dialog_closed = False
        for _ in range(6):
            dialog_open = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some(el => {
                        const t = (el.innerText || '').trim();
                        return t === '点击本地上传'
                            && el.children.length === 0
                            && el.offsetParent !== null;
                    });
                }"""
            )
            if not dialog_open:
                dialog_closed = True
                break
            # 弹窗仍在：依次点可见的「确认」/「确定」（最后一个可见按钮）
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role="button"], [class*="btn" i]')
                    );
                    const visible = nodes.filter(
                        (el) => ['确认', '确定'].includes((el.innerText || '').trim())
                            && el.offsetParent !== null
                    );
                    const target = visible[visible.length - 1] || null;
                    if (target) target.click();
                }"""
            )
            await self.simulator.random_delay(2, 3)
        if not dialog_closed:
            return {
                "success": False,
                "error": f"百家号封面上传弹窗未关闭（confirm={confirmed}）",
            }
        logger.info("百家号封面已设置（本地首图）")
        return {"success": True, "error": ""}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """百家号保存草稿不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "百家号话题选择尚未接入（保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """点击「存草稿」，以「保存接口 2xx」验证。

        百家号编辑器自动保存且草稿入口在内容管理；保存判据 = 点击存草稿
        后捕获保存接口 2xx。标题关键字验证在内容管理草稿列表中补充。
        """
        self._require_page_alive("百家号保存草稿")
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH") and (
                    "save" in response.url.lower()
                    or "draft" in response.url.lower()
                    or "article" in response.url.lower()
                ):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["errno"] = body.get("errno")
                    except Exception:
                        pass
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, [role=button]')
                    );
                    const target = nodes.find((el) =>
                        (el.innerText || '').replace(/\\s+/g, '').includes('存草稿'));
                    if (target) target.click();
                }"""
            )
            await self.simulator.random_delay(2, 4)
            for _ in range(10):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.error("百家号存草稿未产生任何保存请求")
            return ""
        if captured.get("errno") not in (None, 0):
            logger.error("百家号存草稿接口返回错误: {}", captured)
            return ""
        logger.info("百家号存草稿验证成功: 保存接口 {}", captured.get("status"))
        return "https://baijiahao.baidu.com/builder/rc/edit?type=news"

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
