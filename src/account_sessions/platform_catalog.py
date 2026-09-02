"""面向前端的平台目录与运行能力声明。

目录可以提前展示规划平台，但账号和投递能力必须分别显式开启。任何未开启的
平台都不能进入账号服务或平台适配器工厂，避免未知平台错误回落到 ZOL。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class PlatformCatalogItem:
    """一个稳定的平台展示与能力项。"""

    id: str
    display_name: str
    logo_url: str
    status: str
    delivery_enabled: bool
    account_enabled: bool
    sort_order: int

    def public_dict(self) -> dict[str, str | bool | int]:
        """返回不含适配器实现和本机路径的公开字段。"""

        return asdict(self)


def _item(
    platform_id: str,
    display_name: str,
    sort_order: int,
    *,
    account_enabled: bool = False,
    delivery_enabled: bool = False,
) -> PlatformCatalogItem:
    return PlatformCatalogItem(
        id=platform_id,
        display_name=display_name,
        logo_url=f"/static/img/platforms/{platform_id}.svg",
        status="AVAILABLE" if account_enabled or delivery_enabled else "COMING_SOON",
        delivery_enabled=delivery_enabled,
        account_enabled=account_enabled,
        sort_order=sort_order,
    )


PLATFORM_CATALOG: tuple[PlatformCatalogItem, ...] = (
    _item("xiaoheihe", "小黑盒", 10, delivery_enabled=True, account_enabled=True),
    _item("zol", "中关村在线", 20, delivery_enabled=True, account_enabled=True),
    _item("zhihu", "知乎", 30, delivery_enabled=True, account_enabled=True),
    _item("weibo", "微博", 40, delivery_enabled=True, account_enabled=True),
    _item("smzdm", "什么值得买", 50, delivery_enabled=True, account_enabled=True),
    _item("toutiao", "头条号", 60, delivery_enabled=True, account_enabled=True),
    _item("baijiahao", "百家号", 70, delivery_enabled=True, account_enabled=True),
    # 网页端长文“草稿箱”经跨浏览器复核只存在于隔离 Profile 本地，
    # 不是账号云端草稿。保留登录/账号管理，关闭自动投递，避免假成功。
    _item("xiaohongshu", "小红书", 80, account_enabled=True),
    _item("douyin", "抖音", 90, account_enabled=True),
    _item("wechat_mp", "微信公众号", 100),
)

ACCOUNT_ENABLED_PLATFORMS: tuple[str, ...] = tuple(
    item.id for item in PLATFORM_CATALOG if item.account_enabled
)
DELIVERY_ENABLED_PLATFORMS: tuple[str, ...] = tuple(
    item.id for item in PLATFORM_CATALOG if item.delivery_enabled
)


def public_platform_catalog() -> list[dict[str, str | bool | int]]:
    """按固定展示顺序返回前端平台目录。"""

    return [item.public_dict() for item in PLATFORM_CATALOG]
