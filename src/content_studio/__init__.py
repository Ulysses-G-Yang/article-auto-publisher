"""统一“创作与投递”工作台的独立内容域。"""

from content_studio.contracts import (
    CreateDeliveryPlanRequest,
    CreateDraftRequest,
    ExecuteDeliveryPlanRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)


def create_content_studio_blueprint(**kwargs):
    """延迟导入 Flask 组合层，避免内容模型与账号 Blueprint 循环依赖。"""

    from content_studio.web import create_content_studio_blueprint as factory

    return factory(**kwargs)


__all__ = [
    "CreateDeliveryPlanRequest",
    "CreateDraftRequest",
    "ExecuteDeliveryPlanRequest",
    "PatchDraftRequest",
    "ReplaceTargetsRequest",
    "create_content_studio_blueprint",
]
