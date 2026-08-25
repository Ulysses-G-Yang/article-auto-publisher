"""只读核验草稿用例（方案二）。

执行单失败/未知后，业务人员在界面一键触发只读核验：用该账号隔离 Profile
（带租约）只读打开平台草稿箱，按标题查唯一草稿，返回"草稿在/不在 + 结构
摘要"，业务人员自行判断，无需打开终端。

安全边界：
- 标题从执行单读取，不允许调用方传标题（防止探测无关草稿）。
- 全程拦截非 GET 请求；不点保存/发布/删除；不清 Cookie；不自动登录。
- Profile 租约复用（与投递/心跳共用 AccountProfileLease），PROFILE_IN_USE
  返回错误码而非强抢。
- 平台未实现只读钩子 → PROBE_UNSUPPORTED_PLATFORM（fail-closed）。
- 结果不落平台侧数据，仅写 account_activity 审计（action=DRAFT_PROBE）。
"""

from __future__ import annotations

import logging

from account_sessions.errors import AccountSessionError
from account_sessions.permissions import AccessContext

LOGGER = logging.getLogger(__name__)

PROBE_UNSUPPORTED = "PROBE_UNSUPPORTED_PLATFORM"
PROBE_TITLE_MISSING = "PROBE_TITLE_MISSING"
PROBE_ACCOUNT_INACTIVE = "PROBE_ACCOUNT_INACTIVE"
PROBE_NOT_FOUND = "PROBE_NOT_FOUND"
PROBE_TITLE_AMBIGUOUS = "PROBE_TITLE_AMBIGUOUS"
PROBE_RESULT_UNKNOWN = "PROBE_RESULT_UNKNOWN"


class DraftVerifyService:
    """只读核验编排；依赖 DeliveryService 的账号/租约/平台工厂。"""

    def __init__(
        self,
        accounts,
        delivery,
        *,
        platform_factory=None,
        access: AccessContext | None = None,
    ) -> None:
        self.accounts = accounts
        self.delivery = delivery
        self.platform_factory = platform_factory or getattr(delivery, "platform_factory", None)
        self.access = access

    async def verify_draft(self, operation_id: str, access: AccessContext) -> dict:
        """按执行单标题只读核验草稿，同步返回结果。

        Args:
            operation_id: 投递执行单 ID（标题/平台/账号从此读取）。
            access: 调用方上下文（必须拥有 draft.create + session.read）。

        Raises:
            AccountSessionError: 执行单不存在/账号不可用等稳定错误。
        """
        operation, account = await self._load_operation(operation_id)
        access.require("draft.create", account.account_id)
        access.require("session.read", account.account_id)

        title = str(operation.title or "").strip()
        if not title:
            raise AccountSessionError(
                "执行单缺少标题，拒绝探测",
                error_code=PROBE_TITLE_MISSING,
                http_status=409,
            )
        if account.status != "ACTIVE":
            raise AccountSessionError(
                "账号非活动状态，请先恢复账号",
                error_code=PROBE_ACCOUNT_INACTIVE,
                http_status=409,
            )

        platform = self._build_platform(account)
        if platform is None:
            raise AccountSessionError(
                f"平台 {operation.platform} 未实现",
                error_code=PROBE_UNSUPPORTED,
                http_status=409,
            )

        try:
            with self.accounts._lease(account, purpose="VERIFY"):
                await platform.initialize()
                try:
                    result = await platform.verify_draft_readonly(title)
                finally:
                    try:
                        await platform.cleanup()
                    except Exception:
                        LOGGER.warning("只读核验后的平台清理失败（保持业务结果）")
        except AccountSessionError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "只读核验异常: platform=%s error=%s",
                operation.platform,
                type(exc).__name__,
            )
            raise AccountSessionError(
                "只读核验异常，请稍后重试",
                error_code=PROBE_RESULT_UNKNOWN,
                http_status=409,
            ) from exc

        await self._record_probe(operation_id, account, access, result)
        return {
            "operation_id": operation_id,
            "platform": operation.platform,
            **self._normalize_result(result),
        }

    async def _load_operation(self, operation_id: str):
        load = getattr(self.delivery, "_load_operation", None)
        if load is None:
            raise AccountSessionError(
                "投递服务未实现 _load_operation",
                error_code=PROBE_UNSUPPORTED,
            )
        operation, account = await load(operation_id)
        return operation, account

    def _build_platform(self, account):
        if self.platform_factory is None:
            return None
        try:
            return self.platform_factory(account)
        except Exception:
            LOGGER.warning("平台工厂构造失败: platform=%s", account.platform)
            return None

    @staticmethod
    def _normalize_result(result: dict) -> dict:
        """把平台只读核验结果投影为稳定契约。"""
        if not isinstance(result, dict):
            return {
                "title_matched": False,
                "match_count": 0,
                "error_code": PROBE_RESULT_UNKNOWN,
                "error_message": "核验结果格式无效",
            }
        if result.get("unsupported") is True:
            return {
                "title_matched": False,
                "match_count": 0,
                "error_code": PROBE_UNSUPPORTED,
                "error_message": "该平台尚未实现只读核验",
            }
        error_code = result.get("error_code")
        if error_code:
            return {
                "title_matched": False,
                "match_count": int(result.get("match_count") or 0),
                "error_code": error_code,
                "error_message": result.get("error_message") or "只读核验失败",
            }
        title_matched = bool(result.get("title_matched"))
        match_count = int(result.get("match_count") or 0)
        if title_matched and match_count == 1:
            return {
                "title_matched": True,
                "match_count": 1,
                "draft_url": result.get("draft_url"),
                "structure": result.get("structure") or {},
            }
        if match_count > 1:
            return {
                "title_matched": False,
                "match_count": match_count,
                "error_code": PROBE_TITLE_AMBIGUOUS,
                "error_message": f"草稿箱存在 {match_count} 个同名草稿，需要人工区分",
            }
        return {
            "title_matched": False,
            "match_count": 0,
            "error_code": PROBE_NOT_FOUND,
            "error_message": "平台草稿箱未找到该标题草稿",
        }

    async def _record_probe(
        self,
        operation_id: str,
        account,
        access: AccessContext,
        result: dict,
    ) -> None:
        """写脱敏审计记录；失败不阻塞响应。"""
        try:
            from account_sessions.account_service import activity_for

            error_code = result.get("error_code")
            action = "DRAFT_PROBE" if not error_code else f"DRAFT_PROBE_{error_code}"
            message = "只读核验：草稿存在" if not error_code and result.get("title_matched") else (
                "只读核验：未找到草稿" if error_code == PROBE_NOT_FOUND else
                "只读核验：" + str(result.get("error_message") or "完成")
            )
            async with self.accounts.database.session() as session:
                session.add(
                    activity_for(
                        account,
                        access,
                        action=action,
                        level="INFO" if not error_code else "WARN",
                        message=message,
                        operation_id=operation_id,
                    )
                )
        except Exception:
            LOGGER.warning("只读核验审计记录失败（不影响核验结果）")
