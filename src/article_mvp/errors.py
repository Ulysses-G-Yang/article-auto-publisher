"""MVP 内部使用的稳定异常与错误码。"""


class MvpError(RuntimeError):
    """所有可分类业务异常的基类。"""

    error_code = "MVP_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class ConfigurationError(MvpError):
    error_code = "CONFIGURATION_ERROR"


class PlatformBusyError(MvpError):
    error_code = "PLATFORM_BUSY"


class LoginRequiredError(MvpError):
    error_code = "SESSION_EXPIRED"


class PublishResultUnknownError(MvpError):
    """平台可能已经发布，调用方禁止自动重试发布动作。"""

    error_code = "PUBLISH_RESULT_UNKNOWN"


class PublishMappingConflictError(PublishResultUnknownError):
    error_code = "PUBLISH_MAPPING_CONFLICT"


class CollectorError(MvpError):
    error_code = "COLLECTOR_ERROR"


class SessionExpiredError(CollectorError):
    error_code = "SESSION_EXPIRED"


class RateLimitedError(CollectorError):
    error_code = "RATE_LIMITED"


class SchemaChangedError(CollectorError):
    error_code = "SCHEMA_CHANGED"


class CollectionNetworkError(CollectorError):
    error_code = "NETWORK_ERROR"
