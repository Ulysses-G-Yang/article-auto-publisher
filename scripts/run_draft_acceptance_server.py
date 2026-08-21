"""启动一次性、仅保存草稿的真实验收 Flask 入口。

这个入口只用于在明确授权后验证指定平台的 DRAFT 链路。它会在当前进程内
注入本次验收选择的平台格式能力，不修改默认能力注册表、数据库能力状态或
平台适配器。它不能用于生产环境，也不能用于公开发布。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT, REPO_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

CONFIRMATION_WORD = "DRAFT_ONLY"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_SAFETY_SWITCHES = (
    "PUBLISH_AFTER_DRAFT",
    "ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH",
    "LEGACY_UPLOAD_QUEUE_ENABLED",
)
_ACCEPTANCE_ONLY_PLATFORMS = ("weibo",)


class _AcceptanceCapabilityOverlay:
    """给一次性验收进程追加尚未进入生产目录的平台能力。

    生产 ``PlatformFormatCapabilities`` 会拒绝未晋级的平台，这是正确的
    fail-closed 行为。微博真实草稿验收需要先经过一次受控试运行，因此这里只
    在当前 Flask 进程覆盖 ``get``，不修改默认注册表或生产平台目录。
    """

    def __init__(self, base: Any, overrides: Mapping[str, Any]) -> None:
        self._base = base
        self._overrides = dict(overrides)

    def get(self, platform: str) -> Any | None:
        return self._overrides.get(platform) or self._base.get(platform)

    def __contains__(self, platform: str) -> bool:
        return platform in self._overrides or platform in self._base

    @property
    def declarations(self) -> Mapping[str, Any]:
        return {**self._base.declarations, **self._overrides}


def _delivery_platforms() -> tuple[str, ...]:
    from content_studio.platform_format_capabilities import DELIVERY_PLATFORMS

    return tuple(dict.fromkeys((*DELIVERY_PLATFORMS, *_ACCEPTANCE_ONLY_PLATFORMS)))


def _normalize_platforms(platforms: Iterable[str]) -> tuple[str, ...]:
    if isinstance(platforms, (str, bytes, bytearray)):
        raise ValueError("--platform 必须重复传入一个或多个平台名称")

    normalized = tuple(str(platform).strip() for platform in platforms)
    if not normalized:
        raise ValueError("至少需要指定一个验收平台")
    if any(not platform for platform in normalized):
        raise ValueError("验收平台名称不能为空")
    if len(set(normalized)) != len(normalized):
        raise ValueError("验收平台不能重复指定")

    unknown = sorted(set(normalized) - set(_delivery_platforms()))
    if unknown:
        raise ValueError(f"验收平台不在投递目录中: {', '.join(unknown)}")
    return normalized


def _read_disabled_switch(name: str) -> bool:
    """读取必须关闭的开关；未设置或明确 false 才允许继续。"""

    if name not in os.environ:
        return False
    raw = os.environ[name].strip().lower()
    if raw in _TRUE_VALUES:
        raise RuntimeError(f"{name} 必须保持关闭")
    if raw in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} 必须未设置或明确为 false")


def _assert_draft_only_settings() -> None:
    for name in _SAFETY_SWITCHES:
        _read_disabled_switch(name)

    # 配置解析也必须发生在 create_app 之前；错误配置不能触发应用初始化。
    config = _load_config()
    app_config = config.get("app", {})
    if app_config.get("publish_after_draft"):
        raise RuntimeError("配置中的 publish_after_draft 必须保持关闭")
    if app_config.get("legacy_upload_queue_enabled"):
        raise RuntimeError("配置中的 legacy_upload_queue_enabled 必须保持关闭")


def _load_config() -> dict:
    """在开关环境门通过后才加载项目配置。"""

    from config import get_config

    return get_config()


def _load_create_app():
    """在所有安全门通过后才加载项目 Flask 工厂。"""

    from app import create_app

    return create_app()


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return port


def build_acceptance_app(
    platforms: Iterable[str],
    confirmation: str,
    *,
    xhs_resume_title: str = "",
):
    """构造仅对指定平台开放格式能力的验收应用。

    ``confirmation`` 必须是固定的 ``DRAFT_ONLY``；调用方仍需在正式工作台
    中选择具体账号和内容。能力声明只替换当前 Flask 进程内的 Content Studio
    service，不会改变 ``DEFAULT_PLATFORM_FORMAT_CAPABILITIES``。
    """

    if confirmation != CONFIRMATION_WORD:
        raise ValueError("必须使用固定确认词 DRAFT_ONLY")
    selected = _normalize_platforms(platforms)
    normalized_resume_title = " ".join(str(xhs_resume_title or "").split())
    if normalized_resume_title:
        raise ValueError(
            "XHS_CLOUD_DRAFT_UNAVAILABLE: 小红书本地 Profile 草稿不能用于云端验收"
        )
    _assert_draft_only_settings()

    # 所有验收门通过后才导入并创建 Flask 应用，失败路径不会初始化路由、
    # 数据库扩展或账号运行时。
    from content_studio.content_document import FEATURE_KEYS
    from content_studio.platform_format_capabilities import (
        PlatformFormatCapabilities,
        PlatformFormatDeclaration,
    )

    app = _load_create_app()
    content_state = app.extensions.get("content_studio")
    if content_state is None or not hasattr(content_state, "service"):
        raise RuntimeError("Content Studio 运行时未注册")
    declarations = {}
    acceptance_only_declarations = {}
    for platform in selected:
        if platform == "zol":
            declarations[platform] = PlatformFormatDeclaration(
                platform,
                FEATURE_KEYS,
                frozenset({2}),
            )
        elif platform == "xiaoheihe":
            # 真实编辑器只读/不可保存探测已证明：`# ` 生成 H2，`## `
            # 生成 H3。验收入口必须保留该层级白名单，不能把已验证能力降为
            # 空集合并在计划创建阶段误拦截同一 Word。
            declarations[platform] = PlatformFormatDeclaration(
                platform,
                FEATURE_KEYS,
                frozenset({2, 3}),
            )
        elif platform == "zhihu":
            # 真实 Draft.js 编辑器探测已证明：Word H2 通过“标题 →
            # 二级标题”落为正文 h3；验收进程只临时放行已证明的 H2。
            declarations[platform] = PlatformFormatDeclaration(
                platform,
                FEATURE_KEYS,
                frozenset({2}),
            )
        elif platform == "smzdm":
            # 真实 TipTap 编辑器探测已证明：“二级标题”持久化为 h3。
            # 验收进程只临时放行 Word H2 与原始图片顺序；生产默认能力
            # 必须等待完整 Word 草稿保存后重开证据。
            declarations[platform] = PlatformFormatDeclaration(
                platform,
                frozenset({"heading", "image_order"}),
                frozenset({2}),
            )
        elif platform == "baijiahao":
            # 只对本次 DRAFT 验收进程临时放行真实探测到的 UEditor
            # “标题”样式与正文图片顺序；生产能力仍需保存后重开证据晋级。
            declarations[platform] = PlatformFormatDeclaration(
                platform,
                frozenset({"heading", "image_order"}),
                frozenset({2}),
            )
        elif platform == "weibo":
            # 真实只读探测已证明微博长文编辑器存在稳定 H2 菜单与正文图片
            # 面板。本声明只用于本次 DRAFT-only 验收；草稿列表证据通过前，
            # 生产平台目录继续保持 delivery_enabled=False。
            acceptance_only_declarations[platform] = PlatformFormatDeclaration(
                platform,
                frozenset({"heading", "image_order"}),
                frozenset({2}),
            )
        else:
            declarations[platform] = FEATURE_KEYS
    registry = PlatformFormatCapabilities(declarations)
    if acceptance_only_declarations:
        registry = _AcceptanceCapabilityOverlay(
            registry,
            acceptance_only_declarations,
        )
    content_state.service.platform_format_capabilities = registry

    # 验收进程继续显式注入 ZOL 已验证标题路径。小红书本地 Profile 草稿
    # 已从投递目录移除，不能再通过验收参数绕开云端实体要求。
    if "zol" in selected:
        account_state = app.extensions.get("account_sessions")
        if account_state is None or not hasattr(account_state, "accounts"):
            raise RuntimeError("账号会话运行时未注册")
        original_factory = account_state.accounts.platform_factory

        def acceptance_platform_factory(account):
            if getattr(account, "platform", "") == "zol":
                from platforms.zol import ZOLPlatform

                return ZOLPlatform(
                    profile_dir=account.profile_path,
                    strict_profile_lock=True,
                    enable_heading_experiment=True,
                )
            return original_factory(account)

        account_state.accounts.platform_factory = acceptance_platform_factory
        if hasattr(account_state, "delivery"):
            account_state.delivery.platform_factory = acceptance_platform_factory
    return app


def run_acceptance_server(app: Any, port: int) -> None:
    """以固定本地地址运行验收应用，不启用调试器或自动重载。"""

    print(
        "DRAFT_ACCEPTANCE_SERVER_READY "
        f"platforms={','.join(app.config.get('DRAFT_ACCEPTANCE_PLATFORMS', ())) or 'selected'} "
        f"host=127.0.0.1 port={port}"
    )
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="一次性 ArticleOps DRAFT 真实验收入口（禁止 PUBLISH）"
    )
    parser.add_argument(
        "--platform",
        action="append",
        required=True,
        choices=_delivery_platforms(),
        help="要在本进程内临时放行格式能力的平台；可重复传入",
    )
    parser.add_argument(
        "--confirmation",
        required=True,
        help="固定确认词：DRAFT_ONLY",
    )
    parser.add_argument("--port", type=_port, default=5000)
    parser.add_argument(
        "--xhs-resume-title",
        default="",
        help="已停用：小红书网页长文只有 Profile 本地草稿，传入即拒绝",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    app = build_acceptance_app(
        args.platform,
        args.confirmation,
        xhs_resume_title=args.xhs_resume_title,
    )
    app.config["DRAFT_ACCEPTANCE_PLATFORMS"] = tuple(args.platform)
    run_acceptance_server(app, args.port)


if __name__ == "__main__":
    main()
