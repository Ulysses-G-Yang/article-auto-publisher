"""Content Studio 的平台格式能力声明。

能力声明是投递安全边界的一部分：六个平台虽然都在投递目录中，但在真实
验收前不预认证任何 v2 富文档能力。未知平台没有声明时必须保持 fail-closed。
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

    默认表显式包含六个平台，但 ``supported`` 全部为空。测试或未来真实
    验收可以通过构造函数注入已证明的能力；构造完成后表不可变。
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


# 未经真实平台验收，不预认证任何能力；平台条目本身仍然显式存在。
DEFAULT_PLATFORM_FORMAT_CAPABILITIES = PlatformFormatCapabilities()

# 便于依赖注入和调用方按语义检索的别名。
PlatformFormatCapabilityRegistry = PlatformFormatCapabilities


__all__ = [
    "DEFAULT_PLATFORM_FORMAT_CAPABILITIES",
    "DELIVERY_PLATFORMS",
    "PlatformFormatCapabilities",
    "PlatformFormatCapabilityRegistry",
    "PlatformFormatDeclaration",
]
