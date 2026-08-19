"""Content Studio 的平台格式能力声明。

能力声明是投递安全边界的一部分：只有真实页面和适配器证据才能晋级 v2
富文档能力。当前小黑盒已通过真实输入规则证明正文 H2/H3；其他标题层级、
列表、表格等能力仍未声明。未知平台没有声明时必须保持 fail-closed。
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
    "smzdm",
    "baijiahao",
    "xiaohongshu",
)


@dataclass(frozen=True)
class PlatformFormatDeclaration:
    """一个平台的不可变格式能力声明。"""

    platform: str
    supported: frozenset[str]
    heading_levels: frozenset[int] = frozenset()


class PlatformFormatCapabilities:
    """可注入的平台格式能力表。

    默认表显式包含六个平台。当前只声明小黑盒已经真实观察到的
    ``image_order`` 以及通过真实输入规则和 DOM 回读证明的 H2/H3；测试或
    未来真实验收可以通过构造函数注入已证明的其他能力，构造完成后表不可变。
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


# 2026-08-19：小黑盒、知乎、什么值得买与百家号均已用同一份 v2 Word
# 内容完成交错插图、标题映射和保存后重开 DOM 核验。未真实证明的
# heading level 仍保持关闭。2026-08-20 小红书已通过单图 text-image-text
# 草稿重开验收；随后同一份 29-token Word 又完成 5 个 H2、7 张图片的
# 保存后重开核验，因此开放 H2 与 image_order，其他标题层级仍关闭。
DEFAULT_PLATFORM_FORMAT_CAPABILITIES = PlatformFormatCapabilities(
    {
        "xiaoheihe": PlatformFormatDeclaration(
            "xiaoheihe",
            frozenset({"heading", "image_order"}),
            frozenset({2, 3}),
        ),
        "zhihu": PlatformFormatDeclaration(
            "zhihu",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "smzdm": PlatformFormatDeclaration(
            "smzdm",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "baijiahao": PlatformFormatDeclaration(
            "baijiahao",
            frozenset({"heading", "image_order"}),
            frozenset({2}),
        ),
        "xiaohongshu": PlatformFormatDeclaration(
            "xiaohongshu",
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
