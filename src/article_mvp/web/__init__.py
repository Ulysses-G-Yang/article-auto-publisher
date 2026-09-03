"""可挂载到现役 Flask 服务的只读数据 API。"""

from article_mvp.web.app import create_dashboard_app, create_dashboard_blueprint

__all__ = ["create_dashboard_app", "create_dashboard_blueprint"]
