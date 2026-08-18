"""后台账号会话健康心跳的策略、错误分类与数据库服务。

心跳只复用账号域已有的只读验证入口。这个模块不负责创建浏览器，也不提供
登录、扫码、Cookie 清理或 Cookie bridge 能力；生产运行时由外层 scheduler
决定是否启用。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update

from account_sessions.errors import AccountBusyError, AccountSessionError
from account_sessions.models import PlatformAccount
from account_sessions.permissions import AccessContext, PermissionDeniedError
from platforms.base import LoginRequiredError

if TYPE_CHECKING:
    from account_sessions.database import AccountDatabase


HEARTBEAT_ACTOR_ID = "account-session-heartbeat"
HEARTBEAT_SOURCE = "SYSTEM"
HEARTBEAT_ACCESS_CONTEXT = AccessContext(
    actor_id=HEARTBEAT_ACTOR_ID,
    source=HEARTBEAT_SOURCE,
    capabilities=frozenset({"session.verify"}),
)
BUSY_ERROR_CODES = frozenset({"ACCOUNT_BUSY", "PLATFORM_BUSY", "PROFILE_IN_USE"})
HEARTBEAT_CLAIM_NOT_ACQUIRED = "HEARTBEAT_CLAIM_NOT_ACQUIRED"
HEARTBEAT_CLAIM_LOST = "HEARTBEAT_CLAIM_LOST"
LOGGER = logging.getLogger(__name__)
_SAFE_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
HEARTBEAT_CLAIM_TTL_MIN = timedelta(seconds=30)
HEARTBEAT_CLAIM_TTL_MAX = timedelta(hours=1)


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，便于测试注入 clock。"""

    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """把 SQLite 读出的 naive 时间按 UTC 解释并统一为 aware 时间。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _duration(value: timedelta | int | float, name: str) -> timedelta:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是正数时长")
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        raise TypeError(f"{name} 必须是 timedelta 或秒数")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} 必须是有限的正数时长")
    return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class HeartbeatPolicy:
    """心跳 TTL、扫描节奏和退避策略。

    数值时长也可直接按秒传入，便于小范围模拟测试。所有范围在构造时严格
    校验；jitter 只会增加等待时间，不会让下一次检查提前到期。
    """

    success_ttl: timedelta | int | float = timedelta(hours=6)
    scan_interval: timedelta | int | float = timedelta(minutes=5)
    max_per_scan: int = 1
    busy_backoff: timedelta | int | float = timedelta(seconds=30)
    error_backoff_base: timedelta | int | float = timedelta(minutes=5)
    error_backoff_max: timedelta | int | float = timedelta(hours=6)
    login_required_backoff: timedelta | int | float = timedelta(hours=1)
    claim_ttl: timedelta | int | float = timedelta(minutes=10)
    jitter_ratio: float = 0.1
    jitter_seed: str = "account-session-heartbeat-v1"

    def __post_init__(self) -> None:
        success_ttl = _duration(self.success_ttl, "success_ttl")
        scan_interval = _duration(self.scan_interval, "scan_interval")
        busy_backoff = _duration(self.busy_backoff, "busy_backoff")
        error_base = _duration(self.error_backoff_base, "error_backoff_base")
        error_max = _duration(self.error_backoff_max, "error_backoff_max")
        login_backoff = _duration(self.login_required_backoff, "login_required_backoff")
        claim_ttl = _duration(self.claim_ttl, "claim_ttl")
        if not HEARTBEAT_CLAIM_TTL_MIN <= claim_ttl <= HEARTBEAT_CLAIM_TTL_MAX:
            raise ValueError("claim_ttl 必须在 30 秒到 1 小时之间")
        if error_max < error_base:
            raise ValueError("error_backoff_max 必须大于等于 error_backoff_base")
        if not isinstance(self.max_per_scan, int) or isinstance(self.max_per_scan, bool):
            raise ValueError("max_per_scan 必须是整数")
        if not 1 <= self.max_per_scan <= 100:
            raise ValueError("max_per_scan 必须在 1 到 100 之间")
        jitter_ratio = self.jitter_ratio
        if isinstance(jitter_ratio, bool) or not isinstance(jitter_ratio, (int, float)):
            raise ValueError("jitter_ratio 必须是 0 到 1 之间的数")
        if not math.isfinite(float(jitter_ratio)) or not 0 <= float(jitter_ratio) <= 1:
            raise ValueError("jitter_ratio 必须是 0 到 1 之间的数")
        if not isinstance(self.jitter_seed, str) or not self.jitter_seed:
            raise ValueError("jitter_seed 不能为空")
        object.__setattr__(self, "success_ttl", success_ttl)
        object.__setattr__(self, "scan_interval", scan_interval)
        object.__setattr__(self, "busy_backoff", busy_backoff)
        object.__setattr__(self, "error_backoff_base", error_base)
        object.__setattr__(self, "error_backoff_max", error_max)
        object.__setattr__(self, "login_required_backoff", login_backoff)
        object.__setattr__(self, "claim_ttl", claim_ttl)
        object.__setattr__(self, "jitter_ratio", float(jitter_ratio))

    @classmethod
    def from_env(cls) -> HeartbeatPolicy:
        """从可选秒数环境变量读取策略，未设置时使用安全默认值。"""

        def seconds(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} 必须是数字秒数") from exc

        max_raw = os.getenv("ACCOUNT_SESSION_HEARTBEAT_MAX_PER_SCAN", "1")
        try:
            max_per_scan = int(max_raw)
        except ValueError as exc:
            raise ValueError("ACCOUNT_SESSION_HEARTBEAT_MAX_PER_SCAN 必须是整数") from exc
        return cls(
            success_ttl=seconds("ACCOUNT_SESSION_HEARTBEAT_SUCCESS_TTL_SECONDS", 6 * 3600),
            scan_interval=seconds("ACCOUNT_SESSION_HEARTBEAT_SCAN_INTERVAL_SECONDS", 5 * 60),
            max_per_scan=max_per_scan,
            busy_backoff=seconds("ACCOUNT_SESSION_HEARTBEAT_BUSY_BACKOFF_SECONDS", 30),
            error_backoff_base=seconds(
                "ACCOUNT_SESSION_HEARTBEAT_ERROR_BACKOFF_BASE_SECONDS", 5 * 60
            ),
            error_backoff_max=seconds(
                "ACCOUNT_SESSION_HEARTBEAT_ERROR_BACKOFF_MAX_SECONDS", 6 * 3600
            ),
            login_required_backoff=seconds(
                "ACCOUNT_SESSION_HEARTBEAT_LOGIN_REQUIRED_BACKOFF_SECONDS", 3600
            ),
            claim_ttl=seconds("ACCOUNT_SESSION_HEARTBEAT_CLAIM_TTL_SECONDS", 600),
            jitter_ratio=seconds("ACCOUNT_SESSION_HEARTBEAT_JITTER_RATIO", 0.1),
        )

    def is_stale(
        self,
        last_verified_at: datetime | PlatformAccount | None,
        now: datetime | None = None,
    ) -> bool:
        if isinstance(last_verified_at, PlatformAccount):
            last_verified_at = last_verified_at.last_verified_at
        verified = as_utc(last_verified_at)
        current = as_utc(now) or utc_now()
        return verified is None or current - verified >= self.success_ttl

    def is_due(
        self,
        next_heartbeat_at: datetime | PlatformAccount | None,
        *,
        last_verified_at: datetime | None = None,
        now: datetime | None = None,
        status: str = "ACTIVE",
        heartbeat_enabled: bool = True,
        session_status: str | None = None,
    ) -> bool:
        if isinstance(next_heartbeat_at, PlatformAccount):
            account = next_heartbeat_at
            next_heartbeat_at = account.next_heartbeat_at
            status = account.status
            heartbeat_enabled = account.heartbeat_enabled
            session_status = account.session_status
        if status != "ACTIVE" or not heartbeat_enabled or session_status == "LOGIN_REQUIRED":
            return False
        current = as_utc(now) or utc_now()
        next_at = as_utc(next_heartbeat_at)
        return next_at is None or next_at <= current

    def _jitter(self, account_id: str, delay: timedelta, outcome: str) -> timedelta:
        if self.jitter_ratio == 0:
            return timedelta(0)
        digest = hashlib.sha256(
            f"{self.jitter_seed}|{account_id}|{outcome}".encode()
        ).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(2**64)
        return delay * (self.jitter_ratio * fraction)

    def calculate_next(
        self,
        now: datetime | None = None,
        account_id: str = "",
        *,
        outcome: str = "success",
        failures: int = 0,
        error_code: str | None = None,
    ) -> datetime:
        """按结果计算下一次检查时间，jitter 对同一账号和结果可复现。"""

        current = as_utc(now) or utc_now()
        normalized = (error_code or outcome or "success").upper()
        if normalized in {"SUCCESS", "SUCCEEDED"}:
            delay = self.success_ttl
            jitter_key = "SUCCESS"
        elif normalized in {"BUSY", "DEFERRED", "ACCOUNT_BUSY", "PROFILE_IN_USE"}:
            delay = self.busy_backoff
            jitter_key = "BUSY"
        elif normalized in {"LOGIN_REQUIRED", "SESSION_EXPIRED"}:
            delay = self.login_required_backoff
            jitter_key = "LOGIN_REQUIRED"
        else:
            if not isinstance(failures, int) or failures < 1:
                failures = 1
            delay = min(
                self.error_backoff_max,
                self.error_backoff_base * (2 ** min(failures - 1, 20)),
            )
            jitter_key = normalized if normalized in {"RATE_LIMITED", "CHALLENGE"} else "ERROR"
        return current + delay + self._jitter(account_id, delay, jitter_key)


@dataclass(frozen=True, slots=True)
class VerificationFailure:
    """稳定错误分类结果；message 永远不参与公开诊断。"""

    category: str
    error_code: str
    disposition: str
    preserve_session: bool

    @property
    def status(self) -> str:
        """兼容调用方将 category 作为目标状态读取。"""

        return "LOGIN_REQUIRED" if self.category == "LOGIN_REQUIRED" else self.category


_BUSY_CODES = frozenset({"ACCOUNT_BUSY", "PLATFORM_BUSY", "PROFILE_IN_USE"})
_LOGIN_CODES = frozenset({"LOGIN_REQUIRED", "SESSION_EXPIRED"})
_RATE_CODES = frozenset({"RATE_LIMITED", "TOO_MANY_REQUESTS", "RATE_LIMIT"})
_CHALLENGE_CODES = frozenset(
    {"CHALLENGE", "SECURITY_CHALLENGE", "CAPTCHA_REQUIRED", "VERIFICATION_REQUIRED"}
)
_STABLE_CODES = _BUSY_CODES | _LOGIN_CODES | _RATE_CODES | _CHALLENGE_CODES


def _canonical_stable_code(code: str) -> str | None:
    normalized = code.strip().upper()
    if _SAFE_ERROR_CODE_RE.fullmatch(normalized) is None:
        return None
    if normalized in {"SECURITY_CHALLENGE", "CAPTCHA_REQUIRED", "VERIFICATION_REQUIRED"}:
        return "CHALLENGE"
    if normalized in _STABLE_CODES:
        return normalized
    if normalized.endswith("_SECURITY_CHALLENGE") or normalized.endswith("_CHALLENGE"):
        return "CHALLENGE"
    for stable_code in _BUSY_CODES | _LOGIN_CODES | _RATE_CODES:
        if normalized.endswith(f"_{stable_code}"):
            return stable_code
    return None


def _stable_error_code(exc: BaseException) -> str:
    """提取异常类型/稳定 code/prefix，不扫描模糊自然语言。"""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AccountBusyError):
            return "ACCOUNT_BUSY"
        raw_code = getattr(current, "error_code", None)
        if isinstance(raw_code, str):
            code = raw_code.strip().upper()
            canonical = _canonical_stable_code(code)
            if canonical is not None:
                return canonical
            if (
                code
                and code not in {"PLATFORM_ERROR", "ACCOUNT_SESSION_ERROR"}
                and _SAFE_ERROR_CODE_RE.fullmatch(code) is not None
            ):
                return code[:64]
        # 现有平台基类在 PROFILE_IN_USE 分支使用稳定前缀构造消息；只取
        # 冒号前的全大写 token，绝不按“包含登录”等自然语言猜测。
        prefix = str(current).split(":", 1)[0].strip().upper()
        canonical = _canonical_stable_code(prefix)
        if canonical is not None:
            return canonical
        current = current.__cause__ or current.__context__
    return "UNKNOWN"


def classify_verification_failure(exc: BaseException) -> VerificationFailure:
    """把验证异常归为 BUSY、LOGIN_REQUIRED、暂态错误或未知错误。"""

    code = _stable_error_code(exc)
    if code in _BUSY_CODES:
        return VerificationFailure("BUSY", code, "DEFERRED", True)
    if code in _LOGIN_CODES or isinstance(exc, LoginRequiredError):
        return VerificationFailure("LOGIN_REQUIRED", code, "FAILED", False)
    if code in _RATE_CODES:
        return VerificationFailure("RATE_LIMITED", code, "FAILED", True)
    if code in _CHALLENGE_CODES:
        return VerificationFailure("CHALLENGE", code, "FAILED", True)
    return VerificationFailure("ERROR", code, "FAILED", True)


def is_heartbeat_enabled_from_env() -> bool:
    raw = os.getenv("ACCOUNT_SESSION_HEARTBEAT_ENABLED", "false").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("ACCOUNT_SESSION_HEARTBEAT_ENABLED 必须是 true/false")


VerifyCallable = Callable[..., Awaitable[Any] | Any]


class HeartbeatService:
    """只做数据库编排的心跳服务；平台验证通过 verify_callable 注入。"""

    def __init__(
        self,
        database: AccountDatabase,
        verify_callable: VerifyCallable,
        *,
        policy: HeartbeatPolicy | None = None,
        access: AccessContext = HEARTBEAT_ACCESS_CONTEXT,
        clock: Callable[[], datetime] = utc_now,
        enabled: bool | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.database = database
        self.verify_callable = verify_callable
        self.policy = policy or HeartbeatPolicy()
        self.access = access
        self.clock = clock
        self.busy_account_ids: set[str] = set()
        if owner_id is not None and not isinstance(owner_id, str):
            raise TypeError("owner_id 必须是字符串")
        resolved_owner_id = (owner_id or str(uuid.uuid4())).strip()
        if not resolved_owner_id:
            raise ValueError("owner_id 不能为空")
        if len(resolved_owner_id) > 64:
            raise ValueError("owner_id 不能超过 64 个字符")
        self.owner_id = resolved_owner_id
        self.enabled = (
            is_heartbeat_enabled_from_env() if enabled is None else bool(enabled)
        )

    async def get_health_summary(
        self,
        access: AccessContext,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """返回只读健康计数；按账号权限过滤且不返回账号级标识。"""

        current = as_utc(now) or as_utc(self.clock()) or utc_now()
        async with self.database.session() as session:
            accounts = list((await session.scalars(select(PlatformAccount))).all())
        visible = []
        for account in accounts:
            if account.status != "ACTIVE":
                continue
            try:
                access.require("session.read", account.account_id)
            except PermissionDeniedError:
                continue
            visible.append(account)

        last_heartbeat_values = [
            as_utc(account.last_heartbeat_at)
            for account in visible
            if account.last_heartbeat_at is not None
        ]
        next_heartbeat_values = [
            as_utc(account.next_heartbeat_at)
            for account in visible
            if account.heartbeat_enabled
            and account.session_status != "LOGIN_REQUIRED"
            and account.next_heartbeat_at is not None
        ]
        login_required = sum(account.session_status == "LOGIN_REQUIRED" for account in visible)
        busy = sum(
            account.account_id in self.busy_account_ids
            or account.last_heartbeat_error_code in BUSY_ERROR_CODES
            for account in visible
        )
        stale = sum(
            account.session_status != "LOGIN_REQUIRED"
            and (
                account.session_status != "VALID"
                or self.policy.is_stale(account.last_verified_at, current)
            )
            for account in visible
        )
        valid = sum(account.session_status == "VALID" for account in visible)
        due = sum(self.policy.is_due(account, now=current) for account in visible)
        return {
            "heartbeat_enabled": self.enabled,
            "total_accounts": len(visible),
            "valid": valid,
            "stale": stale,
            "due": due,
            "login_required": login_required,
            "busy": busy,
            "last_heartbeat_at": _iso(
                max(last_heartbeat_values) if last_heartbeat_values else None
            ),
            "next_heartbeat_at": _iso(
                min(next_heartbeat_values) if next_heartbeat_values else None
            ),
            "policy": {
                "success_ttl_seconds": self.policy.success_ttl.total_seconds(),
                "scan_interval_seconds": self.policy.scan_interval.total_seconds(),
                "max_per_scan": self.policy.max_per_scan,
                "busy_backoff_seconds": self.policy.busy_backoff.total_seconds(),
                "error_backoff_base_seconds": self.policy.error_backoff_base.total_seconds(),
                "error_backoff_max_seconds": self.policy.error_backoff_max.total_seconds(),
                "login_required_backoff_seconds": (
                    self.policy.login_required_backoff.total_seconds()
                ),
                "claim_ttl_seconds": self.policy.claim_ttl.total_seconds(),
                "jitter_ratio": self.policy.jitter_ratio,
            },
        }

    def _claim_available_clause(self, now: datetime):
        """返回可抢占条件；异常的无过期时间 claim 按占用处理。"""

        return or_(
            PlatformAccount.heartbeat_claim_owner.is_(None),
            and_(
                PlatformAccount.heartbeat_claim_expires_at.is_not(None),
                PlatformAccount.heartbeat_claim_expires_at <= now,
            ),
        )

    async def _claim_account(self, account_id: str, now: datetime) -> bool:
        """用单条条件 UPDATE 抢占账号，rowcount=1 才能访问 Profile。"""

        next_at = or_(
            PlatformAccount.next_heartbeat_at.is_(None),
            PlatformAccount.next_heartbeat_at <= now,
        )
        statement = (
            update(PlatformAccount)
            .where(
                PlatformAccount.account_id == account_id,
                PlatformAccount.status == "ACTIVE",
                PlatformAccount.heartbeat_enabled.is_(True),
                PlatformAccount.session_status != "LOGIN_REQUIRED",
                next_at,
                self._claim_available_clause(now),
            )
            .values(
                heartbeat_claim_owner=self.owner_id,
                heartbeat_claimed_at=now,
                heartbeat_claim_expires_at=now + self.policy.claim_ttl,
            )
        )
        async with self.database.session() as session:
            result = await session.execute(statement)
            return result.rowcount == 1

    async def _release_claim(self, account_id: str) -> bool:
        """只释放当前 owner 的 claim；异常路径使用 best-effort。"""

        statement = (
            update(PlatformAccount)
            .where(
                PlatformAccount.account_id == account_id,
                PlatformAccount.heartbeat_claim_owner == self.owner_id,
            )
            .values(
                heartbeat_claim_owner=None,
                heartbeat_claimed_at=None,
                heartbeat_claim_expires_at=None,
            )
        )
        async with self.database.session() as session:
            result = await session.execute(statement)
            return result.rowcount == 1

    async def recover_stale_state(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """清理过期租约并恢复明确超时的 VERIFYING 状态。

        恢复不会无条件覆盖人工登录流程：只有带有明确且已过期 heartbeat
        claim 的 VERIFYING 账号才会恢复；没有 claim 来源的遗留 VERIFYING
        保持不变。该方法只操作数据库，不打开浏览器、不杀进程、不删除锁文件。
        """

        current = as_utc(now) or as_utc(self.clock()) or utc_now()
        async with self.database.session() as session:
            expired_verifying = and_(
                PlatformAccount.status == "ACTIVE",
                PlatformAccount.session_status == "VERIFYING",
                PlatformAccount.heartbeat_claim_expires_at.is_not(None),
                PlatformAccount.heartbeat_claim_expires_at <= current,
            )
            recovered_valid = (
                update(PlatformAccount)
                .where(expired_verifying, PlatformAccount.last_verified_at.is_not(None))
                .values(
                    session_status="VALID",
                    next_heartbeat_at=current,
                    heartbeat_claim_owner=None,
                    heartbeat_claimed_at=None,
                    heartbeat_claim_expires_at=None,
                    updated_at=current,
                )
            )
            recovered_unverified = (
                update(PlatformAccount)
                .where(expired_verifying, PlatformAccount.last_verified_at.is_(None))
                .values(
                    session_status="UNVERIFIED",
                    next_heartbeat_at=current,
                    heartbeat_claim_owner=None,
                    heartbeat_claimed_at=None,
                    heartbeat_claim_expires_at=None,
                    updated_at=current,
                )
            )
            valid_result = await session.execute(recovered_valid)
            unverified_result = await session.execute(recovered_unverified)
            clear_expired = (
                update(PlatformAccount)
                .where(
                    PlatformAccount.heartbeat_claim_expires_at.is_not(None),
                    PlatformAccount.heartbeat_claim_expires_at <= current,
                )
                .values(
                    heartbeat_claim_owner=None,
                    heartbeat_claimed_at=None,
                    heartbeat_claim_expires_at=None,
                )
            )
            expired_result = await session.execute(clear_expired)
        return {
            "expired_claims": (expired_result.rowcount or 0)
            + (valid_result.rowcount or 0)
            + (unverified_result.rowcount or 0),
            "recovered_valid": valid_result.rowcount or 0,
            "recovered_unverified": unverified_result.rowcount or 0,
            "recovered_verifying": (valid_result.rowcount or 0)
            + (unverified_result.rowcount or 0),
        }

    async def due_account_ids(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[str]:
        current = as_utc(now) or as_utc(self.clock()) or utc_now()
        bounded = (
            self.policy.max_per_scan
            if limit is None
            else min(limit, self.policy.max_per_scan)
        )
        if bounded < 1:
            return []
        async with self.database.session() as session:
            statement = (
                select(PlatformAccount.account_id)
                .where(
                    PlatformAccount.status == "ACTIVE",
                    PlatformAccount.heartbeat_enabled.is_(True),
                    PlatformAccount.session_status != "LOGIN_REQUIRED",
                    or_(
                        PlatformAccount.next_heartbeat_at.is_(None),
                        PlatformAccount.next_heartbeat_at <= current,
                    ),
                    self._claim_available_clause(current),
                )
                .order_by(PlatformAccount.next_heartbeat_at, PlatformAccount.account_id)
                .limit(bounded)
            )
            return list((await session.scalars(statement)).all())

    async def scan_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """扫描有限批次；账号验证严格逐个串行执行。"""

        current = as_utc(now) or as_utc(self.clock()) or utc_now()
        account_ids = await self.due_account_ids(now=current)
        results: list[dict[str, Any]] = []
        for account_id in account_ids:
            results.append(await self.check_account(account_id, now=current))
        counts = {
            "succeeded": sum(item["outcome"] == "SUCCEEDED" for item in results),
            "failed": sum(item["outcome"] == "FAILED" for item in results),
            "deferred": sum(item["outcome"] == "DEFERRED" for item in results),
        }
        return {
            "processed": len(results),
            **counts,
            "results": results,
        }

    async def check_account(
        self,
        account_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = as_utc(now) or as_utc(self.clock()) or utc_now()
        async with self.database.session() as session:
            account = await session.get(PlatformAccount, account_id)
            if account is None:
                return {"account_id": account_id, "outcome": "SKIPPED", "error_code": "NOT_FOUND"}
            if not self.policy.is_due(account, now=current):
                return {"account_id": account_id, "outcome": "SKIPPED", "error_code": None}

        if not await self._claim_account(account_id, current):
            return {
                "account_id": account_id,
                "outcome": "SKIPPED",
                "error_code": HEARTBEAT_CLAIM_NOT_ACQUIRED,
            }

        async with self.database.session() as session:
            claimed_account = await session.scalar(
                select(PlatformAccount).where(
                    PlatformAccount.account_id == account_id,
                    PlatformAccount.heartbeat_claim_owner == self.owner_id,
                )
            )
            if claimed_account is None:
                # 极端情况下 claim 可能在读取前被接管；不要访问 Profile。
                return {
                    "account_id": account_id,
                    "outcome": "SKIPPED",
                    "error_code": HEARTBEAT_CLAIM_LOST,
                }
            original_status = claimed_account.session_status
            original_verified = claimed_account.last_verified_at

        self.busy_account_ids.add(account_id)
        record_completed = False
        try:
            try:
                result = await self._invoke_verify(account_id)
                if result is False:
                    raise AccountSessionError("验证未通过", error_code="LOGIN_REQUIRED")
            except Exception as exc:
                classification = classify_verification_failure(exc)
                recorded = await self._record_failure(
                    account_id,
                    current,
                    classification,
                    original_status=original_status,
                    original_verified=original_verified,
                )
                record_completed = True
                return recorded
            recorded = await self._record_success(account_id, current)
            record_completed = True
            return recorded
        finally:
            self.busy_account_ids.discard(account_id)
            if not record_completed:
                try:
                    await self._release_claim(account_id)
                except Exception:
                    LOGGER.warning("账号会话心跳 claim 释放失败（仅记录稳定状态）")

    async def _invoke_verify(self, account_id: str) -> Any:
        """调用冻结的只读验证契约，不猜测其他签名。"""

        value = self.verify_callable(
            account_id,
            self.access,
            allow_interactive_login=False,
        )
        if hasattr(value, "__await__"):
            # 让只读验证在 claim TTL 到期前结束，避免旧 verifier 在租约
            # 被接管后继续把内部状态写回。同步注入函数仍须由调用方自行限时。
            timeout = max(0.1, self.policy.claim_ttl.total_seconds() * 0.8)
            try:
                return await asyncio.wait_for(value, timeout=timeout)
            except TimeoutError as exc:
                raise AccountSessionError("会话验证超时", error_code="VERIFY_TIMEOUT") from exc
        return value

    async def _record_success(self, account_id: str, now: datetime) -> dict[str, Any]:
        next_at = self.policy.calculate_next(now, account_id, outcome="success")
        async with self.database.session() as session:
            account = await session.scalar(
                select(PlatformAccount).where(
                    PlatformAccount.account_id == account_id,
                    PlatformAccount.heartbeat_claim_owner == self.owner_id,
                )
            )
            if account is None:
                return {
                    "account_id": account_id,
                    "outcome": "CLAIM_LOST",
                    "error_code": HEARTBEAT_CLAIM_LOST,
                }
            statement = (
                update(PlatformAccount)
                .where(
                    PlatformAccount.account_id == account_id,
                    PlatformAccount.heartbeat_claim_owner == self.owner_id,
                )
                .values(
                    session_status="VALID",
                    last_verified_at=now,
                    last_heartbeat_at=now,
                    heartbeat_failures=0,
                    last_heartbeat_error_code=None,
                    next_heartbeat_at=next_at,
                    heartbeat_claim_owner=None,
                    heartbeat_claimed_at=None,
                    heartbeat_claim_expires_at=None,
                    updated_at=now,
                )
            )
            result = await session.execute(statement)
            if result.rowcount != 1:
                return {
                    "account_id": account_id,
                    "outcome": "CLAIM_LOST",
                    "error_code": HEARTBEAT_CLAIM_LOST,
                }
            from account_sessions.account_service import activity_for

            session.add(
                activity_for(
                    account,
                    self.access,
                    action="HEARTBEAT_SUCCEEDED",
                    message="账号会话健康检查成功",
                )
            )
        return {
            "account_id": account_id,
            "outcome": "SUCCEEDED",
            "error_code": None,
            "next_heartbeat_at": next_at,
        }

    async def _record_failure(
        self,
        account_id: str,
        now: datetime,
        classification: VerificationFailure,
        *,
        original_status: str,
        original_verified: datetime | None,
    ) -> dict[str, Any]:
        async with self.database.session() as session:
            account = await session.scalar(
                select(PlatformAccount).where(
                    PlatformAccount.account_id == account_id,
                    PlatformAccount.heartbeat_claim_owner == self.owner_id,
                )
            )
            if account is None:
                return {
                    "account_id": account_id,
                    "outcome": "CLAIM_LOST",
                    "error_code": HEARTBEAT_CLAIM_LOST,
                }
            next_at = self.policy.calculate_next(
                now,
                account_id,
                outcome=classification.category,
                failures=(account.heartbeat_failures or 0) + 1,
                error_code=classification.error_code,
            )
            if classification.category == "BUSY":
                # 忙碌只表示本轮没有取得租约；不改变 VALID 或最近成功验证时间。
                next_status = _restore_session_status(original_status, original_verified)
                next_verified = original_verified
                action = "HEARTBEAT_DEFERRED"
                level = "WARNING"
                outcome = "DEFERRED"
                message = f"账号会话健康检查已延后（{classification.error_code}）"
                next_failures = account.heartbeat_failures or 0
            else:
                next_failures = (account.heartbeat_failures or 0) + 1
                if classification.category == "LOGIN_REQUIRED":
                    next_status = "LOGIN_REQUIRED"
                    next_verified = None
                elif classification.preserve_session:
                    next_status = _restore_session_status(
                        original_status, original_verified
                    )
                    next_verified = original_verified
                else:
                    next_status = account.session_status
                    next_verified = account.last_verified_at
                action = "HEARTBEAT_FAILED"
                level = "ERROR"
                outcome = "FAILED"
                message = f"账号会话健康检查失败（{classification.error_code}）"
            statement = (
                update(PlatformAccount)
                .where(
                    PlatformAccount.account_id == account_id,
                    PlatformAccount.heartbeat_claim_owner == self.owner_id,
                )
                .values(
                    session_status=next_status,
                    last_verified_at=next_verified,
                    next_heartbeat_at=next_at,
                    last_heartbeat_at=now,
                    last_heartbeat_error_code=classification.error_code,
                    heartbeat_failures=next_failures,
                    heartbeat_claim_owner=None,
                    heartbeat_claimed_at=None,
                    heartbeat_claim_expires_at=None,
                    updated_at=now,
                )
            )
            result = await session.execute(statement)
            if result.rowcount != 1:
                return {
                    "account_id": account_id,
                    "outcome": "CLAIM_LOST",
                    "error_code": HEARTBEAT_CLAIM_LOST,
                }
            from account_sessions.account_service import activity_for

            session.add(
                activity_for(
                    account,
                    self.access,
                    action=action,
                    level=level,
                    message=message,
                )
            )
        return {
            "account_id": account_id,
            "outcome": outcome,
            "error_code": classification.error_code,
            "next_heartbeat_at": next_at,
        }


# 直观的别名，便于外层 runtime 与测试按“账号会话健康”命名引用。
AccountSessionHealthService = HeartbeatService


class HeartbeatScheduler:
    """可停止、非重入、单账号串行的后台扫描任务。"""

    def __init__(
        self,
        service: HeartbeatService,
        *,
        enabled: bool | None = None,
        policy: HeartbeatPolicy | None = None,
    ) -> None:
        self.service = service
        self.policy = policy or service.policy
        self.enabled = (
            is_heartbeat_enabled_from_env() if enabled is None else bool(enabled)
        )
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._scan_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """启动一次后台 task；重复 start 不创建第二个 task。"""

        if self.running:
            return False
        recover = getattr(self.service, "recover_stale_state", None)
        if recover is not None:
            await recover()
        if not self.enabled:
            return False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="account-session-heartbeat")
        return True

    async def stop(self) -> bool:
        """设置停止信号并立即唤醒、等待后台 task 完成。"""

        task = self._task
        stop_event = self._stop_event
        if task is None:
            return False
        if stop_event is not None:
            stop_event.set()
        if task is not asyncio.current_task():
            await task
        self._task = None
        self._stop_event = None
        return True

    async def scan_once(self) -> dict[str, Any]:
        """单次扫描入口；并发调用只允许一个进入服务。"""

        if self._scan_lock.locked():
            return {"processed": 0, "skipped": "NON_REENTRANT"}
        async with self._scan_lock:
            return await self.service.scan_once()

    async def _run(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.policy.scan_interval.total_seconds()
                    )
                except TimeoutError:
                    pass
                if stop_event.is_set():
                    break
                try:
                    await self.scan_once()
                except Exception as exc:
                    error_code = classify_verification_failure(exc).error_code
                    LOGGER.warning("账号会话心跳扫描失败（稳定码=%s）", error_code)
        except asyncio.CancelledError:
            raise


def _restore_session_status(original_status: str, verified_at: datetime | None) -> str:
    """把一次性 VERIFYING 标记恢复为之前可观察的会话状态。"""

    if original_status == "VERIFYING":
        return "VALID" if verified_at is not None else "UNVERIFIED"
    return original_status


__all__ = [
    "AccountSessionHealthService",
    "HEARTBEAT_ACCESS_CONTEXT",
    "HeartbeatPolicy",
    "HeartbeatScheduler",
    "HeartbeatService",
    "BUSY_ERROR_CODES",
    "HEARTBEAT_CLAIM_LOST",
    "HEARTBEAT_CLAIM_NOT_ACQUIRED",
    "VerificationFailure",
    "as_utc",
    "classify_verification_failure",
    "is_heartbeat_enabled_from_env",
    "utc_now",
]
