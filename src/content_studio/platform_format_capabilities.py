"""Content Studio 的平台格式能力声明。

能力声明是投递安全边界的一部分：只有真实页面和适配器证据才能晋级 v2
富文档能力。当前小黑盒仅有正文图片顺序证据；heading、列表、表格等能力
仍未声明。未知平台没有声明时必须保持 fail-closed。
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


class PlatformFormatCapabilities:
    """可注入的平台格式能力表。

    默认表显式包含六个平台。当前只声明小黑盒已经真实观察到的
    ``image_order``；测试或未来真实验收可以通过构造函数注入已证明的其他
    能力，构造完成后表不可变。
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
            else:
                supported = frozenset(value)
            unknown = supported - FEATURE_KEYS
            if unknown:
                raise ValueError(
                    f"平台 {platform} 包含未知格式能力: {', '.join(sorted(unknown))}"
                )
            normalized[platform] = PlatformFormatDeclaration(platform, supported)
        self._declarations = MappingProxyType(normalized)

    def get(self, platform: str) -> PlatformFormatDeclaration | None:
        """返回声明；未声明平台返回 ``None``，调用方必须拒绝自动投递。"""

        return self._declarations.get(platform)

    def __contains__(self, platform: str) -> bool:
        return platform in self._declarations

    @property
    def declarations(self) -> Mapping[str, PlatformFormatDeclaration]:
        return self._declarations


# 2026-08-19：小黑盒已有交错插图和稳定图片数量增长的真实证据。
# 没有 H2/heading DOM 证据，因此绝不把 ``heading`` 放入默认声明。
DEFAULT_PLATFORM_FORMAT_CAPABILITIES = PlatformFormatCapabilities(
    {"xiaoheihe": {"image_order"}}
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
