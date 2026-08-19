"""ContentDocument v2 纯模块验收。"""

from __future__ import annotations

import json

import pytest

from content_studio.content_document import (
    ContentDocumentValidationError,
    PlatformCapabilities,
    canonical_document_json,
    compute_incompatibilities,
    delivery_features,
    delivery_heading_levels,
    document_features,
    document_hash,
    project_to_delivery_blocks,
    project_to_v1,
    required_features,
    upgrade_v1,
    validate_document,
)


def mixed_document() -> dict:
    asset_one = "00000000-0000-4000-8000-000000000001"
    return {
        "schema_version": 2,
        "title": "标题",
        "title_block_id": "title-block",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "heading",
                "block_id": "title-block",
                "level": 1,
                "children": [{"kind": "text", "text": "标题", "marks": ["bold"]}],
            },
            {
                "kind": "paragraph",
                "block_id": "mixed",
                "children": [
                    {"kind": "text", "text": "图片前", "link": {"href": "https://example.test"}},
                    {
                        "kind": "image",
                        "asset_id": asset_one,
                        "alt": "一张图",
                        "caption": "图注",
                        "anchor": {"kind": "floating"},
                    },
                    {"kind": "text", "text": "图片后", "marks": ["italic"]},
                ],
            },
            {
                "kind": "list",
                "ordered": True,
                "level": 0,
                "items": [
                    {
                        "blocks": [
                            {"kind": "paragraph", "children": [{"kind": "text", "text": "项目"}]}
                        ]
                    }
                ],
            },
            {
                "kind": "table",
                "rows": [
                    {
                        "cells": [
                            {
                                "colspan": 2,
                                "blocks": [
                                    {
                                        "kind": "paragraph",
                                        "children": [{"kind": "text", "text": "单元格"}],
                                    }
                                ],
                            }
                        ]
                    }
                ],
            },
        ],
    }


def test_v2_validation_normalizes_marks_and_rejects_unknown_or_paths() -> None:
    document = mixed_document()
    normalized = validate_document(document)
    assert normalized["blocks"][0]["children"][0]["marks"] == ["bold"]
    assert "storage_path" not in json.dumps(normalized, ensure_ascii=False)

    for invalid in (
        {**document, "schema_version": 1},
        {**document, "unexpected": True},
        {**document, "storage_path": "C:/secret"},
        {
            "schema_version": 2,
            "blocks": [
                {"kind": "paragraph", "children": [{"kind": "text", "text": "x", "oops": 1}]}
            ],
        },
        {
            "schema_version": 2,
            "blocks": [
                {"kind": "paragraph", "children": [{"kind": "image", "asset_id": "C:/secret"}]}
            ],
        },
    ):
        with pytest.raises(ContentDocumentValidationError):
            validate_document(invalid)


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 2, "blocks": [{"kind": "heading", "level": 0, "children": []}]},
        {"schema_version": 2, "blocks": [{"kind": "heading", "level": 7, "children": []}]},
        {
            "schema_version": 2,
            "blocks": [{"kind": "list", "ordered": "yes", "level": 0, "items": []}],
        },
        {"schema_version": 2, "blocks": [{"kind": "image", "asset_id": "a"}]},
        {
            "schema_version": 2,
            "blocks": [
                {"kind": "paragraph", "block_id": "same", "children": []},
                {"kind": "paragraph", "block_id": "same", "children": []},
            ],
        },
    ],
)
def test_v2_invalid_structures_fail_closed(document: dict) -> None:
    with pytest.raises(ContentDocumentValidationError):
        validate_document(document)


def test_v2_limits_are_enforced() -> None:
    too_many = {"schema_version": 2, "blocks": [{"kind": "paragraph", "children": []}] * 2_001}
    with pytest.raises(ContentDocumentValidationError):
        validate_document(too_many)

    too_long = {
        "schema_version": 2,
        "blocks": [{"kind": "paragraph", "children": [{"kind": "text", "text": "x" * 200_001}]}],
    }
    with pytest.raises(ContentDocumentValidationError):
        validate_document(too_long)


def test_upgrade_v1_is_conservative_and_marks_loss_of_fidelity() -> None:
    upgraded = upgrade_v1(
        "标题",
        [
            {"type": "text", "text": "标题", "position": 0},
            {"type": "text", "text": "图片前", "position": 1},
            {
                "type": "image",
                "asset_id": "00000000-0000-4000-8000-000000000001",
                "position": 2,
            },
            {"type": "text", "text": "图片后", "position": 3},
        ],
    )
    assert upgraded["source_fidelity"] == "LEGACY_PROJECTED"
    assert upgraded["title_block_id"] == "legacy-block-000000"
    assert [block["kind"] for block in upgraded["blocks"]] == [
        "paragraph",
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert upgraded["blocks"][1]["children"][0]["text"] == "图片前"
    assert (
        upgraded["blocks"][2]["children"][0]["asset_id"] == "00000000-0000-4000-8000-000000000001"
    )

    with pytest.raises(ContentDocumentValidationError):
        upgrade_v1("标题", [{"type": "text", "text": "x", "storage_path": "C:/secret"}])


def test_upgrade_v1_stably_sorts_position_and_matches_normalized_title() -> None:
    title = "  Cafe\u0301   标题  "
    blocks = [
        {"type": "text", "text": "无位置", "position": None},
        {"type": "text", "text": "第二", "position": 2},
        {"type": "text", "text": "第一", "position": 1},
        {"type": "text", "text": "并列 A", "position": 2},
        {"type": "text", "text": "\u00a0Café 标题\u00a0", "position": 0},
        {"type": "text", "text": "仍无位置", "position": None},
    ]
    upgraded = upgrade_v1(title, blocks)
    assert upgraded["title"] == title
    assert [block["children"][0].get("text") for block in upgraded["blocks"]] == [
        "\u00a0Café 标题\u00a0",
        "第一",
        "第二",
        "并列 A",
        "无位置",
        "仍无位置",
    ]
    assert upgraded["title_block_id"] == "legacy-block-000004"

    for invalid_position in (-1, True, "2"):
        with pytest.raises(ContentDocumentValidationError):
            upgrade_v1("标题", [{"type": "text", "text": "标题", "position": invalid_position}])


def test_projection_preserves_mixed_inline_order_and_reports_precise_losses() -> None:
    projected, losses = project_to_v1(mixed_document(), omit_title_block=False)
    assert [
        (block["type"], block.get("text"), block.get("asset_id")) for block in projected[:4]
    ] == [
        ("text", "标题", None),
        ("text", "图片前", None),
        ("image", None, "00000000-0000-4000-8000-000000000001"),
        ("text", "图片后", None),
    ]
    assert {
        "heading",
        "list",
        "table",
        "link",
        "marks",
        "caption",
        "floating_anchor",
        "table_span",
    } <= losses
    assert "storage_path" not in json.dumps(projected, ensure_ascii=False)

    without_title, title_losses = project_to_v1(mixed_document(), omit_title_block=True)
    assert [block.get("text") for block in without_title if block["type"] == "text"][:2] == [
        "图片前",
        "图片后",
    ]
    assert "标题" not in [block.get("text") for block in without_title]
    assert "title_block_omitted" in title_losses


def test_hash_is_stable_but_sensitive_to_content_order_and_marks() -> None:
    document = mixed_document()
    reordered_keys = {
        "blocks": document["blocks"],
        "source_fidelity": document["source_fidelity"],
        "title_block_id": document["title_block_id"],
        "title": document["title"],
        "schema_version": document["schema_version"],
    }
    assert canonical_document_json(document) == canonical_document_json(reordered_keys)
    assert document_hash(document) == document_hash(reordered_keys)

    changed_mark = mixed_document()
    changed_mark["blocks"][0]["children"][0]["marks"] = ["italic"]
    changed_order = mixed_document()
    changed_order["blocks"][1]["children"] = list(reversed(changed_order["blocks"][1]["children"]))
    assert document_hash(document) != document_hash(changed_mark)
    assert document_hash(document) != document_hash(changed_order)


def test_capability_check_is_exact_and_fail_closed() -> None:
    document = mixed_document()
    document["blocks"][1]["children"].append(
        {"kind": "image", "asset_id": "00000000-0000-4000-8000-000000000002"}
    )
    features = document_features(document)
    assert {"heading", "list", "table", "mixed_inline", "link", "marks"} <= features
    assert "image_order" in required_features(document)
    capability = PlatformCapabilities("minimal", {"heading", "image_order"})
    missing = compute_incompatibilities(document, capability)
    assert "table" in missing
    assert "link" in missing
    assert (
        compute_incompatibilities(
            document, PlatformCapabilities("all", required_features(document))
        )
        == frozenset()
    )


def test_delivery_features_exclude_title_block_from_platform_requirements() -> None:
    document = {
        "schema_version": 2,
        "title": "标题",
        "title_block_id": "title-block",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "heading",
                "block_id": "title-block",
                "level": 1,
                "children": [{"kind": "text", "text": "标题", "marks": ["bold"]}],
            },
            {
                "kind": "heading",
                "block_id": "body-heading",
                "level": 2,
                "children": [{"kind": "text", "text": "小节"}],
            },
            {
                "kind": "paragraph",
                "block_id": "image-one",
                "children": [
                    {
                        "kind": "image",
                        "asset_id": "00000000-0000-4000-8000-000000000001",
                    }
                ],
            },
            {
                "kind": "paragraph",
                "block_id": "image-two",
                "children": [
                    {
                        "kind": "image",
                        "asset_id": "00000000-0000-4000-8000-000000000002",
                    }
                ],
            },
        ],
    }

    assert delivery_features(document) == frozenset({"heading", "image_order"})
    # 完整文档能力的历史 API 仍包含标题本身的 marks。
    assert "marks" in required_features(document)


def test_delivery_projection_preserves_heading_levels_and_inline_image_order() -> None:
    document = {
        "schema_version": 2,
        "title": "标题",
        "title_block_id": "title-block",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "heading",
                "block_id": "title-block",
                "level": 1,
                "children": [{"kind": "text", "text": "标题"}],
            },
            {
                "kind": "heading",
                "block_id": "h2",
                "level": 2,
                "children": [{"kind": "text", "text": "章节二"}],
            },
            {
                "kind": "paragraph",
                "block_id": "mixed",
                "children": [
                    {"kind": "text", "text": "图前"},
                    {
                        "kind": "image",
                        "asset_id": "00000000-0000-4000-8000-000000000001",
                        "alt": "插图",
                        "anchor": {"kind": "inline"},
                    },
                    {"kind": "text", "text": "图后"},
                ],
            },
            {
                "kind": "heading",
                "block_id": "h3",
                "level": 3,
                "children": [{"kind": "text", "text": "章节三"}],
            },
        ],
    }

    assert delivery_heading_levels(document) == frozenset({2, 3})
    assert project_to_delivery_blocks(document) == [
        {"type": "heading", "text": "章节二", "position": 0, "level": 2},
        {"type": "text", "text": "图前", "position": 1},
        {
            "type": "image",
            "asset_id": "00000000-0000-4000-8000-000000000001",
            "alt": "插图",
            "position": 2,
        },
        {"type": "text", "text": "图后", "position": 3},
        {"type": "heading", "text": "章节三", "position": 4, "level": 3},
    ]


def test_delivery_projection_accepts_matching_word_heading_style_only() -> None:
    document = {
        "schema_version": 2,
        "title": "标题",
        "title_block_id": "title-block",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "heading",
                "block_id": "title-block",
                "level": 1,
                "style_name": "Heading 1",
                "children": [{"kind": "text", "text": "标题"}],
            },
            {
                "kind": "heading",
                "block_id": "h2",
                "level": 2,
                "style_name": "Heading 2",
                "children": [{"kind": "text", "text": "章节二"}],
            },
            {
                "kind": "heading",
                "block_id": "h3",
                "level": 3,
                "style_name": "Heading 3",
                "children": [{"kind": "text", "text": "章节三"}],
            },
        ],
    }

    assert project_to_delivery_blocks(document) == [
        {"type": "heading", "text": "章节二", "position": 0, "level": 2},
        {"type": "heading", "text": "章节三", "position": 1, "level": 3},
    ]

    mismatched = json.loads(json.dumps(document))
    mismatched["blocks"][1]["style_name"] = "Heading 4"
    with pytest.raises(ContentDocumentValidationError, match="不匹配"):
        project_to_delivery_blocks(mismatched)


def test_delivery_projection_rejects_unrepresentable_rich_features() -> None:
    document = mixed_document()
    with pytest.raises(ContentDocumentValidationError, match="marks/link"):
        project_to_delivery_blocks(document)


def test_pydantic_envelope_is_strict_and_style_dimensions_are_hash_sensitive() -> None:
    from content_studio.content_document import ContentDocument

    document = mixed_document()
    document["blocks"][1]["style_name"] = "Quote"
    document["blocks"][1]["children"][1]["width"] = 640
    document["blocks"][1]["children"][1]["height"] = 360
    parsed = ContentDocument.model_validate(document)
    assert parsed.schema_version == 2
    normalized = validate_document(document)
    assert normalized["blocks"][1]["style_name"] == "Quote"
    assert normalized["blocks"][1]["children"][1]["width"] == 640
    assert "paragraph_style" in document_features(document)
    assert "paragraph_style" in project_to_v1(document, omit_title_block=False).losses

    changed_dimension = json.loads(json.dumps(document))
    changed_dimension["blocks"][1]["children"][1]["width"] = 641
    assert document_hash(document) != document_hash(changed_dimension)

    for href in (
        "file:///tmp/x",
        "javascript:alert(1)",
        "\\\\server\\share",
        "https://example.test\\x",
    ):
        bad = mixed_document()
        bad["blocks"][1]["children"][0]["link"] = {"href": href}
        with pytest.raises(ContentDocumentValidationError):
            validate_document(bad)

    bad_asset = mixed_document()
    bad_asset["blocks"][1]["children"][1]["asset_id"] = "asset-one"
    with pytest.raises(ContentDocumentValidationError):
        validate_document(bad_asset)


def test_projection_merges_adjacent_text_runs_only_splitting_at_image() -> None:
    document = {
        "schema_version": 2,
        "blocks": [
            {
                "kind": "paragraph",
                "children": [
                    {"kind": "text", "text": "左", "marks": ["bold"]},
                    {"kind": "text", "text": "侧", "marks": ["italic"]},
                    {
                        "kind": "image",
                        "asset_id": "00000000-0000-4000-8000-000000000002",
                    },
                    {"kind": "text", "text": "右"},
                ],
            }
        ],
    }
    projected, losses = project_to_v1(document, omit_title_block=False)
    assert [block["type"] for block in projected] == ["text", "image", "text"]
    assert projected[0]["text"] == "左侧"
    assert projected[1]["asset_id"] == "00000000-0000-4000-8000-000000000002"
    assert projected[2]["text"] == "右"
    assert "marks" in losses
