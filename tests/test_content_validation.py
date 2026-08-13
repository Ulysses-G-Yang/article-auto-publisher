"""富文本编辑器正文校验的纯函数回归测试。"""

from __future__ import annotations

import copy
import json
import unicodedata

import pytest

from platforms.content_validation import (
    MAX_ACTUAL_CHARS,
    MAX_EXPECTED_CHARS,
    ContentValidationError,
    ensure_valid_content,
    extract_expected_paragraphs,
    normalize_for_comparison,
    safe_media_error,
    validate_paragraphs_in_order,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A&amp;nbsp;B", "A B"),
        ("A&amp;amp;B", "A&B"),
        ("A&#x200b;B", "AB"),
        ("A\ufeffB\u2060C", "ABC"),
        ("A\u00a0B\u2007C\u202fD", "A B C D"),
        ("A\r\nB\rC\u0085D\u2028E\u2029F", "A B C D E F"),
        ("A\x00B", "A B"),
    ],
)
def test_normalize_editor_equivalences(raw: str, expected: str) -> None:
    assert normalize_for_comparison(raw) == expected


def test_normalize_uses_nfc_not_nfkc_and_preserves_joiners() -> None:
    decomposed = "Cafe\u0301"
    assert normalize_for_comparison(decomposed) == unicodedata.normalize("NFC", decomposed)
    assert normalize_for_comparison("Ａ") == "Ａ"
    assert normalize_for_comparison("a\u200cb\u200dc") == "a\u200cb\u200dc"


def test_nested_entity_unescape_is_bounded() -> None:
    four_layers = "&amp;amp;amp;amp;lt;"
    five_layers = "&amp;amp;amp;amp;amp;lt;"
    assert normalize_for_comparison(four_layers) == "&lt;"
    # 第五层按上限停止，证明这里没有无限 fixed-point 解码。
    assert normalize_for_comparison(five_layers) == "&amp;lt;"


def test_extract_expected_paragraphs_keeps_provenance_and_skips_images() -> None:
    blocks = [
        {"type": "heading", "text": "标题\r\n第二行"},
        {"type": "image", "asset_id": "asset-1"},
        {"type": "text", "text": "\n正文&nbsp;末尾\n"},
    ]
    paragraphs = extract_expected_paragraphs(blocks)
    assert [item.comparison_text for item in paragraphs] == [
        "标题",
        "第二行",
        "正文 末尾",
    ]
    assert [(item.block_index, item.line_index) for item in paragraphs] == [
        (0, 0),
        (0, 1),
        (2, 1),
    ]


@pytest.mark.parametrize(
    "blocks",
    [
        ["不是对象"],
        [{"type": "video", "text": "未来类型"}],
        [{"type": "text", "text": {"unexpected": "mapping"}}],
    ],
)
def test_extract_expected_paragraphs_fails_closed(blocks) -> None:
    with pytest.raises(ContentValidationError):
        extract_expected_paragraphs(blocks)


def test_empty_text_expectation_is_valid_for_image_only_content() -> None:
    expected = extract_expected_paragraphs([{"type": "image", "asset_id": "asset"}])
    result = validate_paragraphs_in_order(expected, "")
    assert result.ok
    assert result.expected_count == 0


def test_ordered_validation_requires_distinct_duplicate_occurrences() -> None:
    blocks = [
        {"type": "text", "text": "重复段"},
        {"type": "text", "text": "重复段"},
    ]
    expected = extract_expected_paragraphs(blocks)
    one = validate_paragraphs_in_order(expected, "重复段")
    two = validate_paragraphs_in_order(expected, "重复段\n重复段")
    assert not one.ok
    assert one.matched_count == 1
    assert one.first_mismatch is not None
    assert one.first_mismatch.ordinal == 2
    assert two.ok


@pytest.mark.parametrize(
    "actual",
    [
        "第三段 第二段 第一段",
        "第一段 第三段",
        "",
    ],
)
def test_ordered_validation_rejects_reverse_missing_and_empty(actual: str) -> None:
    expected = extract_expected_paragraphs(
        [{"type": "text", "text": "第一段\n第二段\n第三段"}]
    )
    assert not validate_paragraphs_in_order(expected, actual).ok


def test_ordered_validation_tolerates_extra_editor_text_and_entities() -> None:
    blocks = [{"type": "text", "text": "A&B\n第二 段"}]
    actual = "编辑器提示 A&amp;B \u200b 第二&nbsp;&nbsp;段 尾部提示"
    assert validate_paragraphs_in_order(
        extract_expected_paragraphs(blocks),
        actual,
    ).ok


def test_ensure_valid_content_has_stable_code_and_redacted_diagnostics() -> None:
    blocks = [{"type": "text", "text": "D:\\secret\\photo.png token_abcdefghijklmnopqrstuv"}]
    with pytest.raises(ContentValidationError) as exc_info:
        ensure_valid_content(blocks, "缺少正文", platform="ZOL", phase="输入后")
    error = exc_info.value
    assert error.error_code == "CONTENT_VALIDATION_ERROR"
    assert "D:\\secret" not in str(error)
    assert "abcdefghijklmnopqrstuv" not in str(error)


def test_validation_never_mutates_blocks_or_content_hash_input() -> None:
    blocks = [{"type": "text", "text": "A&amp;B\r\nCafe\u0301"}]
    before = copy.deepcopy(blocks)
    serialized_before = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
    ensure_valid_content(blocks, "A&B\nCafé", platform="测试", phase="回读")
    assert blocks == before
    assert json.dumps(blocks, ensure_ascii=False, sort_keys=True) == serialized_before


def test_validation_size_limits_are_explicit() -> None:
    with pytest.raises(ContentValidationError):
        extract_expected_paragraphs(
            [{"type": "text", "text": "x" * (MAX_EXPECTED_CHARS + 1)}]
        )
    with pytest.raises(ContentValidationError):
        normalize_for_comparison("x" * (MAX_ACTUAL_CHARS + 1))


@pytest.mark.parametrize(
    "physical_path",
    [
        r'D:\Secret Folder\private image.png',
        r'\\server\Private Share\private image.png',
        '/home/private user/private image.png',
    ],
)
def test_safe_media_error_removes_paths_with_spaces_and_unc(
    physical_path: str,
) -> None:
    safe = safe_media_error(
        f"set_input_files failed for {physical_path} token=secret_value_12345678901234567890",
        fallback="上传失败",
    )
    assert physical_path not in safe
    assert "private image.png" not in safe
    assert "Secret Folder" not in safe
    assert "Private Share" not in safe
    assert "private user" not in safe
    assert "[路径]" in safe

@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: Bearer short-secret", "short-secret"),
        ("Authorization=eyJhbGciOi.JwYXlsb2Fk.signature", "eyJhbGciOi"),
        ("Cookie: sid=short; theme=dark", "sid=short"),
        ("Set-Cookie: session=short-secret; HttpOnly", "short-secret"),
    ],
)
def test_safe_media_error_redacts_complete_auth_and_cookie_values(
    raw: str,
    secret: str,
) -> None:
    safe = safe_media_error(raw, fallback="上传失败")
    assert secret not in safe
    assert "[已脱敏]" in safe

def test_safe_media_error_removes_physical_paths_and_credentials() -> None:
    raw = (
        r"set_input_files failed for D:\Backup\private\image.png "
        "token=very_secret_value_12345678901234567890"
    )
    safe = safe_media_error(raw, fallback="上传失败")
    assert "D:\\Backup" not in safe
    assert "very_secret" not in safe
    assert "[路径]" in safe
