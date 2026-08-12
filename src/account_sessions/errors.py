"""账号会话域稳定错误码。"""


class AccountSessionError(RuntimeError):
    error_code = "ACCOUNT_SESSION_ERROR"
    http_status = 400

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class AccountNotFoundError(AccountSessionError):
    error_code = "ACCOUNT_NOT_FOUND"
    http_status = 404


class AccountPlatformMismatchError(AccountSessionError):
    error_code = "ACCOUNT_PLATFORM_MISMATCH"
    http_status = 409


class AccountUnavailableError(AccountSessionError):
    error_code = "ACCOUNT_SESSION_UNAVAILABLE"
    http_status = 409


class AccountIdentityError(AccountSessionError):
    error_code = "ACCOUNT_IDENTITY_UNVERIFIED"
    http_status = 409


class AccountBusyError(AccountSessionError):
    error_code = "ACCOUNT_BUSY"
    http_status = 409


class PublicPublishDisabledError(AccountSessionError):
    error_code = "PUBLIC_PUBLISH_DISABLED"
    http_status = 403


class ConfirmationInvalidError(AccountSessionError):
    error_code = "PUBLISH_CONFIRMATION_INVALID"
    http_status = 409


class ConfirmationRequiredError(AccountSessionError):
    error_code = "PUBLISH_CONFIRMATION_REQUIRED"
    http_status = 428

    def __init__(self, token: str, expires_at: str, summary: dict) -> None:
        super().__init__("公开发布需要明确的二次确认")
        self.token = token
        self.expires_at = expires_at
        self.summary = summary
