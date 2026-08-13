"""平台富文本编辑器正文回读校验。

本模块只生成用于比较的规范化视图。调用方仍须把原始内容块写入平台，
不得用这里的规范化文本替换正文、回写草稿或重算 ``ContentVersion`` 哈希。
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

MAX_ENTITY_PASSES = 4
MAX_EXPECTED_CHARS = 200_000
MAX_ACTUAL_CHARS = 400_000
MAX_PREVIEW_CHARS = 32

_LINE_SEPARATOR_TRANSLATION = str.maketrans(
    {
        "\r": "\n",
        "\u0085": "\n",
        "\u2028": "\n",
        "\u2029": "\n",
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
    }
)
_EDITOR_PSEUDO_CHARACTERS = {"\u200b", "\u2060", "\ufeff"}
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+", re.UNICODE)
_ALL_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://\S+")
_EMAIL_RE = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_WINDOWS_PATH_RE = re.compile(
    r'''(?ix)
    (?:
        [a-z]:[\\/]
        | \\\\[^\\/\r\n<>:"|?*]+[\\/][^\r\n<>:"|?*]+[\\/]
    )
    (?:
        "[^"\r\n]*"
        | '[^'\r\n]*'
        | [^\r\n,;|]+?
    )
    (?=
        \s+(?:cookie|token|authorization|password|error|code)\s*[:=]
        | [\r\n,;|]
        | $
    )
    '''
)
_POSIX_PATH_RE = re.compile(
    r'''(?ix)
    (?<![\w:])/(?:home|Users|var|tmp|opt|srv|mnt)/
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,;|]+?)
    (?=\s+(?:cookie|token|authorization|password|error|code)\s*[:=]|[\r\n,;|]|$)
    '''
)
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_LONG_NUMBER_RE = re.compile(r"\b\d{7,}\b")


class ContentValidationError(RuntimeError):
    """正文无法证明完整写入平台编辑器。"""

    error_code = "CONTENT_VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class ExpectedParagraph:
    """从原始内容块派生的单个比较段落。"""

    ordinal: int
    block_index: int
    line_index: int
    comparison_text: str = field(repr=False)
    safe_preview: str
    char_length: int


@dataclass(frozen=True, slots=True)
class ParagraphMismatch:
    """第一个缺失或乱序的段落诊断信息。"""

    ordinal: int
    block_index: int
    line_index: int
    safe_preview: str
    char_length: int
    reason: str = "MISSING_OR_OUT_OF_ORDER"


@dataclass(frozen=True, slots=True)
class OrderedValidationResult:
    """有序段落匹配结果。"""

    ok: bool
    expected_count: int
    matched_count: int
    first_mismatch: ParagraphMismatch | None


def _require_bounded_text(value: str | None, *, limit: int, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContentValidationError(f"{label}必须是字符串")
    if len(value) > limit:
        raise ContentValidationError(f"{label}超过校验上限 {limit} 字符")
    return value


def _unescape_entities(value: str) -> str:
    current = value
    for _ in range(MAX_ENTITY_PASSES):
        decoded = html.unescape(current)
        if decoded == current:
            break
        current = decoded
    return current


def _canonicalize(
    value: str | None,
    *,
    limit: int,
    label: str,
    preserve_newlines: bool,
) -> str:
    text = _require_bounded_text(value, limit=limit, label=label)
    text = _unescape_entities(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").translate(_LINE_SEPARATOR_TRANSLATION)

    characters: list[str] = []
    for character in text:
        if character in _EDITOR_PSEUDO_CHARACTERS:
            continue
        if character != "\n" and unicodedata.category(character) == "Cc":
            characters.append(" ")
        else:
            characters.append(character)
    text = "".join(characters)

    if preserve_newlines:
        text = _HORIZONTAL_WHITESPACE_RE.sub(" ", text)
        return "\n".join(line.strip() for line in text.split("\n"))
    return _ALL_WHITESPACE_RE.sub(" ", text).strip()


def normalize_for_comparison(value: str | None) -> str:
    """将编辑器允许的无语义变换折叠为稳定比较视图。"""

    return _canonicalize(
        value,
        limit=MAX_ACTUAL_CHARS,
        label="编辑器正文",
        preserve_newlines=False,
    )


def _safe_preview(value: str) -> str:
    preview = _URL_RE.sub("[链接]", value)
    preview = _EMAIL_RE.sub("[邮箱]", preview)
    preview = _WINDOWS_PATH_RE.sub("[路径]", preview)
    preview = _POSIX_PATH_RE.sub("[路径]", preview)
    preview = _UUID_RE.sub("[标识]", preview)
    preview = _LONG_TOKEN_RE.sub("[令牌]", preview)
    preview = _LONG_NUMBER_RE.sub("[长数字]", preview)
    preview = "".join(
        character
        for character in preview
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    if len(preview) > MAX_PREVIEW_CHARS:
        return f"{preview[:MAX_PREVIEW_CHARS]}…"
    return preview


def extract_expected_paragraphs(
    content_blocks: Sequence[Mapping[str, object]],
) -> tuple[ExpectedParagraph, ...]:
    """从原始 ``text/heading`` 块提取非空段落，图片块由媒体契约校验。"""

    if isinstance(content_blocks, (str, bytes)) or not isinstance(
        content_blocks, Sequence
    ):
        raise ContentValidationError("正文内容块必须是序列")

    paragraphs: list[ExpectedParagraph] = []
    total_chars = 0
    for block_index, block in enumerate(content_blocks):
        if not isinstance(block, Mapping):
            raise ContentValidationError(f"正文块 #{block_index + 1} 不是对象")

        block_type = block.get("type")
        if block_type == "image":
            continue
        if block_type not in {"text", "heading"}:
            raise ContentValidationError(
                f"正文块 #{block_index + 1} 类型不受支持"
            )

        raw_text = block.get("text")
        if not isinstance(raw_text, str):
            raise ContentValidationError(
                f"正文块 #{block_index + 1} 的 text 必须是字符串"
            )
        total_chars += len(raw_text)
        if total_chars > MAX_EXPECTED_CHARS:
            raise ContentValidationError(
                f"待校验正文超过上限 {MAX_EXPECTED_CHARS} 字符"
            )

        canonical = _canonicalize(
            raw_text,
            limit=MAX_EXPECTED_CHARS,
            label=f"正文块 #{block_index + 1}",
            preserve_newlines=True,
        )
        for line_index, line in enumerate(canonical.split("\n")):
            comparison_text = _ALL_WHITESPACE_RE.sub(" ", line).strip()
            if not comparison_text:
                continue
            paragraphs.append(
                ExpectedParagraph(
                    ordinal=len(paragraphs) + 1,
                    block_index=block_index,
                    line_index=line_index,
                    comparison_text=comparison_text,
                    safe_preview=_safe_preview(comparison_text),
                    char_length=len(comparison_text),
                )
            )
    return tuple(paragraphs)


def validate_paragraphs_in_order(
    expected: Sequence[ExpectedParagraph],
    actual_text: str | None,
) -> OrderedValidationResult:
    """按出现顺序、非重叠地验证每个期望段落。"""

    actual = normalize_for_comparison(actual_text)
    cursor = 0
    matched_count = 0
    for paragraph in expected:
        position = actual.find(paragraph.comparison_text, cursor)
        if position < 0:
            mismatch = ParagraphMismatch(
                ordinal=paragraph.ordinal,
                block_index=paragraph.block_index,
                line_index=paragraph.line_index,
                safe_preview=paragraph.safe_preview,
                char_length=paragraph.char_length,
            )
            return OrderedValidationResult(
                ok=False,
                expected_count=len(expected),
                matched_count=matched_count,
                first_mismatch=mismatch,
            )
        cursor = position + len(paragraph.comparison_text)
        matched_count += 1

    return OrderedValidationResult(
        ok=True,
        expected_count=len(expected),
        matched_count=matched_count,
        first_mismatch=None,
    )


def ensure_valid_content(
    content_blocks: Sequence[Mapping[str, object]],
    actual_text: str | None,
    *,
    platform: str,
    phase: str,
) -> int:
    """校验正文并在首个缺失/乱序段落处抛出稳定错误。"""

    expected = extract_expected_paragraphs(content_blocks)
    result = validate_paragraphs_in_order(expected, actual_text)
    if result.ok:
        return result.expected_count

    mismatch = result.first_mismatch
    if mismatch is None:  # pragma: no cover - 防御不可达状态
        raise ContentValidationError(f"{platform} {phase}正文校验失败")
    raise ContentValidationError(
        f"{platform} {phase}正文校验失败：段落 #{mismatch.ordinal} "
        f"(块 {mismatch.block_index + 1}，行 {mismatch.line_index + 1}，"
        f"长度 {mismatch.char_length}，预览 {mismatch.safe_preview!r}) "
        "缺失或顺序错误"
    )


def safe_media_error(value: object, *, fallback: str) -> str:
    """生成可返回 API 的简短媒体错误，移除路径与疑似凭据。"""

    if not isinstance(value, str) or not value.strip():
        return fallback
    message = value.replace("\r", " ").replace("\n", " ")
    message = _URL_RE.sub("[链接]", message)
    message = _WINDOWS_PATH_RE.sub("[路径]", message)
    message = _POSIX_PATH_RE.sub("[路径]", message)
    message = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^,;|\r\n]+",
        "Authorization=[已脱敏]",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:set-)?cookie\s*[:=]\s*[^|\r\n]+",
        "Cookie=[已脱敏]",
        message,
    )
    message = re.sub(
        r"(?i)\b(token|password)\s*[:=]\s*[^\s,;|]+",
        r"\1=[已脱敏]",
        message,
    )
    message = _LONG_TOKEN_RE.sub("[敏感值]", message)
    message = _ALL_WHITESPACE_RE.sub(" ", message).strip()
    return message[:300] or fallback
