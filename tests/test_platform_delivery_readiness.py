from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 路径引导必须先于项目包导入。
# ruff: noqa: E402, I001

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from account_sessions.platform_catalog import DELIVERY_ENABLED_PLATFORMS, PLATFORM_CATALOG
from content_studio.platform_format_capabilities import DELIVERY_PLATFORMS
from content_studio.platform_delivery_readiness import (
    FACET_NAMES,
    PLATFORM_DELIVERY_READINESS,
    ReadinessStatus,
    can_run_complete_word_draft,
    can_run_stable_image_draft,
    readiness_by_platform,
    validate_readiness_matrix,
)


EXPECTED_PLATFORMS = (
    "xiaoheihe",
    "zol",
    "zhihu",
    "smzdm",
    "baijiahao",
    "xiaohongshu",
)


def test_readiness_matrix_matches_delivery_catalog_and_has_seven_facets() -> None:
    assert EXPECTED_PLATFORMS == DELIVERY_ENABLED_PLATFORMS
    assert EXPECTED_PLATFORMS == DELIVERY_PLATFORMS
    assert tuple(record.platform for record in PLATFORM_DELIVERY_READINESS) == (
        EXPECTED_PLATFORMS
    )
    for record in PLATFORM_DELIVERY_READINESS:
        assert tuple(record.facets) == FACET_NAMES
        assert all(record.facets[name].success_criteria for name in FACET_NAMES)
    expected_names = {
        item.id: item.display_name for item in PLATFORM_CATALOG if item.delivery_enabled
    }
    assert {
        record.platform: record.display_name for record in PLATFORM_DELIVERY_READINESS
    } == expected_names


def test_readiness_status_enum_contains_required_evidence_states() -> None:
    assert {status.value for status in ReadinessStatus} >= {
        "UNVERIFIED",
        "CODE_TESTED",
        "REAL_VERIFIED",
        "REAL_FAILED",
        "RETEST_REQUIRED",
        "DISABLED",
        "NOT_APPLICABLE",
    }


def test_matrix_validation_checks_existing_relative_evidence_and_public_gate() -> None:
    validate_readiness_matrix()
    root = PROJECT_ROOT
    for record in PLATFORM_DELIVERY_READINESS:
        public_publish = record.facets["public_publish"]
        assert public_publish.status is ReadinessStatus.DISABLED
        for facet in record.facets.values():
            assert facet.evidence_refs
            for reference in facet.evidence_refs:
                path = Path(reference)
                assert not path.is_absolute()
                assert ".." not in path.parts
                assert (root / path).is_file(), reference
                if facet.status in {
                    ReadinessStatus.REAL_VERIFIED,
                    ReadinessStatus.REAL_FAILED,
                }:
                    assert facet.last_real_check is not None


def test_platform_facts_follow_current_real_acceptance_evidence() -> None:
    by_platform = readiness_by_platform()

    assert by_platform["zhihu"].facets["body_images"].status is ReadinessStatus.REAL_VERIFIED
    assert by_platform["xiaohongshu"].facets["text_draft"].status is ReadinessStatus.REAL_VERIFIED
    assert by_platform["xiaohongshu"].facets["body_images"].status is ReadinessStatus.REAL_VERIFIED
    assert by_platform["baijiahao"].facets["cover"].status is ReadinessStatus.REAL_FAILED


def test_xiaohongshu_word_draft_is_promoted_after_exact_reopen_evidence() -> None:
    record = readiness_by_platform()["xiaohongshu"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-20"
        assert "XIAOHONGSHU_WORD_DRAFT_20260820.md" in " ".join(
            facet.evidence_refs
        )

    assert can_run_stable_image_draft("xiaohongshu")
    assert not can_run_complete_word_draft("xiaohongshu")

def test_zol_word_draft_is_promoted_only_after_persisted_reopen_evidence() -> None:
    record = readiness_by_platform()["zol"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-19"
        assert "ZOL_WORD_DRAFT_20260819.md" in " ".join(facet.evidence_refs)

    assert record.facets["cover"].status is ReadinessStatus.RETEST_REQUIRED
    assert can_run_stable_image_draft("zol")
    assert not can_run_complete_word_draft("zol")


def test_xiaoheihe_word_draft_is_promoted_only_after_persisted_reopen_evidence() -> None:
    record = readiness_by_platform()["xiaoheihe"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-19"
        assert "XIAOHEIHE_WORD_DRAFT_20260819.md" in " ".join(facet.evidence_refs)

    assert record.facets["cover"].status is ReadinessStatus.RETEST_REQUIRED
    assert can_run_stable_image_draft("xiaoheihe")
    assert not can_run_complete_word_draft("xiaoheihe")


def test_zhihu_word_draft_is_promoted_only_after_persisted_reopen_evidence() -> None:
    record = readiness_by_platform()["zhihu"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-19"
        assert "ZHIHU_WORD_DRAFT_20260819.md" in " ".join(facet.evidence_refs)

    assert record.facets["cover"].status is ReadinessStatus.RETEST_REQUIRED
    assert can_run_stable_image_draft("zhihu")
    assert not can_run_complete_word_draft("zhihu")


def test_smzdm_word_draft_is_promoted_only_after_persisted_reopen_evidence() -> None:
    record = readiness_by_platform()["smzdm"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-19"
        assert "SMZDM_WORD_DRAFT_20260819.md" in " ".join(facet.evidence_refs)

    assert record.facets["cover"].status is ReadinessStatus.RETEST_REQUIRED
    assert can_run_stable_image_draft("smzdm")
    assert not can_run_complete_word_draft("smzdm")


def test_baijiahao_word_draft_is_promoted_after_persisted_reopen_evidence() -> None:
    record = readiness_by_platform()["baijiahao"]
    for facet_name in (
        "account_session",
        "editor_entry",
        "text_draft",
        "body_images",
        "draft_verification",
    ):
        facet = record.facets[facet_name]
        assert facet.status is ReadinessStatus.REAL_VERIFIED
        assert facet.last_real_check.isoformat() == "2026-08-19"
        assert "BAIJIAHAO_WORD_DRAFT_20260819.md" in " ".join(
            facet.evidence_refs
        )

    assert record.facets["cover"].status is ReadinessStatus.REAL_FAILED
    assert can_run_stable_image_draft("baijiahao")
    assert not can_run_complete_word_draft("baijiahao")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("zhihu", True),
        ("xiaoheihe", True),
        ("zol", True),
        ("smzdm", True),
        ("baijiahao", True),
        ("xiaohongshu", True),
        ("unknown", False),
    ],
)
def test_can_run_stable_image_draft_is_read_only_evidence_computation(
    platform: str,
    expected: bool,
) -> None:
    assert can_run_stable_image_draft(platform) is expected


def test_can_run_complete_word_draft_requires_cover_and_is_false_for_current_six() -> None:
    assert all(
        not can_run_complete_word_draft(platform) for platform in EXPECTED_PLATFORMS
    )
    # 知乎已通过稳定带图草稿，但封面没有真实验收，不能宣称完整 Word 闭环完成。
    assert readiness_by_platform()["zhihu"].facets["cover"].status is (
        ReadinessStatus.RETEST_REQUIRED
    )


def test_public_dict_is_safe_and_machine_readable() -> None:
    record = readiness_by_platform()["zhihu"].as_dict()
    assert set(record) == {"platform", "display_name", "facets"}
    assert set(record["facets"]) == set(FACET_NAMES)
    assert record["facets"]["body_images"]["status"] == "REAL_VERIFIED"
    serialized = repr(record)
    assert "data\\" not in serialized
    assert "Cookie" not in serialized
    assert "Token" not in serialized
