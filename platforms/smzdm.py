"""什么值得买（smzdm）账号会话适配器。

登录方式说明（2026-08 真实页面探测）：
- smzdm Web 登录页（https://zhiyou.smzdm.com/user/login）只有
  手机号/邮箱 + 密码 + 「60 天内免登录」表单，**没有扫码登录**
  （页面上 120x120 二维码是 App 下载码，不是登录码）。
- 因此本适配器采用「人工凭据登录」：打开可见浏览器窗口，由用户在
  窗口内输入账号密码（凭据不进入代码、不存储、不记录），适配器
  轮询会话 cookie 确认登录成功。

登录成功信号：.smzdm.com 出现 sess 会话 cookie（登录后才下发）。
身份提取采用「捕获页面自身响应」模式 + 首页 DOM 兜底。
"""

from __future__ import annotations

import asyncio
import json
import re
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

LOGIN_URL = "https://zhiyou.smzdm.com/user/login"
HOME_URL = "https://zhiyou.smzdm.com/"
USERNAME_SELECTOR = "input#username.form-input"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name", "nick"}
IDENTITY_UID_KEYS = {"smzdm_id", "uid", "user_id", "id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class SmzdmPlatform(BasePlatform):
    """什么值得买账号会话适配器；内容投递能力保持关闭。"""

    platform_name = "smzdm"
    SESSION_COOKIE_NAMES = frozenset({"sess"})
    LOGIN_POLL_ATTEMPTS = 120
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
        """sess 会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies([
                "https://www.smzdm.com/",
                "https://zhiyou.smzdm.com/",
            ])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话 cookie 出现才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("smzdm 登录态检测")
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
                self.last_login_error = "SMZDM_IDENTITY_MISSING: 会话存在但身份未确认"
                return False
            self.last_login_error = "LOGIN_REQUIRED: smzdm 账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "SMZDM_LOGIN_CHECK_ERROR: smzdm 登录态验证失败"
            return False

    async def login(self):
        """打开 smzdm 登录页，由用户在可见窗口中输入凭据完成登录。"""

        self._require_page_alive("smzdm 打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await self.page.wait_for_selector(
                USERNAME_SELECTOR,
                state="visible",
                timeout=20000,
            )
        except Exception:
            pass
        await self._show_login_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("smzdm 等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        self.last_login_error = "LOGIN_REQUIRED: smzdm 登录超时，请重新完成登录"
        raise LoginRequiredError(self.last_login_error)

    async def _show_login_hint(self):
        """页面顶部显示登录提示条。"""

        try:
            await self.page.evaluate(
                """() => {
                    const div = document.createElement('div');
                    div.id = 'smzdm-login-hint';
                    div.style.cssText = 'position:fixed;top:10px;left:50%;'
                        + 'transform:translateX(-50%);background:#fe6d01;color:#fff;'
                        + 'padding:12px 24px;border-radius:8px;font-size:16px;'
                        + 'z-index:999999;box-shadow:0 4px 12px rgba(0,0,0,0.3);'
                        + 'text-align:center;';
                    div.innerHTML = '请在下方表单输入手机号/邮箱和密码登录'
                        + '（可勾选 60 天内免登录）<br><small>登录成功后此窗口自动关闭</small>';
                    document.body.appendChild(div);
                }"""
            )
        except Exception:
            pass

    async def fetch_identity_payload(self) -> dict[str, str | int | bool]:
        """返回 smzdm 同源确认的最小平台身份（捕获 + DOM 兜底）。"""

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        try:
            async def _on_response(response) -> None:
                try:
                    if (
                        response.request.resource_type in ("xhr", "fetch")
                        and "smzdm.com" in response.url
                        and any(
                            key in response.url.lower()
                            for key in ("user", "profile", "account", "info")
                        )
                    ):
                        # smzdm 当前用户接口是 JSONP（callback 包裹），
                        # response.json() 会失败，必须取文本后剥壳解析。
                        text = await response.text()
                        payload = self._parse_jsonp(text)
                        if not isinstance(payload, dict):
                            return
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
                    "BROWSER_CONTEXT_CLOSED: smzdm 身份捕获时页面已关闭"
                ) from exc
            logger.warning("smzdm 身份捕获失败: {}", exc)

        # DOM 兜底：个人中心昵称 + user cookie 中的用户 ID
        if not (
            isinstance(self._identity_payload, dict) and self._identity_payload.get("ok")
        ):
            try:
                nickname = await self.page.evaluate(
                    """() => {
                        const el = document.querySelector('.info-stuff-nickname');
                        return el ? (el.innerText || '').trim() : '';
                    }"""
                )
                user_id = ""
                if self.context is not None:
                    cookies = await self.context.cookies([
                        "https://www.smzdm.com/",
                        "https://zhiyou.smzdm.com/",
                    ])
                    user_cookie = next(
                        (c.get("value", "") for c in cookies if c.get("name") == "user"),
                        "",
                    )
                    m = re.search(r"user%3A(\d+)\|(\d+)", str(user_cookie))
                    if m:
                        user_id = m.group(2) or m.group(1)
                if user_id and nickname:
                    self._identity_payload = {
                        "ok": True,
                        "user_id": user_id,
                        "display_name": nickname,
                    }
            except Exception:  # noqa: BLE001
                pass

        if isinstance(self._identity_payload, dict) and self._identity_payload.get("ok"):
            return dict(self._identity_payload)
        return {"ok": False, "user_id": "", "display_name": ""}

    @staticmethod
    def _parse_jsonp(text: str) -> dict | None:
        """剥掉 JSONP 的 callback 包裹并解析为 dict；失败返回 None。"""

        match = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", text or "", re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
            return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001
            return None

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
            f"PLATFORM_NOT_IMPLEMENTED: smzdm{operation}能力尚未接入"
        )

    async def navigate_to_editor(self):
        """打开 smzdm 投稿编辑器：投稿页 → 发布新文章。

        真实结构（2026-08 探测）：标题 ``textarea.article-title``
        （0/30），正文 ``div.ProseMirror``（TipTap），「草稿将自动保存」。
        """
        self._require_page_alive("smzdm 打开编辑器")
        try:
            await self.page.goto(
                "https://post.smzdm.com/tougao/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.page.wait_for_function(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    return nodes.some((el) => {
                        const t = (el.innerText || '').trim();
                        return (t === '发布新文章' || t === '写文章')
                            && el.children.length <= 3;
                    });
                }""",
                timeout=20000,
            )
            clicked = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const target = nodes.find((el) => {
                        const t = (el.innerText || '').trim();
                        return (t === '发布新文章' || t === '写文章')
                            && el.children.length <= 3;
                    });
                    if (target) { target.click(); return true; }
                    return false;
                }"""
            )
            if not clicked:
                raise RuntimeError("发布新文章入口未找到")
            await self.page.wait_for_selector(
                "textarea.article-title",
                state="visible",
                timeout=20000,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 打开编辑器时页面已关闭"
                ) from exc
            raise SelectorError("smzdm 投稿编辑器未找到标题输入框") from exc

    async def fill_title(self, title: str):
        """填写 smzdm 文章标题（textarea.article-title，0/30）。"""

        self._require_page_alive("smzdm 填写标题")
        title_field = self.page.locator("textarea.article-title").first
        try:
            if await title_field.count() == 0 or not await title_field.is_visible():
                raise RuntimeError("标题输入框不可见")
            await title_field.fill(str(title or "").strip())
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 填写标题时页面已关闭"
                ) from exc
            logger.error("smzdm 标题填写失败: {}", exc)
            raise SelectorError("smzdm 标题输入框未找到或填写失败") from exc

    async def fill_content(self, content_blocks: list, images: list):
        """填写正文（ProseMirror），回读并有序校验。"""

        self._require_page_alive("smzdm 填写正文")
        editor = self.page.locator("div.ProseMirror").first
        try:
            if await editor.count() == 0 or not await editor.is_visible():
                raise RuntimeError("正文编辑器不可见")
            try:
                await editor.click(timeout=5000)
            except Exception:
                await editor.evaluate("(el) => el.focus()")
            await self.simulator.random_delay(0.3, 0.8)
            try:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass
            await self.simulator.random_delay(0.3, 0.8)
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 定位正文编辑器时页面已关闭"
                ) from exc
            raise SelectorError("smzdm 正文编辑器未找到") from exc

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
            platform="smzdm",
            phase="输入后",
        )
        logger.info("smzdm 正文文字输入并验证成功: {} 个文本段落", expected_count)

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
                    # 每张图片之间放慢节奏，降低风控敏感度
                    await self.simulator.random_delay(2.5, 4.5)
                else:
                    failed_images.append(
                        {"filename": "", "error": "文章图片块没有对应本地文件"}
                    )

        actual_text = await editor.inner_text()
        ensure_valid_content(
            content_blocks,
            actual_text,
            platform="smzdm",
            phase="图片处理后",
        )
        logger.info("smzdm 正文输入并最终验证成功: {} 个文本段落", expected_count)

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
                "smzdm 图片处理结果: expected={}, uploaded={}, failed={}",
                expected_images,
                uploaded_images,
                len(failed_images),
            )
        return {
            "text_ok": True,
            "expected_images": expected_images,
            "uploaded_images": uploaded_images,
            "failed_images": failed_images,
            "media_status": media_status,
            "media_error": media_error,
        }

    async def _upload_image(self, image_path: str) -> dict:
        """通过编辑器文件控件上传图片；以编辑器内图片数量增加为判据。"""

        self._require_page_alive("smzdm 上传图片")
        try:
            # 优先选择 accept 含 image 的文件控件，避免误选封面/视频控件
            # （百家号 video 控件教训：file_inputs.first 可能选错）。
            target_input = None
            file_inputs = self.page.locator("input[type=file]")
            count = await file_inputs.count()
            if count == 0:
                return {"success": False, "error": "smzdm 图片上传控件未找到"}
            for i in range(count):
                accept = (await file_inputs.nth(i).get_attribute("accept")) or ""
                if "image" in accept.lower():
                    target_input = file_inputs.nth(i)
                    break
            if target_input is None:
                target_input = file_inputs.first
            before = await self.page.evaluate(
                """() => document.querySelectorAll('.ProseMirror img').length"""
            )
            await target_input.set_input_files(str(image_path), timeout=15000)
            after = before
            for _ in range(10):
                await asyncio.sleep(1)
                after = await self.page.evaluate(
                    """() => document.querySelectorAll('.ProseMirror img').length"""
                )
                if after > before:
                    break
            if after <= before:
                return {"success": False, "error": "上传后编辑器图片数量未增加"}
            return {"success": True, "error": ""}
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """smzdm 草稿自动保存不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "smzdm 话题选择尚未接入（自动保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """smzdm 编辑器「草稿将自动保存」；验证 = 草稿箱出现标题。

        无独立存草稿按钮，自动保存在内容变化后触发。要点：
        1. fill_content 输入期间自动保存已可能触发（此时监听器尚未挂上），
           因此仅等待「新请求」会误报失败——本方法先主动制造一次内容变化
           （光标处空格+退格，文档内容不变但触发 onChange），强制刷新自动保存；
        2. 捕获保存相关 POST/PUT/PATCH 2xx 作为补充证据；
        3. **平台真值**：回到投稿页「我的草稿」区块，标题关键字出现才算成功
           （2026-08 实测该区块展示 标题/字数/图数/创建时间/继续编辑）。
        """
        self._require_page_alive("smzdm 保存草稿")
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH") and (
                    "save" in response.url.lower()
                    or "draft" in response.url.lower()
                    or "edit" in response.url.lower()
                    or "article" in response.url.lower()
                ):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["error_code"] = body.get("error_code")
                    except Exception:
                        pass
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            # 主动制造一次内容变化，强制触发自动保存（文档内容保持不变）：
            # 聚焦正文 → 移到末尾 → 输入一个空格 → 删除 → 失焦。
            await self.page.evaluate(
                """() => {
                    const el = document.querySelector('.ProseMirror');
                    if (el) el.focus();
                }"""
            )
            await self.simulator.random_delay(0.3, 0.8)
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ", delay=50)
            await self.page.keyboard.press("Backspace")
            await self.page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (el) el.blur();
                }"""
            )
            await self.simulator.random_delay(1, 2)
            for _ in range(15):
                if captured.get("status"):
                    break
                await asyncio.sleep(1)
            # 自动保存可能有防抖，多等一会儿让请求落地
            await self.simulator.random_delay(1, 2)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.warning("smzdm 未捕获到保存请求（继续以草稿箱真值验证）")

        # 平台真值：投稿页「我的草稿」区块出现标题关键字
        try:
            await self.page.goto(
                "https://post.smzdm.com/tougao/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            keyword = str(title or "").strip()[:12]
            if not keyword:
                logger.error("smzdm 草稿验证缺少标题关键字")
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
                    "smzdm 草稿箱未找到标题包含「{}」的草稿（保存请求={}）",
                    keyword,
                    captured.get("status"),
                )
                return ""
            logger.info(
                "smzdm 草稿验证成功: 草稿箱出现标题「{}」（保存请求={}）",
                keyword,
                captured.get("status"),
            )
            return "https://post.smzdm.com/tougao/"
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: smzdm 草稿验证时页面已关闭"
                ) from exc
            logger.error("smzdm 草稿箱验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        self._not_implemented("公开发布")
