"""按规范化 Profile 路径的跨进程租约。"""

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from account_sessions.errors import AccountBusyError
from account_sessions.models import PlatformAccount
from account_sessions.runtime_paths import default_root, legacy_profile_path
from article_mvp.platforms.locks import PlatformFileLock


class AccountProfileLease:
    def __init__(
        self,
        account: PlatformAccount,
        *,
        purpose: str,
        allowed_profile_roots: tuple[str | Path, ...] | None = None,
        acquire_legacy_guard: Callable[[str], bool] | None = None,
        release_legacy_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.account = account
        self.purpose = purpose
        self.acquire_legacy_guard = acquire_legacy_guard
        self.release_legacy_guard = release_legacy_guard
        self.allowed_profile_roots = allowed_profile_roots
        self.profile_path = self._validate_profile_path()
        digest = hashlib.sha256(
            os.path.normcase(str(self.profile_path)).encode("utf-8")
        ).hexdigest()
        self.file_lock = PlatformFileLock(default_root() / "locks" / f"{digest}.lock")
        self._legacy_guarded = False

    def _validate_profile_path(self) -> Path:
        path = Path(self.account.profile_path).resolve()
        allowed_roots = {
            Path(root).expanduser().resolve()
            for root in (
                self.allowed_profile_roots
                or (
                    default_root() / "profiles",
                    legacy_profile_path(self.account.platform).parent,
                )
            )
        }
        if not any(_is_within(path, root) for root in allowed_roots):
            raise AccountBusyError("账号 Profile 不在允许的运行目录中")
        if not path.is_dir():
            raise AccountBusyError("账号 Profile 不存在")
        return path

    def acquire(self) -> None:
        try:
            self.file_lock.acquire()
        except Exception as exc:
            raise AccountBusyError("账号正在被另一个流程使用") from exc

        try:
            if self.account.is_legacy_profile and self.acquire_legacy_guard:
                if not self.acquire_legacy_guard(self.account.platform):
                    raise AccountBusyError("现役发布流程正在使用该平台账号")
                self._legacy_guarded = True

            occupied = [
                name
                for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
                if (self.profile_path / name).exists()
            ]
            if occupied:
                raise AccountBusyError("账号浏览器 Profile 正在被 Chrome 占用")
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        if self._legacy_guarded and self.release_legacy_guard:
            self.release_legacy_guard(self.account.platform)
        self._legacy_guarded = False
        self.file_lock.release()

    def __enter__(self) -> "AccountProfileLease":
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False
