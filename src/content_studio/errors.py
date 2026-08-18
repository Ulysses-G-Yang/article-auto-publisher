"""Content Studio 稳定错误码。"""


class ContentStudioError(RuntimeError):
    error_code = "CONTENT_STUDIO_ERROR"
    http_status = 400

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class DraftNotFoundError(ContentStudioError):
    error_code = "DRAFT_NOT_FOUND"
    http_status = 404


class DraftRevisionConflictError(ContentStudioError):
    error_code = "DRAFT_REVISION_CONFLICT"
    http_status = 409

    def __init__(self, server_draft: dict) -> None:
        super().__init__("草稿已在其他页面更新，请选择服务端版本或另存副本")
        self.server_draft = server_draft


class DraftContentSchemaConflictError(ContentStudioError):
    """拒绝用 v1 客户端覆盖已保存的 v2 canonical 文档。"""

    error_code = "DRAFT_CONTENT_SCHEMA_CONFLICT"
    http_status = 409

    def __init__(self, server_draft: dict | None = None) -> None:
        super().__init__(
            "当前草稿使用 v2 富文档，旧版编辑器不能保存；请使用支持 v2 的编辑器"
        )
        self.server_draft = server_draft


class DraftValidationError(ContentStudioError):
    error_code = "DRAFT_VALIDATION_FAILED"
    http_status = 422


class DraftTargetConflictError(ContentStudioError):
    error_code = "DRAFT_TARGET_DUPLICATE"
    http_status = 409


class ContentAssetError(ContentStudioError):
    error_code = "CONTENT_ASSET_INVALID"
    http_status = 422


class DeliveryPlanNotFoundError(ContentStudioError):
    error_code = "DELIVERY_PLAN_NOT_FOUND"
    http_status = 404


class DeliveryPlanStaleError(ContentStudioError):
    error_code = "DELIVERY_PLAN_STALE"
    http_status = 409


class DraftBatchConfirmationRequiredError(ContentStudioError):
    error_code = "DRAFT_BATCH_CONFIRMATION_REQUIRED"
    http_status = 428
