"""六个平台真实投递就绪度的只读证据清单。

本模块只描述已发生的验收事实，不参与账号选择、投递编排或平台适配器执行。
尤其要区分“代码存在”“单元测试通过”和“真实平台验收通过”：默认清单只把
交接文档明确确认的结果标记为 ``REAL_*``，其余保持 ``RETEST_REQUIRED`` 或
``REAL_FAILED``，避免前端或未来 MCP 把未重跑的链路误当成可用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from account_sessions.platform_catalog import DELIVERY_ENABLED_PLATFORMS, PLATFORM_CATALOG
from content_studio.platform_format_capabilities import DELIVERY_PLATFORMS


class ReadinessStatus(str, Enum):
    """平台能力的证据状态。"""

    UNVERIFIED = "UNVERIFIED"
    CODE_TESTED = "CODE_TESTED"
    REAL_VERIFIED = "REAL_VERIFIED"
    REAL_FAILED = "REAL_FAILED"
    RETEST_REQUIRED = "RETEST_REQUIRED"
    DISABLED = "DISABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


FACET_NAMES: tuple[str, ...] = (
    "account_session",
    "editor_entry",
    "text_draft",
    "body_images",
    "cover",
    "draft_verification",
    "public_publish",
)

PLATFORM_DISPLAY_NAMES: Mapping[str, str] = MappingProxyType(
    {
        item.id: item.display_name
        for item in PLATFORM_CATALOG
        if item.delivery_enabled
    }
)

_HANDOFF_DATE = date(2026, 8, 17)
_HANDOFF_EVIDENCE = ("codex_handoff_20260817.md",)
_DRAFTBOX_EVIDENCE = ("codex_handoff_20260817.md", "docs/PLATFORM_DRAFTBOX_AUDIT.md")
_XHH_WORD_DATE = date(2026, 8, 19)
_XHH_WORD_EVIDENCE = (
    "docs/acceptance/XIAOHEIHE_WORD_DRAFT_20260819.md",
    "codex_handoff_20260817.md",
)
_ZOL_WORD_DATE = date(2026, 8, 19)
_ZOL_WORD_EVIDENCE = (
    "docs/acceptance/ZOL_WORD_DRAFT_20260819.md",
    "codex_handoff_20260817.md",
)
_ZHIHU_WORD_DATE = date(2026, 8, 19)
_ZHIHU_WORD_EVIDENCE = (
    "docs/acceptance/ZHIHU_WORD_DRAFT_20260819.md",
    "codex_handoff_20260817.md",
)
_SMZDM_WORD_DATE = date(2026, 8, 19)
_SMZDM_WORD_EVIDENCE = (
    "docs/acceptance/SMZDM_WORD_DRAFT_20260819.md",
    "codex_handoff_20260817.md",
)
_BAIJIAHAO_WORD_DATE = date(2026, 8, 19)
_BAIJIAHAO_WORD_EVIDENCE = (
    "docs/acceptance/BAIJIAHAO_WORD_DRAFT_20260819.md",
    "codex_handoff_20260817.md",
)
_XHS_EVIDENCE = (
    "docs/acceptance/XIAOHONGSHU_SINGLE_IMAGE_DRAFT_20260820.md",
    "codex_handoff_20260817.md",
    "docs/XIAOHONGSHU_RISK_CONTROL.md",
)
_PUBLIC_GATE_EVIDENCE = ("codex_handoff_20260817.md", "AGENTS.md")


@dataclass(frozen=True, slots=True)
class FacetReadiness:
    """一个平台能力项的证据与验收门。"""

    status: ReadinessStatus
    page_state: str
    last_real_check: date | None
    evidence_refs: tuple[str, ...]
    success_criteria: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "page_state": self.page_state,
            "last_real_check": (
                self.last_real_check.isoformat() if self.last_real_check else None
            ),
            "evidence_refs": list(self.evidence_refs),
            "success_criteria": self.success_criteria,
        }


@dataclass(frozen=True, slots=True)
class PlatformDeliveryReadiness:
    """一个投递平台的七项就绪度记录。"""

    platform: str
    display_name: str
    facets: Mapping[str, FacetReadiness]

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "facets": {
                name: self.facets[name].as_dict() for name in FACET_NAMES
            },
        }


_SUCCESS_CRITERIA: Mapping[str, str] = MappingProxyType(
    {
        "account_session": (
            "隔离账号在真实页面完成登录态检查，账号为 ACTIVE/VALID，且没有残留 Profile 锁。"
        ),
        "editor_entry": (
            "使用指定账号进入目标编辑器，页面状态和编辑器入口均与平台契约一致。"
        ),
        "text_draft": (
            "标题、正文和段落顺序完整写入，并在平台侧成功保存为 DRAFT。"
        ),
        "body_images": (
            "正文图片按冻结版本顺序出现，图片数量在页面稳定后与输入数量一致。"
        ),
        "cover": (
            "封面策略对应的图片在平台预览中真实出现，且不误传正文或其他控件。"
        ),
        "draft_verification": (
            "平台草稿箱标题/计数和正文媒体状态可追溯核对，不能只凭接口返回码判定。"
        ),
        "public_publish": (
            "只有用户逐目标确认、一次性令牌和公开发布总开关同时满足时才允许公开发布。"
        ),
    }
)


def _evidence_for(platform: str, facet: str) -> tuple[str, ...]:
    if facet == "public_publish":
        return _PUBLIC_GATE_EVIDENCE
    if platform == "xiaohongshu":
        return _XHS_EVIDENCE
    if facet == "draft_verification":
        return _DRAFTBOX_EVIDENCE
    return _HANDOFF_EVIDENCE


def _facet(
    platform: str,
    facet: str,
    status: ReadinessStatus,
    *,
    page_state: str,
    last_real_check: date | None = _HANDOFF_DATE,
    evidence_refs: tuple[str, ...] | None = None,
) -> FacetReadiness:
    return FacetReadiness(
        status=status,
        page_state=page_state,
        last_real_check=last_real_check,
        evidence_refs=evidence_refs or _evidence_for(platform, facet),
        success_criteria=_SUCCESS_CRITERIA[facet],
    )


def _build_platform(
    platform: str,
    *,
    real_verified: frozenset[str] = frozenset(),
    real_failed: frozenset[str] = frozenset(),
    no_prior_run: frozenset[str] = frozenset(),
    facet_overrides: Mapping[
        str, tuple[ReadinessStatus, str, date | None, tuple[str, ...]]
    ] = MappingProxyType({}),
) -> PlatformDeliveryReadiness:
    facets: dict[str, FacetReadiness] = {}
    for facet in FACET_NAMES:
        if facet == "public_publish":
            facets[facet] = _facet(
                platform,
                facet,
                ReadinessStatus.DISABLED,
                page_state="global_publish_gate_closed",
                last_real_check=None,
            )
        elif facet in facet_overrides:
            status, page_state, last_real_check, evidence_refs = facet_overrides[facet]
            facets[facet] = _facet(
                platform,
                facet,
                status,
                page_state=page_state,
                last_real_check=last_real_check,
                evidence_refs=evidence_refs,
            )
        elif facet == "account_session":
            facets[facet] = _facet(
                platform,
                facet,
                ReadinessStatus.REAL_VERIFIED,
                page_state="account_active_valid",
            )
        elif facet in real_verified:
            facets[facet] = _facet(
                platform,
                facet,
                ReadinessStatus.REAL_VERIFIED,
                page_state="real_acceptance_passed",
            )
        elif facet in real_failed:
            facets[facet] = _facet(
                platform,
                facet,
                ReadinessStatus.REAL_FAILED,
                page_state="real_acceptance_failed",
            )
        else:
            facets[facet] = _facet(
                platform,
                facet,
                ReadinessStatus.RETEST_REQUIRED,
                page_state=(
                    "no_prior_real_run"
                    if facet in no_prior_run
                    else "code_fixed_waiting_real_rerun"
                ),
                last_real_check=None if facet in no_prior_run else _HANDOFF_DATE,
            )
    return PlatformDeliveryReadiness(
        platform=platform,
        display_name=PLATFORM_DISPLAY_NAMES[platform],
        facets=MappingProxyType(facets),
    )


PLATFORM_DELIVERY_READINESS: tuple[PlatformDeliveryReadiness, ...] = (
    _build_platform(
        "xiaoheihe",
        facet_overrides={
            "account_session": (
                ReadinessStatus.REAL_VERIFIED,
                "account_active_valid_during_word_draft_acceptance",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_editor_entry_passed",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_22_text_and_heading_blocks",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_7_ordered_body_images",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
            "cover": (
                ReadinessStatus.RETEST_REQUIRED,
                "real_run_stopped_before_cover_verification_waiting_rerun",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "unique_draft_card_and_persisted_dom_verified",
                _XHH_WORD_DATE,
                _XHH_WORD_EVIDENCE,
            ),
        },
    ),
    _build_platform(
        "zol",
        facet_overrides={
            "account_session": (
                ReadinessStatus.REAL_VERIFIED,
                "account_active_valid_during_word_draft_acceptance",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_creator_editor_entry_passed",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_22_text_and_heading_blocks",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_7_ordered_body_images",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
            "cover": (
                ReadinessStatus.RETEST_REQUIRED,
                "cover_control_not_independently_verified",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "unique_title_and_29_ordered_tokens_reopened",
                _ZOL_WORD_DATE,
                _ZOL_WORD_EVIDENCE,
            ),
        },
    ),
    _build_platform(
        "zhihu",
        facet_overrides={
            "account_session": (
                ReadinessStatus.REAL_VERIFIED,
                "account_active_valid_during_word_draft_acceptance",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_draftjs_editor_entry_passed",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_22_text_and_heading_blocks",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_7_ordered_body_images",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
            "cover": (
                ReadinessStatus.RETEST_REQUIRED,
                "cover_control_not_independently_verified",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "unique_api_draft_id_and_29_ordered_tokens_reopened",
                _ZHIHU_WORD_DATE,
                _ZHIHU_WORD_EVIDENCE,
            ),
        },
    ),
    _build_platform(
        "smzdm",
        facet_overrides={
            "account_session": (
                ReadinessStatus.REAL_VERIFIED,
                "account_active_valid_during_word_draft_acceptance",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_prosemirror_editor_entry_passed",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_22_text_and_heading_blocks",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_7_ordered_body_images",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
            "cover": (
                ReadinessStatus.RETEST_REQUIRED,
                "cover_control_not_independently_verified",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "unique_title_and_29_ordered_tokens_reopened",
                _SMZDM_WORD_DATE,
                _SMZDM_WORD_EVIDENCE,
            ),
        },
    ),
    _build_platform(
        "baijiahao",
        facet_overrides={
            "account_session": (
                ReadinessStatus.REAL_VERIFIED,
                "account_active_valid_during_word_draft_acceptance",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_ueditor_entry_passed",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_22_text_and_heading_blocks",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_7_ordered_body_images",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
            "cover": (
                ReadinessStatus.REAL_FAILED,
                "cover_control_not_independently_verified",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "persisted_draft_and_29_ordered_tokens_reopened",
                _BAIJIAHAO_WORD_DATE,
                _BAIJIAHAO_WORD_EVIDENCE,
            ),
        },
    ),
    _build_platform(
        "xiaohongshu",
        facet_overrides={
            "editor_entry": (
                ReadinessStatus.REAL_VERIFIED,
                "real_longform_tiptap_entry_passed",
                date(2026, 8, 20),
                _XHS_EVIDENCE,
            ),
            "text_draft": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_text_before_and_after_image",
                date(2026, 8, 20),
                _XHS_EVIDENCE,
            ),
            "body_images": (
                ReadinessStatus.REAL_VERIFIED,
                "real_reopen_verified_single_body_image_in_order",
                date(2026, 8, 20),
                _XHS_EVIDENCE,
            ),
            "draft_verification": (
                ReadinessStatus.REAL_VERIFIED,
                "unique_title_and_text_image_text_reopened",
                date(2026, 8, 20),
                _XHS_EVIDENCE,
            ),
        },
        no_prior_run=frozenset({"cover"}),
    ),
)


def readiness_by_platform() -> Mapping[str, PlatformDeliveryReadiness]:
    """返回只读的平台就绪度映射。"""

    return MappingProxyType(
        {record.platform: record for record in PLATFORM_DELIVERY_READINESS}
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validate_evidence_ref(reference: str) -> None:
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"证据引用必须是仓库相对路径: {reference}")
    if not (_repo_root() / path).is_file():
        raise ValueError(f"证据文件不存在: {reference}")


def validate_readiness_matrix(
    records: tuple[PlatformDeliveryReadiness, ...] = PLATFORM_DELIVERY_READINESS,
) -> None:
    """校验证据清单、平台目录和公开发布门的一致性。"""

    platforms = tuple(record.platform for record in records)
    if tuple(DELIVERY_ENABLED_PLATFORMS) != tuple(DELIVERY_PLATFORMS):
        raise ValueError("platform_catalog 与格式能力 registry 的平台集合不一致")
    if platforms != tuple(DELIVERY_ENABLED_PLATFORMS):
        raise ValueError(
            "平台就绪清单必须与 platform_catalog.delivery_enabled 完全一致"
        )
    for record in records:
        if record.display_name != PLATFORM_DISPLAY_NAMES.get(record.platform):
            raise ValueError(f"平台显示名不一致: {record.platform}")
        if tuple(record.facets) != FACET_NAMES:
            raise ValueError(f"平台能力项不完整: {record.platform}")
        for facet_name in FACET_NAMES:
            facet = record.facets[facet_name]
            if not facet.page_state or not facet.success_criteria:
                raise ValueError(f"平台能力项缺少验收信息: {record.platform}/{facet_name}")
            if not facet.evidence_refs:
                raise ValueError(f"平台能力项缺少证据引用: {record.platform}/{facet_name}")
            for reference in facet.evidence_refs:
                _validate_evidence_ref(reference)
            if facet.status in {
                ReadinessStatus.REAL_VERIFIED,
                ReadinessStatus.REAL_FAILED,
            } and facet.last_real_check is None:
                raise ValueError(
                    f"REAL 状态必须有日期: {record.platform}/{facet_name}"
                )
            if facet_name == "public_publish" and facet.status != ReadinessStatus.DISABLED:
                raise ValueError(f"公开发布必须保持 DISABLED: {record.platform}")


def can_run_stable_image_draft(platform: str) -> bool:
    """计算平台是否具备稳定带图草稿证据；不接入生产执行路径。"""

    record = readiness_by_platform().get(platform)
    if record is None:
        return False
    required = (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    )
    return all(record.facets[name].status == ReadinessStatus.REAL_VERIFIED for name in required)


def can_run_complete_word_draft(platform: str) -> bool:
    """计算完整 Word 一键草稿是否具备封面闭环证据；不接入生产执行路径。"""

    record = readiness_by_platform().get(platform)
    if record is None:
        return False
    required = (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    )
    if not all(record.facets[name].status == ReadinessStatus.REAL_VERIFIED for name in required):
        return False
    return record.facets["cover"].status in {
        ReadinessStatus.REAL_VERIFIED,
        ReadinessStatus.NOT_APPLICABLE,
    }


__all__ = [
    "FACET_NAMES",
    "PLATFORM_DELIVERY_READINESS",
    "PLATFORM_DISPLAY_NAMES",
    "FacetReadiness",
    "PlatformDeliveryReadiness",
    "ReadinessStatus",
    "can_run_stable_image_draft",
    "can_run_complete_word_draft",
    "readiness_by_platform",
    "validate_readiness_matrix",
]
