"""头条号（mp.toutiao.com）账号会话与草稿投递适配器。

登录（2026-08 真实探测）：
- 登录页 https://mp.toutiao.com/auth/page/login（**不能带 redirect_url 参数**，
  带参数会触发页面 JS atob 解码崩溃导致白屏；无参数正常）。
- 「扫码登录」用抖音 App 扫码；登录成功信号 cookie：
  sessionid / sessionid_ss / sid_tt / uid_tt / sid_guard / toutiao_sso_user 等。
- 新账号首次登录后需完成创作者账号类型选择（personal/landing-type →
  landing-complete/success），完成后才能访问创作页与草稿箱。

身份（捕获页面自身响应）：
- 登录后访问工作台，捕获 /user/profile/auth/info/v2/ 响应：
  data.user_name 为昵称，URL 参数 __user_id 为平台用户 ID。

编辑器（https://mp.toutiao.com/profile_v4/graphic/publish）：
- 标题：textarea[placeholder*="文章标题"]（2~30 字，最少 2 字）。
- 正文：div.ProseMirror（contenteditable，TipTap/ProseMirror 系）。
- 封面模式：单图(2)/三图(3)/无封面(1)；纯文字文章需选「无封面」。
- 草稿：无独立存草稿按钮，「草稿将自动保存」（自动保存接口
  POST /mp/agw/article/publish?type=article，save=0）。

2026-08 真实验收结论：**头条自动化草稿保存被风控稳定拒绝**——
- 自动保存请求（带正文，save=0）返回 code 7050「保存失败」；
  空正文保存可通过（深诊空草稿成功落库）。
- 已排除：封面模式（选无封面 coverType=1 仍 7050）、输入节奏
  （超真人慢速/随机停顿/打错重打仍 7050）、频率（冷却后仍 7050）、
  发布路径（点「预览并发布」同样 save=0 → 7050）。
- 人工手动（真人浏览器）保存/发布正常——是**字节系风控判定
  Playwright 启动的 Chrome 为自动化环境**，带内容（高风险）保存被拒。
- 因此头条**不满足「真实草稿验收」前置条件，投递保持关闭**；
  save_draft 如实失败（返回空串），绝不以假成功放行。将来若需
  头条投递，只能在真人浏览器手动操作或另行评估风控规避（有账号风险）。
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

LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
HOME_URL = "https://mp.toutiao.com/profile_v4/"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
DRAFT_BOX_URL = "https://mp.toutiao.com/profile_v4/manage/draft"
TITLE_SELECTOR = "textarea[placeholder*='文章标题']"
BODY_SELECTOR = "div.ProseMirror[contenteditable='true']"
IDENTITY_NAME_KEYS = {"nickname", "user_name", "screen_name", "name"}
IDENTITY_UID_KEYS = {"user_id", "uid", "id"}


class PlatformNotImplementedError(PlatformAutomationError):
    """平台能力尚未实现。"""

    error_code = "PLATFORM_NOT_IMPLEMENTED"


class ToutiaoPlatform(BasePlatform):
    """头条号账号会话适配器；投递开放需通过真实草稿验收。"""

    platform_name = "toutiao"
    SESSION_COOKIE_NAMES = frozenset(
        {
            "sessionid",
            "sessionid_ss",
            "sid_tt",
            "sid_guard",
            "uid_tt",
            "uid_tt_ss",
            "toutiao_sso_user",
            "passport_auth_status",
        }
    )
    LOGIN_POLL_ATTEMPTS = 100
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
        """会话 cookie 作为登录成功信号；不返回、不记录 cookie 值。"""

        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies(["https://mp.toutiao.com/"])
        except Exception:
            return False
        return any(
            str(item.get("name") or "") in self.SESSION_COOKIE_NAMES
            for item in cookies
        )

    async def check_login(self) -> bool:
        """只读验证现有 Profile；会话 cookie 出现且身份可确认才认定登录有效。"""

        try:
            self.last_login_error = ""
            self._require_page_alive("头条号登录态检测")
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
                self.last_login_error = "TOUTIAO_IDENTITY_MISSING: 会话存在但身份未确认"
                return False
            self.last_login_error = "LOGIN_REQUIRED: 头条号账号需要登录"
            return False
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号登录态检测时页面已关闭"
                ) from exc
            self.last_login_error = "TOUTIAO_LOGIN_CHECK_ERROR: 头条号登录态验证失败"
            return False

    async def login(self):
        """打开头条号登录页（无 redirect_url 参数），等待抖音 App 扫码登录。"""

        self._require_page_alive("头条号打开登录页")
        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        # 确保「扫码登录」Tab 激活
        try:
            await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('span, div'));
                    const tab = nodes.find(
                        (el) => (el.innerText || '').trim() === '扫码登录'
                    );
                    if (tab) tab.click();
                }"""
            )
        except Exception:
            pass
        await self._show_scan_hint()

        for _ in range(self.LOGIN_POLL_ATTEMPTS):
            self._require_page_alive("头条号等待登录")
            if await self._has_session_cookie_signal():
                self.last_login_error = ""
                return
            await asyncio.sleep(self.LOGIN_POLL_INTERVAL_SECONDS)
        raise LoginRequiredError("等待头条号扫码登录超时")

    async def _show_scan_hint(self):
        logger.info(
            "请使用抖音 App 扫描头条号登录二维码（隔离 Profile: {}）",
            getattr(self, "profile_dir", "?"),
        )

    async def fetch_identity_payload(self) -> dict:
        """捕获页面自身 /user/profile/auth/info/v2 响应（裸 fetch 会被签名拒绝）。"""

        captured: list[dict] = []

        async def _on_response(response) -> None:
            try:
                url = response.url
                if "/user/profile/auth/info/" in url and "__user_id=" in url:
                    body = await response.text()
                    if len(body) < 200000:
                        captured.append({"url": url, "body": body})
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            await self.page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self.simulator.random_delay(4, 7)
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号身份捕获时页面已关闭"
                ) from exc
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        for item in captured:
            try:
                payload = json.loads(item["body"])
                data = payload.get("data") or {}
            except Exception:
                continue
            user_name = str(data.get("user_name") or "").strip()
            match = re.search(r"__user_id=(\d+)", item["url"])
            if user_name and match:
                self._identity_payload = {
                    "ok": True,
                    "user_id": match.group(1),
                    "display_name": user_name,
                }
                return self._identity_payload

        # DOM 兜底：工作台头部昵称
        try:
            nickname = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const texts = nodes
                        .filter((el) => el.children.length === 0)
                        .map((el) => (el.innerText || '').trim())
                        .filter((t) => t && t.length <= 40);
                    return texts[0] || '';
                }"""
            )
        except Exception:  # noqa: BLE001
            nickname = ""
        if nickname:
            self._identity_payload = {
                "ok": True,
                "user_id": "",
                "display_name": nickname,
            }
            return self._identity_payload
        self._identity_payload = {"ok": False}
        return self._identity_payload

    # ==================== 草稿投递链 ====================

    async def navigate_to_editor(self):
        """直接打开头条号文章编辑器（graphic/publish）。"""

        self._require_page_alive("头条号打开编辑器")
        await self.page.goto(
            PUBLISH_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        try:
            await self.page.wait_for_selector(
                TITLE_SELECTOR,
                timeout=20000,
            )
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号编辑器打开时页面已关闭"
                ) from exc
            raise SelectorError(
                "SELECTOR_ERROR: 头条号编辑器标题输入框未出现"
            ) from exc
        await self.simulator.random_delay(2, 4)

    async def fill_title(self, title: str):
        self._require_page_alive("头条号填写标题")
        title_input = self.page.locator(TITLE_SELECTOR)
        try:
            await title_input.click(timeout=8000)
        except Exception:
            pass
        await title_input.fill("")
        await self.page.keyboard.insert_text(str(title))
        await self.simulator.random_delay(1, 2)
        actual = (await title_input.input_value()).strip()
        if not actual:
            raise SelectorError("SELECTOR_ERROR: 头条号标题输入后为空")
        if str(title).strip()[:2] not in actual and len(str(title).strip()) >= 2:
            raise SelectorError("SELECTOR_ERROR: 头条号标题回读不一致")
        logger.info("头条号标题已填写并回读验证: {} 字", len(actual))

    async def fill_content(self, content_blocks: list, images: list):
        """正文输入 div.ProseMirror（contenteditable）；图片上传按需。"""

        self._require_page_alive("头条号填写正文")
        editor = self.page.locator(BODY_SELECTOR)
        try:
            await editor.click(timeout=8000)
        except Exception:
            pass
        await self.page.evaluate(
            """() => {
                const el = document.querySelector('.ProseMirror');
                if (el) el.focus();
            }"""
        )

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
            platform="toutiao",
            phase="输入后",
        )
        logger.info("头条号正文文字输入并验证成功: {} 个文本段落", expected_count)

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

        self._require_page_alive("头条号上传图片")
        try:
            file_inputs = self.page.locator("input[type=file]")
            if await file_inputs.count() == 0:
                return {"success": False, "error": "头条号图片上传控件未找到"}
            before = await self.page.evaluate(
                """() => document.querySelectorAll('.ProseMirror img').length"""
            )
            await file_inputs.first.set_input_files(str(image_path), timeout=15000)
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
                    "BROWSER_CONTEXT_CLOSED: 头条号上传图片时页面已关闭"
                ) from exc
            return {"success": False, "error": str(exc)}

    async def select_topic(
        self,
        topic: str = "",
        community: str = "",
        selection_query: str = "",
        selection_override: dict | None = None,
    ):
        """头条号草稿自动保存不需要话题；公开话题选择尚未接入，如实报告。"""

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
            "error": "头条号话题选择尚未接入（自动保存草稿不需要话题）",
            "selection": {},
        }

    async def save_draft(self, title: str = "") -> str:
        """头条号「草稿将自动保存」；验证 = 自动保存接口成功响应 + 草稿箱标题。

        2026-08 实测：自动保存接口 POST /mp/agw/article/publish?type=article
        （save=0）在自动化环境下对带正文请求返回 7050「保存失败」（风控），
        仅空正文可保存成功；因此本方法按真实结果如实失败，绝不假成功。
        """

        self._require_page_alive("头条号保存草稿")
        captured: dict = {}

        async def _on_response(response) -> None:
            try:
                if response.request.method in ("POST", "PUT", "PATCH") and (
                    "/mp/agw/article/publish" in response.url
                ):
                    captured["status"] = response.status
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            captured["code"] = body.get("code")
                            captured["err_no"] = body.get("err_no")
                            captured["reason"] = body.get("reason")
                    except Exception:
                        pass
            except Exception:  # noqa: BLE001
                pass

        try:
            self.page.on("response", _on_response)
            # 不手动触发内容变化：头条为纯自动保存，输入过程已自然触发。
            # 若此前未发生任何保存请求，等待一个防抖周期后如实失败。
            for _ in range(25):
                if captured.get("status"):
                    break
                await asyncio.sleep(2)
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if not captured.get("status"):
            logger.error("头条号保存草稿未产生任何自动保存请求")
            return ""
        if captured.get("err_no"):
            logger.error(
                "头条号自动保存被拒: code={} err_no={} reason={}",
                captured.get("code"),
                captured.get("err_no"),
                captured.get("reason"),
            )
            return ""

        # 草稿箱验证：标题关键字出现（成功判据）
        try:
            await self.page.goto(
                DRAFT_BOX_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            keyword = str(title or "").strip()[:12]
            if not keyword:
                logger.error("头条号草稿验证缺少标题关键字")
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
                    "头条号草稿箱未找到标题包含「{}」的草稿（status={} err_no={}）",
                    keyword,
                    captured.get("status"),
                    captured.get("err_no"),
                )
                return ""
            logger.info(
                "头条号草稿验证成功: 草稿箱出现标题「{}」，自动保存 status={} err_no={}",
                keyword,
                captured.get("status"),
                captured.get("err_no"),
            )
            return DRAFT_BOX_URL
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            if self._exception_means_browser_closed(exc):
                raise BrowserLifecycleError(
                    "BROWSER_CONTEXT_CLOSED: 头条号验证草稿时页面已关闭"
                ) from exc
            logger.error("头条号草稿验证失败: {}", exc)
            return ""

    async def publish_now(self, title: str = "") -> str:
        raise PlatformNotImplementedError("头条号公开发布未开启")
