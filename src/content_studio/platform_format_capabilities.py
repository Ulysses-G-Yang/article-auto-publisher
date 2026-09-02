"""Content Studio 的平台格式能力声明。

能力声明是投递安全边界的一部分：只有真实页面和适配器证据才能晋级 v2
富文档能力。当前小黑盒已证明正文 H2/H3；微博及其余已启用平台仅声明
真实重开证据覆盖的 H2 和图片顺序。其他能力仍保持 fail-closed。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from content_studio.content_document import FEATURE_KEYS

DELIVERY_PLATFORMS = (
    "xiaoheihe",
    "zol",
    "zhihu",
    "weibo",
    "smzdm",
    "toutiao",
    "baijiahao",
)


@dataclass(frozen=True)
class PlatformFormatDeclaration:
    """一个平台的不可变格式能力声明。"""

    platform: str
    supported: frozenset[str]
    heading_levels: frozenset[int] = frozenset()


class PlatformFormatCapabilities:
    """可注入的平台格式能力表。

    默认表显式包含已启用平台，只声明真实草稿保存后重开已经证明的
    ``image_order`` 与标题层级；测试或未来真实验收可以通过构造函数注入
    已证明的其他能力，构造完成后表不可变。
    """

    def __init__(
        self,
        declarations: Mapping[
            str,
            Iterable[str] | PlatformFormatDeclaration,
        ]
        | None = None,
    ) -> None:
        raw: dict[str, Iterable[str] | PlatformFormatDeclaration] = {
            platform: () for platform in DELIVERY_PLATFORMS
        }
        for platform, supported in (declarations or {}).items():
            if platform not in DELIVERY_PLATFORMS:
                raise ValueError(f"未知投递平台格式声明: {platform}")
            raw[platform] = supported

        normalized: dict[str, PlatformFormatDeclaration] = {}
        for platform in DELIVERY_PLATFORMS:
            value = raw[platform]
            if isinstance(value, PlatformFormatDeclaration):
                if value.platform != platform:
                    raise ValueError("格式声明 platform 与映射键不一致")
                supported = value.supported
                heading_levels = value.heading_levels
            else:
                supported = frozenset(value)
                heading_levels = frozenset()
            unknown = supported - FEATURE_KEYS
            if unknown:
                raise ValueError(
                    f"平台 {platform} 包含未知格式能力: {', '.join(sorted(unknown))}"
                )
            if any(
                isinstance(level, bool)
                or not isinstance(level, int)
                or level not in range(1, 7)
                for level in heading_levels
            ):
                raise ValueError(f"平台 {platform} 包含无效 heading level")
            if heading_levels and "heading" not in supported:
                raise ValueError("heading_levels 只能随 heading 能力声明")
            normalized[platform] = PlatformFormatDeclaration(
                platform,
                supported,
                frozenset(heading_levels),
            )
        self._declarations = MappingProxyType(normalized)

    def get(self, platform: str) -> PlatformFormatDeclaration | None:
        """返回声明；未声明平台返回 ``None``，调用方必须拒绝自动投递。"""

        return self._declarations.get(platform)

    def __contains__(self, platform: str) -> bool:
        return platform in self._declarations

    @property
    def declarations(self) -> Mapping[str, PlatformFormatDeclaration]:
        return self._declarations


# 2026-08-19：小黑盒、ZOL、知乎、什么值得买与百家号均已用同一份 v2 Word
# 内容完成交错插图、标题映射和保存后重开 DOM 核验。ZOL 的独立重开证据
# 为 22 个文字/章节块、7 张图片与 29 个有序 DOM token 完全一致。未证明的
# heading level 仍保持关闭。2026-08-21 微博又通过标准 Content Studio 流程
# 完成 22 个文本段落、7 张语义正文图、5 个 H2 和唯一云端草稿重开核验。
# 小红书曾通过同一 Profile 的本地卡片重开，
# 但 2026-08-21 跨浏览器复核证明该卡片不是云端草稿，已退出投递注册表。
DEFAULT_PLATFORM_FORMAT_CAPABILITIES = PlatformFormatCapabilities(
    {
        "xiaoheihe": PlatformFormatDeclaration(
            "xiaoheihe",
            frozenset({"heading", "image_order"}),
            frozenset({2, 3}),
        ),
        "zol": PlatformFormatDeclaration(
            "zol",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "zhihu": PlatformFormatDeclaration(
            "zhihu",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "weibo": PlatformFormatDeclaration(
            "weibo",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "smzdm": PlatformFormatDeclaration(
            "smzdm",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "toutiao": PlatformFormatDeclaration(
            "toutiao",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "baijiahao": PlatformFormatDeclaration(
            "baijiahao",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
    }
)

# 便于依赖注入和调用方按语义检索的别名。
PlatformFormatCapabilityRegistry = PlatformFormatCapabilities


__all__ = [
    "DEFAULT_PLATFORM_FORMAT_CAPABILITIES",
    "DELIVERY_PLATFORMS",
    "PlatformFormatCapabilities",
    "PlatformFormatCapabilityRegistry",
    "PlatformFormatDeclaration",
]
