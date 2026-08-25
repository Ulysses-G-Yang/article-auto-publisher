"""Content Studio 的版本化图文文档核心。

本模块只处理内存中的 JSON-compatible 数据，不读取数据库、文件系统或网络，
也不接触 ``ContentAsset.storage_path``。它是 v1 图文块与后续平台渲染之间的
canonical 边界：输入先经过严格校验，输出只包含平台可以安全消费的公开资产 ID。

v2 envelope 的最小形式为::

    {
        "schema_version": 2,
        "title": "文章标题",
        "title_block_id": "block-title",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "paragraph",
                "block_id": "block-1",
                "children": [{"kind": "text", "text": "正文"}],
            }
        ],
    }

所有校验均 fail-closed：未知字段、路径字段、类型错误和超限结构都会抛出
``ContentDocumentValidationError``。模块不试图猜测遗失的 Word 样式；v1 升级
只会标记 ``LEGACY_PROJECTED``，明确告知调用方这是保守投影。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NamedTuple, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 2
LEGACY_PROJECTED = "LEGACY_PROJECTED"
NATIVE = "NATIVE"
DELIVERY_POLICY_VERSION = "stable_v1"

# 这些上限是文档层的防护，不替代 API 层和数据库层的资源限制。
MAX_BLOCKS = 2_000
MAX_TEXT_LENGTH = 200_000
MAX_TOTAL_TEXT_LENGTH = 200_000
MAX_TITLE_LENGTH = 200
MAX_NESTING_DEPTH = 16
MAX_LIST_LEVEL = 32
MAX_TABLE_SPAN = 100
MAX_ID_LENGTH = 128
MAX_URL_LENGTH = 4_096
MAX_IMAGE_DIMENSION = 100_000
MAX_POSITION = 100_000

FEATURE_KEYS = frozenset(
    {
        "heading",
        "list",
        "link",
        "table",
        "mixed_inline",
        "image_order",
        "caption",
        "floating_anchor",
        "marks",
        "paragraph_style",
    }
)

_MARKS = ("bold", "italic", "underline", "strike", "code")
_MARK_SET = frozenset(_MARKS)
_SOURCE_FIDELITY = frozenset({NATIVE, LEGACY_PROJECTED})
_BLOCK_KINDS = frozenset({"paragraph", "heading", "list", "table"})
_INLINE_KINDS = frozenset({"text", "image"})


class _StrictModel(BaseModel):
    """Pydantic v2 的严格 JSON 模型基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentLink(_StrictModel):
    href: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    title: str | None = Field(default=None, max_length=500)

    @field_validator("href")
    @classmethod
    def validate_href(cls, value: str) -> str:
        if any(ord(char) < 0x20 for char in value) or "\\" in value:
            raise ValueError("href 只能是安全的 HTTP(S) URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("href 只能是安全的 HTTP(S) URL")
        return value


class DocumentAnchor(_StrictModel):
    kind: Literal["inline", "floating"]


class TextNode(_StrictModel):
    kind: Literal["text"]
    text: str = Field(max_length=MAX_TEXT_LENGTH)
    marks: list[Literal["bold", "italic", "underline", "strike", "code"]] = Field(
        default_factory=list, max_length=len(_MARKS)
    )
    link: DocumentLink | None = None


class ImageNode(_StrictModel):
    kind: Literal["image"]
    asset_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    alt: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=2_000)
    anchor: DocumentAnchor | None = None
    width: int | None = Field(default=None, gt=0, le=MAX_IMAGE_DIMENSION)
    height: int | None = Field(default=None, gt=0, le=MAX_IMAGE_DIMENSION)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return _safe_asset_id(value, "image.asset_id")


InlineNode: TypeAlias = Annotated[TextNode | ImageNode, Field(discriminator="kind")]


class ParagraphBlock(_StrictModel):
    kind: Literal["paragraph"]
    block_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)
    style_name: str | None = Field(default=None, min_length=1, max_length=128)
    children: list[InlineNode] = Field(max_length=MAX_BLOCKS)


class HeadingBlock(_StrictModel):
    kind: Literal["heading"]
    block_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)
    style_name: str | None = Field(default=None, min_length=1, max_length=128)
    level: int = Field(ge=1, le=6)
    children: list[InlineNode] = Field(max_length=MAX_BLOCKS)


class ListItem(_StrictModel):
    blocks: list[Block] = Field(max_length=MAX_BLOCKS)


class ListBlock(_StrictModel):
    kind: Literal["list"]
    block_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)
    ordered: bool
    level: int = Field(ge=0, le=MAX_LIST_LEVEL)
    items: list[ListItem] = Field(max_length=MAX_BLOCKS)


class TableCell(_StrictModel):
    blocks: list[Block] = Field(max_length=MAX_BLOCKS)
    colspan: int = Field(default=1, ge=1, le=MAX_TABLE_SPAN)
    rowspan: int = Field(default=1, ge=1, le=MAX_TABLE_SPAN)


class TableRow(_StrictModel):
    cells: list[TableCell] = Field(max_length=MAX_BLOCKS)


class TableBlock(_StrictModel):
    kind: Literal["table"]
    block_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)
    rows: list[TableRow] = Field(max_length=MAX_BLOCKS)


Block: TypeAlias = Annotated[
    ParagraphBlock | HeadingBlock | ListBlock | TableBlock, Field(discriminator="kind")
]


class ContentDocument(_StrictModel):
    """严格的 v2 envelope；未知字段由 ``extra=forbid`` 拒绝。"""

    schema_version: Literal[2]
    title: str = Field(default="", max_length=MAX_TITLE_LENGTH)
    title_block_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)
    source_fidelity: Literal["NATIVE", "LEGACY_PROJECTED"] = NATIVE
    blocks: list[Block] = Field(max_length=MAX_BLOCKS)

    @model_validator(mode="after")
    def validate_semantics(self) -> ContentDocument:
        # Pydantic 的 discriminator/extra 校验负责形状；共享的纯语义校验再
        # 检查深度、重复 block_id、总文本长度和 title_block_id 引用完整性。
        validate_document(self.model_dump(mode="json", exclude_none=True))
        return self


# 递归 Block 别名在类声明后才完整，显式 rebuild 让 Pydantic v2 在 import 时
# 立即解析并在 .model_validate() 时保持 strict discriminator 行为。
ListItem.model_rebuild()
TableCell.model_rebuild()
ContentDocument.model_rebuild()


class ContentDocumentValidationError(ValueError):
    """v2 文档不满足严格契约。"""


# 常见调用方会把错误类命名为 ContentDocumentError；保留一个明确别名，
# 但不引入额外的错误层级，便于 API 层统一捕获 ValueError。
ContentDocumentError = ContentDocumentValidationError


def _fail(path: str, message: str) -> None:
    raise ContentDocumentValidationError(f"{path}: {message}")


def _ensure_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "必须是对象")
    # Mapping 的键不一定是字符串；先在这里拒绝，避免 canonical JSON 隐式转换。
    if any(not isinstance(key, str) for key in value):
        _fail(path, "对象键必须是字符串")
    if "storage_path" in value:
        _fail(path, "禁止包含 storage_path")
    return value


def _ensure_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "必须是数组")
    return value


def _check_keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        _fail(path, f"包含未知字段: {', '.join(sorted(unknown))}")


def _string(value: Any, path: str, *, max_length: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(path, "必须是字符串")
    if len(value) > max_length:
        _fail(path, f"长度不能超过 {max_length}")
    if not allow_empty and not value:
        _fail(path, "不能为空")
    return value


def _safe_identifier(value: Any, path: str) -> str:
    result = _string(value, path, max_length=MAX_ID_LENGTH, allow_empty=False)
    if "/" in result or "\\" in result or "\x00" in result:
        _fail(path, "不能是路径或包含 NUL")
    return result


def _safe_asset_id(value: Any, path: str) -> str:
    """资产引用必须是 UUID 字符串，不能借机传入路径。"""

    result = _string(value, path, max_length=36, allow_empty=False)
    try:
        parsed = uuid.UUID(result)
    except (ValueError, AttributeError, TypeError):
        _fail(path, "必须是标准 UUID 字符串")
    if str(parsed) != result.lower() or len(result) != 36:
        _fail(path, "必须是标准 UUID 字符串")
    return result


def _optional_string(
    value: Any,
    path: str,
    *,
    max_length: int,
    allow_empty: bool = True,
) -> str | None:
    if value is None:
        return None
    return _string(value, path, max_length=max_length, allow_empty=allow_empty)


def _optional_identifier(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _safe_identifier(value, path)


def _int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    # bool 是 int 的子类，但在契约中不是合法的数字字段。
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "必须是整数")
    if not minimum <= value <= maximum:
        _fail(path, f"必须在 {minimum}..{maximum} 范围内")
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "必须是布尔值")
    return value


def _copy_link(value: Any, path: str) -> dict[str, str] | None:
    if value is None:
        return None
    link = _ensure_mapping(value, path)
    _check_keys(link, {"href", "title"}, path)
    if "href" not in link:
        _fail(path, "缺少 href")
    href = _string(link["href"], f"{path}.href", max_length=MAX_URL_LENGTH, allow_empty=False)
    if any(ord(char) < 0x20 for char in href) or "\\" in href:
        _fail(f"{path}.href", "只能使用安全的 HTTP(S) URL")
    parsed = urlsplit(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _fail(f"{path}.href", "只能使用安全的 HTTP(S) URL")
    result = {"href": href}
    if "title" in link and link["title"] is not None:
        result["title"] = _string(link["title"], f"{path}.title", max_length=500, allow_empty=True)
    return result


def _copy_marks(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    marks = _ensure_list(value, path)
    if len(marks) > len(_MARKS):
        _fail(path, "marks 数量超限")
    normalized: set[str] = set()
    for index, mark in enumerate(marks):
        if not isinstance(mark, str) or mark not in _MARK_SET:
            _fail(f"{path}[{index}]", "不是受支持的 mark")
        if mark in normalized:
            _fail(f"{path}[{index}]", "marks 不能重复")
        normalized.add(mark)
    # mark 是无序语义集合，用固定声明顺序输出，确保 hash 不受输入顺序影响。
    return [mark for mark in _MARKS if mark in normalized]


def _copy_inline(value: Any, path: str, counters: _Counters, depth: int) -> dict[str, Any]:
    if depth > MAX_NESTING_DEPTH:
        _fail(path, f"嵌套深度不能超过 {MAX_NESTING_DEPTH}")
    item = _ensure_mapping(value, path)
    kind = item.get("kind")
    if kind == "text":
        _check_keys(item, {"kind", "text", "marks", "link"}, path)
        if "text" not in item:
            _fail(path, "text 缺失")
        text = _string(item["text"], f"{path}.text", max_length=MAX_TEXT_LENGTH)
        counters.text_length += len(text)
        if counters.text_length > MAX_TOTAL_TEXT_LENGTH:
            _fail(path, f"文档文本总长度不能超过 {MAX_TOTAL_TEXT_LENGTH}")
        result: dict[str, Any] = {"kind": "text", "text": text}
        marks = _copy_marks(item.get("marks"), f"{path}.marks")
        if marks:
            result["marks"] = marks
            counters.features.add("marks")
        link = _copy_link(item.get("link"), f"{path}.link")
        if link is not None:
            result["link"] = link
            counters.features.add("link")
        return result
    if kind == "image":
        _check_keys(item, {"kind", "asset_id", "alt", "caption", "anchor", "width", "height"}, path)
        if "asset_id" not in item:
            _fail(path, "image 缺少 asset_id")
        result = {"kind": "image", "asset_id": _safe_asset_id(item["asset_id"], f"{path}.asset_id")}
        alt = _optional_string(item.get("alt"), f"{path}.alt", max_length=500)
        caption = _optional_string(item.get("caption"), f"{path}.caption", max_length=2_000)
        if alt is not None:
            result["alt"] = alt
        if caption is not None:
            result["caption"] = caption
            counters.features.add("caption")
        for dimension in ("width", "height"):
            if dimension in item and item[dimension] is not None:
                result[dimension] = _int(
                    item[dimension],
                    f"{path}.{dimension}",
                    minimum=1,
                    maximum=MAX_IMAGE_DIMENSION,
                )
        if "anchor" in item and item["anchor"] is not None:
            anchor = _ensure_mapping(item["anchor"], f"{path}.anchor")
            _check_keys(anchor, {"kind"}, f"{path}.anchor")
            anchor_kind = anchor.get("kind")
            if anchor_kind not in {"inline", "floating"}:
                _fail(f"{path}.anchor.kind", "必须是 inline 或 floating")
            result["anchor"] = {"kind": anchor_kind}
            if anchor_kind == "floating":
                counters.features.add("floating_anchor")
        return result
    _fail(path, "inline kind 必须是 text 或 image")


class _Counters:
    def __init__(self) -> None:
        self.block_count = 0
        self.text_length = 0
        self.features: set[str] = set()
        self.image_count = 0


def _copy_children(value: Any, path: str, counters: _Counters, depth: int) -> list[dict[str, Any]]:
    children = _ensure_list(value, path)
    if len(children) > MAX_BLOCKS:
        _fail(path, f"子节点数量不能超过 {MAX_BLOCKS}")
    result = [
        _copy_inline(item, f"{path}[{index}]", counters, depth + 1)
        for index, item in enumerate(children)
    ]
    image_positions = [index for index, item in enumerate(result) if item["kind"] == "image"]
    counters.image_count += len(image_positions)
    if image_positions and any(item["kind"] == "text" for item in result):
        # 只有文字和图片交错/并存时才需要 mixed_inline 能力；纯图片段落不算混排。
        counters.features.add("mixed_inline")
    return result


def _block_id(item: Mapping[str, Any], path: str) -> str | None:
    if "block_id" not in item or item["block_id"] is None:
        return None
    return _safe_identifier(item["block_id"], f"{path}.block_id")


def _copy_block(value: Any, path: str, counters: _Counters, depth: int) -> dict[str, Any]:
    if depth > MAX_NESTING_DEPTH:
        _fail(path, f"嵌套深度不能超过 {MAX_NESTING_DEPTH}")
    block = _ensure_mapping(value, path)
    kind = block.get("kind")
    if kind not in _BLOCK_KINDS:
        _fail(path, "block kind 不受支持")
    counters.block_count += 1
    if counters.block_count > MAX_BLOCKS:
        _fail(path, f"block 数量不能超过 {MAX_BLOCKS}")

    common = {"kind", "block_id", "style_name"}
    result: dict[str, Any] = {"kind": kind}
    block_id = _block_id(block, path)
    if block_id is not None:
        result["block_id"] = block_id

    if kind == "paragraph":
        _check_keys(block, common | {"children"}, path)
        if "children" not in block:
            _fail(path, "paragraph 缺少 children")
        style_name = _optional_string(
            block.get("style_name"), f"{path}.style_name", max_length=128, allow_empty=False
        )
        if style_name is not None:
            result["style_name"] = style_name
            if style_name != "Normal":
                counters.features.add("paragraph_style")
        result["children"] = _copy_children(block["children"], f"{path}.children", counters, depth)
        return result

    if kind == "heading":
        _check_keys(block, common | {"level", "children"}, path)
        if "level" not in block or "children" not in block:
            _fail(path, "heading 必须包含 level 和 children")
        style_name = _optional_string(
            block.get("style_name"), f"{path}.style_name", max_length=128, allow_empty=False
        )
        if style_name is not None:
            result["style_name"] = style_name
        result["level"] = _int(block["level"], f"{path}.level", minimum=1, maximum=6)
        result["children"] = _copy_children(block["children"], f"{path}.children", counters, depth)
        counters.features.add("heading")
        return result

    if kind == "list":
        _check_keys(block, common | {"ordered", "level", "items"}, path)
        for field in ("ordered", "level", "items"):
            if field not in block:
                _fail(path, f"list 缺少 {field}")
        result["ordered"] = _bool(block["ordered"], f"{path}.ordered")
        result["level"] = _int(block["level"], f"{path}.level", minimum=0, maximum=MAX_LIST_LEVEL)
        items = _ensure_list(block["items"], f"{path}.items")
        if len(items) > MAX_BLOCKS:
            _fail(f"{path}.items", f"条目数量不能超过 {MAX_BLOCKS}")
        normalized_items = []
        for index, raw_item in enumerate(items):
            item = _ensure_mapping(raw_item, f"{path}.items[{index}]")
            _check_keys(item, {"blocks"}, f"{path}.items[{index}]")
            if "blocks" not in item:
                _fail(f"{path}.items[{index}]", "缺少 blocks")
            nested = _ensure_list(item["blocks"], f"{path}.items[{index}].blocks")
            normalized_items.append(
                {
                    "blocks": [
                        _copy_block(
                            raw, f"{path}.items[{index}].blocks[{block_index}]", counters, depth + 1
                        )
                        for block_index, raw in enumerate(nested)
                    ]
                }
            )
        result["items"] = normalized_items
        counters.features.add("list")
        return result

    _check_keys(block, common | {"rows"}, path)
    if "rows" not in block:
        _fail(path, "table 缺少 rows")
    rows = _ensure_list(block["rows"], f"{path}.rows")
    if len(rows) > MAX_BLOCKS:
        _fail(f"{path}.rows", f"行数不能超过 {MAX_BLOCKS}")
    normalized_rows = []
    for row_index, raw_row in enumerate(rows):
        row = _ensure_mapping(raw_row, f"{path}.rows[{row_index}]")
        _check_keys(row, {"cells"}, f"{path}.rows[{row_index}]")
        if "cells" not in row:
            _fail(f"{path}.rows[{row_index}]", "缺少 cells")
        cells = _ensure_list(row["cells"], f"{path}.rows[{row_index}].cells")
        normalized_cells = []
        for cell_index, raw_cell in enumerate(cells):
            cell = _ensure_mapping(raw_cell, f"{path}.rows[{row_index}].cells[{cell_index}]")
            _check_keys(
                cell,
                {"blocks", "colspan", "rowspan"},
                f"{path}.rows[{row_index}].cells[{cell_index}]",
            )
            if "blocks" not in cell:
                _fail(f"{path}.rows[{row_index}].cells[{cell_index}]", "缺少 blocks")
            nested = _ensure_list(
                cell["blocks"], f"{path}.rows[{row_index}].cells[{cell_index}].blocks"
            )
            normalized_cell: dict[str, Any] = {
                "blocks": [
                    _copy_block(
                        raw,
                        f"{path}.rows[{row_index}].cells[{cell_index}].blocks[{block_index}]",
                        counters,
                        depth + 1,
                    )
                    for block_index, raw in enumerate(nested)
                ]
            }
            for span in ("colspan", "rowspan"):
                if span in cell:
                    normalized_cell[span] = _int(
                        cell[span],
                        f"{path}.rows[{row_index}].cells[{cell_index}].{span}",
                        minimum=1,
                        maximum=MAX_TABLE_SPAN,
                    )
            normalized_cells.append(normalized_cell)
        normalized_rows.append({"cells": normalized_cells})
    result["rows"] = normalized_rows
    counters.features.add("table")
    return result


def validate_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """严格校验并返回不含路径字段的 canonical v2 文档副本。

    传入对象不会被修改。返回值只由 JSON 基本类型组成，适合写入 JSON 列；
    但本批不会让现有 service 自动写入该列。
    """

    envelope = _ensure_mapping(document, "document")
    _check_keys(
        envelope,
        {"schema_version", "title", "title_block_id", "source_fidelity", "blocks"},
        "document",
    )
    if envelope.get("schema_version") != SCHEMA_VERSION:
        _fail("document.schema_version", f"必须是 {SCHEMA_VERSION}")
    title = _string(envelope.get("title", ""), "document.title", max_length=MAX_TITLE_LENGTH)
    title_block_id = _optional_identifier(envelope.get("title_block_id"), "document.title_block_id")
    source_fidelity = envelope.get("source_fidelity", NATIVE)
    if source_fidelity not in _SOURCE_FIDELITY:
        _fail("document.source_fidelity", "不是受支持的来源保真度")
    if "blocks" not in envelope:
        _fail("document", "缺少 blocks")
    raw_blocks = _ensure_list(envelope["blocks"], "document.blocks")
    if len(raw_blocks) > MAX_BLOCKS:
        _fail("document.blocks", f"block 数量不能超过 {MAX_BLOCKS}")
    counters = _Counters()
    normalized_blocks = [
        _copy_block(block, f"document.blocks[{index}]", counters, 1)
        for index, block in enumerate(raw_blocks)
    ]
    all_block_ids = [
        block["block_id"] for block in _walk_blocks(normalized_blocks) if "block_id" in block
    ]
    if len(all_block_ids) != len(set(all_block_ids)):
        _fail("document.blocks", "block_id 不能重复")
    if title_block_id is not None and title_block_id not in all_block_ids:
        _fail("document.title_block_id", "必须引用 blocks 中存在的 block_id")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "blocks": normalized_blocks,
    }
    if title_block_id is not None:
        result["title_block_id"] = title_block_id
    # source_fidelity 始终落盘，避免 canonical 文档在默认值语义上产生歧义。
    result["source_fidelity"] = source_fidelity
    return result


def _walk_blocks(blocks: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    for block in blocks:
        yield block
        kind = block.get("kind")
        if kind == "list":
            for item in block.get("items", []):
                yield from _walk_blocks(item.get("blocks", []))
        elif kind == "table":
            for row in block.get("rows", []):
                for cell in row.get("cells", []):
                    yield from _walk_blocks(cell.get("blocks", []))


def canonical_document_json(document: Mapping[str, Any]) -> str:
    """返回稳定 canonical JSON；对象键插入顺序不影响结果。"""

    normalized = validate_document(document)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def document_hash(document: Mapping[str, Any]) -> str:
    """返回只覆盖文档本体的 SHA-256，不包含封面策略或数据库字段。"""

    return hashlib.sha256(canonical_document_json(document).encode("utf-8")).hexdigest()


# 便于调用方按常见命名使用；实现仍只有一套。
canonical_json = canonical_document_json
hash_document = document_hash
content_hash = document_hash


def _legacy_identifier(index: int, raw: Mapping[str, Any]) -> str:
    value = raw.get("block_id")
    if value is not None:
        return _safe_identifier(value, f"blocks[{index}].block_id")
    return f"legacy-block-{index:06d}"


def _legacy_position(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return _int(value, path, minimum=0, maximum=MAX_POSITION)


def _normalized_title(value: str) -> str:
    """只用于匹配标题块；不改变 canonical 文档里的原始文字。"""

    return " ".join(unicodedata.normalize("NFC", value).split())


def upgrade_v1(title: str, blocks_json: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把旧 ``text/image`` blocks 投影到内存 v2 envelope。

    v1 已丢失的 run 样式、链接、列表、表格和浮动定位不会被猜测；输出只保留
    可证明存在的文字、图片顺序和资产 ID，并标记 ``LEGACY_PROJECTED``。
    """

    title_value = _string(title, "title", max_length=MAX_TITLE_LENGTH)
    if not isinstance(blocks_json, Sequence) or isinstance(blocks_json, (str, bytes, bytearray)):
        _fail("blocks_json", "必须是对象数组")
    if len(blocks_json) > MAX_BLOCKS:
        _fail("blocks_json", f"block 数量不能超过 {MAX_BLOCKS}")
    blocks: list[dict[str, Any]] = []
    title_block_id: str | None = None
    ordered_entries: list[tuple[int | None, int, Mapping[str, Any]]] = []
    for original_index, raw_value in enumerate(blocks_json):
        path = f"blocks_json[{original_index}]"
        raw = _ensure_mapping(raw_value, path)
        _check_keys(raw, {"block_id", "type", "text", "asset_id", "alt", "position"}, path)
        position = _legacy_position(raw.get("position"), f"{path}.position")
        ordered_entries.append((position, original_index, raw))

    # 只有合法 position 参与升序；缺失位置排在有位置项之后，且每组均保持
    # 原物理顺序（Python sorted 是稳定排序）。
    ordered_entries.sort(
        key=lambda item: (item[0] is None, item[0] if item[0] is not None else 0, item[1])
    )
    normalized_title = _normalized_title(title_value)
    for _output_index, (_position, _stable_index, raw) in enumerate(ordered_entries):
        path = f"blocks_json[{_stable_index}]"
        kind = raw.get("type")
        block_id = _legacy_identifier(_stable_index, raw)
        if kind == "text":
            if "text" not in raw:
                _fail(path, "text block 缺少 text")
            text = _string(raw["text"], f"{path}.text", max_length=MAX_TEXT_LENGTH)
            child: dict[str, Any] = {"kind": "text", "text": text}
            block = {"kind": "paragraph", "block_id": block_id, "children": [child]}
            if (
                title_block_id is None
                and normalized_title
                and _normalized_title(text) == normalized_title
            ):
                title_block_id = block_id
        elif kind == "image":
            if "asset_id" not in raw:
                _fail(path, "image block 缺少 asset_id")
            child = {
                "kind": "image",
                "asset_id": _safe_identifier(raw["asset_id"], f"{path}.asset_id"),
            }
            alt = _optional_string(raw.get("alt"), f"{path}.alt", max_length=500)
            if alt is not None:
                child["alt"] = alt
            block = {"kind": "paragraph", "block_id": block_id, "children": [child]}
        else:
            _fail(path, "v1 type 必须是 text 或 image")
        blocks.append(block)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "title": title_value,
        "blocks": blocks,
        "source_fidelity": LEGACY_PROJECTED,
    }
    if title_block_id is not None:
        result["title_block_id"] = title_block_id
    return validate_document(result)


class V1Projection(NamedTuple):
    """v2→v1 投影结果；支持 ``blocks, losses = project_to_v1(...)``。"""

    blocks: list[dict[str, Any]]
    losses: set[str]


def project_to_v1(document: Mapping[str, Any], *, omit_title_block: bool = True) -> V1Projection:
    """将 v2 文档投影为旧 text/image blocks 与明确的 loss 集合。

    v2 的文字/图片子节点按原顺序展开，因此 ``文字→图片→文字`` 不会被重排。
    该函数只返回公开 ``asset_id``，绝不返回本机路径。
    """

    normalized = validate_document(document)
    title_block_id = normalized.get("title_block_id")
    losses: set[str] = set()
    projected: list[dict[str, Any]] = []

    def add_text(text: str, source_id: str | None) -> None:
        projected.append(
            {
                "block_id": _projection_id(source_id, len(projected)),
                "type": "text",
                "text": text,
                "position": len(projected),
            }
        )

    def add_image(item: Mapping[str, Any], source_id: str | None) -> None:
        output: dict[str, Any] = {
            "block_id": _projection_id(source_id, len(projected)),
            "type": "image",
            "asset_id": item["asset_id"],
            "position": len(projected),
        }
        if item.get("alt") is not None:
            output["alt"] = item["alt"]
        projected.append(output)
        if item.get("caption") is not None:
            losses.add("caption")
        if item.get("anchor", {}).get("kind") == "floating":
            losses.add("floating_anchor")

    def inline_children(children: Sequence[Mapping[str, Any]], source_id: str | None) -> None:
        # v1 的一个 text block 对应一个可见段落，不应把 Word 的相邻 runs
        # 误投影成多个段落。图片是唯一需要切断文字串的边界。
        pending_text: list[str] = []
        pending_marks = False
        pending_link = False

        def flush_text() -> None:
            nonlocal pending_marks, pending_link
            if pending_text:
                add_text("".join(pending_text), source_id)
                pending_text.clear()
            if pending_marks:
                losses.add("marks")
            if pending_link:
                losses.add("link")
            pending_marks = False
            pending_link = False

        for child in children:
            if child["kind"] == "text":
                pending_text.append(child["text"])
                pending_marks = pending_marks or bool(child.get("marks"))
                pending_link = pending_link or child.get("link") is not None
            else:
                flush_text()
                add_image(child, source_id)
        flush_text()

    def nested_blocks(blocks: Sequence[Mapping[str, Any]]) -> None:
        for block in blocks:
            project_block(block)

    def project_block(block: Mapping[str, Any]) -> None:
        source_id = block.get("block_id")
        if omit_title_block and source_id is not None and source_id == title_block_id:
            losses.add("title_block_omitted")
            return
        kind = block["kind"]
        if kind in {"paragraph", "heading"}:
            inline_children(block["children"], source_id)
            if kind == "heading":
                losses.add("heading")
            elif block.get("style_name") not in (None, "Normal"):
                losses.add("paragraph_style")
            return
        if kind == "list":
            losses.add("list")
            for item in block["items"]:
                nested_blocks(item["blocks"])
            return
        losses.add("table")
        for row in block["rows"]:
            for cell in row["cells"]:
                nested_blocks(cell["blocks"])
                if cell.get("colspan", 1) != 1 or cell.get("rowspan", 1) != 1:
                    losses.add("table_span")

    for block in normalized["blocks"]:
        project_block(block)
    return V1Projection(projected, losses)


def _projection_id(source_id: str | None, position: int) -> str:
    if source_id:
        candidate = f"{source_id}:v1:{position}"
    else:
        candidate = f"v1-block-{position:06d}"
    return candidate[:MAX_ID_LENGTH]


@dataclass(frozen=True)
class PlatformCapabilities:
    """平台声明的 v2 特性集合。

    ``exact=True`` 是默认值：只要文档要求的平台能力不在 ``supported`` 中，
    调用方就应拒绝投递，而不是偷偷降级。``supported`` 只允许声明本模块定义
    的 feature key，避免配置拼写错误变成虚假的兼容性。
    """

    name: str
    supported: frozenset[str]
    exact: bool = True

    def __init__(self, name: str, supported: Iterable[str] = (), *, exact: bool = True) -> None:
        normalized = frozenset(supported)
        unknown = normalized - FEATURE_KEYS
        if unknown:
            raise ContentDocumentValidationError(
                f"platform {name}: 未知 capability: {', '.join(sorted(unknown))}"
            )
        if not isinstance(name, str) or not name.strip():
            raise ContentDocumentValidationError("platform name 不能为空")
        if not isinstance(exact, bool):
            raise ContentDocumentValidationError("exact 必须是布尔值")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "supported", normalized)
        object.__setattr__(self, "exact", exact)


@dataclass(frozen=True)
class CompatibilityResult:
    required: frozenset[str]
    supported: frozenset[str]
    incompatibilities: frozenset[str]
    exact: bool

    @property
    def compatible(self) -> bool:
        return not self.incompatibilities

    @property
    def ok(self) -> bool:
        return self.compatible


def document_features(document: Mapping[str, Any]) -> frozenset[str]:
    """计算文档实际使用的 v2 feature keys。"""

    normalized = validate_document(document)
    counters = _Counters()
    # 用校验后的结构重新遍历，避免把实现细节暴露给调用方。
    for block in normalized["blocks"]:
        _feature_block(block, counters)
    return frozenset(counters.features)


def _feature_block(
    block: Mapping[str, Any],
    counters: _Counters,
    *,
    excluded_block_id: str | None = None,
) -> None:
    if excluded_block_id is not None and block.get("block_id") == excluded_block_id:
        return
    kind = block["kind"]
    if kind == "heading":
        counters.features.add("heading")
        for child in block["children"]:
            if child["kind"] == "text":
                if child.get("marks"):
                    counters.features.add("marks")
                if child.get("link") is not None:
                    counters.features.add("link")
            elif child.get("caption") is not None:
                counters.features.add("caption")
            if child.get("anchor", {}).get("kind") == "floating":
                counters.features.add("floating_anchor")
        if any(child["kind"] == "image" for child in block["children"]) and any(
            child["kind"] == "text" for child in block["children"]
        ):
            counters.features.add("mixed_inline")
    elif kind == "paragraph":
        if block.get("style_name") not in (None, "Normal"):
            counters.features.add("paragraph_style")
        text_found = False
        image_found = False
        for child in block["children"]:
            if child["kind"] == "text":
                text_found = True
                if child.get("marks"):
                    counters.features.add("marks")
                if child.get("link") is not None:
                    counters.features.add("link")
            else:
                image_found = True
                if child.get("caption") is not None:
                    counters.features.add("caption")
                if child.get("anchor", {}).get("kind") == "floating":
                    counters.features.add("floating_anchor")
        if text_found and image_found:
            counters.features.add("mixed_inline")
    elif kind == "list":
        counters.features.add("list")
        for item in block["items"]:
            for nested in item["blocks"]:
                _feature_block(
                    nested,
                    counters,
                    excluded_block_id=excluded_block_id,
                )
    elif kind == "table":
        counters.features.add("table")
        for row in block["rows"]:
            for cell in row["cells"]:
                for nested in cell["blocks"]:
                    _feature_block(
                        nested,
                        counters,
                        excluded_block_id=excluded_block_id,
                    )


def _image_count(document: Mapping[str, Any]) -> int:
    count = 0
    for block in _walk_all_blocks(validate_document(document)):
        if block["kind"] in {"paragraph", "heading"}:
            count += sum(child["kind"] == "image" for child in block["children"])
    return count


def _image_count_excluding(
    blocks: Iterable[Mapping[str, Any]],
    excluded_block_id: str | None,
) -> int:
    count = 0
    for block in blocks:
        if excluded_block_id is not None and block.get("block_id") == excluded_block_id:
            continue
        kind = block["kind"]
        if kind in {"paragraph", "heading"}:
            count += sum(child["kind"] == "image" for child in block["children"])
        elif kind == "list":
            for item in block["items"]:
                count += _image_count_excluding(item["blocks"], excluded_block_id)
        elif kind == "table":
            for row in block["rows"]:
                for cell in row["cells"]:
                    count += _image_count_excluding(cell["blocks"], excluded_block_id)
    return count


def _walk_all_blocks(document: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield from _walk_blocks(document["blocks"])


def required_features(document: Mapping[str, Any]) -> frozenset[str]:
    """返回平台兼容性判断需要的 feature 集合。

    多图正文要求平台声明 ``image_order``，因为旧投递器可能会把多图排序或
    合并；单图不额外要求该能力。
    """

    features = set(document_features(document))
    if _image_count(document) > 1:
        features.add("image_order")
    return frozenset(features)


def delivery_features(document: Mapping[str, Any]) -> frozenset[str]:
    """返回投递正文所需能力，不把独立映射的标题块计入其中。

    ``required_features`` 保留文档完整能力的历史语义。投递时标题由平台
    标题字段单独映射，因此必须排除 ``title_block_id`` 对应 block；正文的
    heading、图片顺序以及其他富文本能力仍按原结构递归计算。
    """

    normalized = validate_document(document)
    title_block_id = normalized.get("title_block_id")
    counters = _Counters()
    for block in normalized["blocks"]:
        _feature_block(
            block,
            counters,
            excluded_block_id=title_block_id,
        )
    if _image_count_excluding(normalized["blocks"], title_block_id) > 1:
        counters.features.add("image_order")
    return frozenset(counters.features)


def delivery_heading_levels(document: Mapping[str, Any]) -> frozenset[int]:
    """返回正文中需要平台保留的标题层级。

    ``title_block_id`` 对应的文档标题由平台标题字段单独映射，不属于正文
    标题能力。返回值只来自已通过 canonical 校验的 v2 文档，调用方可以用
    它和平台声明的真实支持层级做精确的 fail-closed 比较。
    """

    normalized = validate_document(document)
    excluded_id = normalized.get("title_block_id")
    levels: set[int] = set()

    def visit(blocks: Iterable[Mapping[str, Any]]) -> None:
        for block in blocks:
            if excluded_id is not None and block.get("block_id") == excluded_id:
                continue
            kind = block["kind"]
            if kind == "heading":
                levels.add(block["level"])
            elif kind == "list":
                for item in block["items"]:
                    visit(item["blocks"])
            elif kind == "table":
                for row in block["rows"]:
                    for cell in row["cells"]:
                        visit(cell["blocks"])

    visit(normalized["blocks"])
    return frozenset(levels)


def normalize_for_delivery(
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成六平台可稳定表达的基础投递副本。

    原始 canonical 文档绝不被修改。这里只降级不改变可见内容的展示属性：
    去除 marks/link、把正文标题统一为 H2，并把同段图文按原 child 顺序拆成
    独立块。列表、表格、说明文字和浮动图片等有结构损失风险的内容继续保留，
    后续能力门仍会 fail closed。
    """

    normalized = validate_document(document)
    title_block_id = normalized.get("title_block_id")
    removed_marks: list[dict[str, Any]] = []
    removed_links: list[dict[str, Any]] = []
    normalized_headings: list[dict[str, Any]] = []
    normalized_paragraph_styles: list[dict[str, Any]] = []
    split_mixed_inline: list[dict[str, Any]] = []
    existing_block_ids = {
        block["block_id"]
        for block in _walk_blocks(normalized["blocks"])
        if block.get("block_id") is not None
    }
    generated_block_index = 0

    def next_block_id() -> str:
        nonlocal generated_block_index
        while True:
            generated_block_index += 1
            candidate = f"delivery-block-{generated_block_index:06d}"
            if candidate not in existing_block_ids:
                existing_block_ids.add(candidate)
                return candidate

    def normalize_heading(block: dict[str, Any]) -> None:
        if block.get("kind") != "heading":
            return
        block_id = block.get("block_id")
        if block_id == title_block_id:
            return
        previous_level = block.get("level")
        previous_style = block.get("style_name")
        block["level"] = 2
        block["style_name"] = "Heading 2"
        if previous_level != 2 or previous_style != "Heading 2":
            normalized_headings.append(
                {
                    "block_id": block_id,
                    "from_level": previous_level,
                    "to_level": 2,
                }
            )

    def normalize_leaf(block: Mapping[str, Any]) -> list[dict[str, Any]]:
        source = dict(block)
        source_block_id = source.get("block_id")
        children: list[dict[str, Any]] = []
        for child_index, raw_child in enumerate(source.get("children", [])):
            child = dict(raw_child)
            if child.get("kind") == "text":
                marks = child.pop("marks", None)
                if marks:
                    removed_marks.append(
                        {
                            "block_id": source_block_id,
                            "child_index": child_index,
                            "marks": list(marks),
                        }
                    )
                if child.pop("link", None) is not None:
                    removed_links.append(
                        {
                            "block_id": source_block_id,
                            "child_index": child_index,
                        }
                    )
            children.append(child)

        source["children"] = children
        normalize_heading(source)
        if source.get("kind") == "paragraph" and source.get("style_name") not in {
            None,
            "Normal",
        }:
            normalized_paragraph_styles.append(
                {
                    "block_id": source_block_id,
                    "from_style": source.get("style_name"),
                    "to_style": "Normal",
                }
            )
            source["style_name"] = "Normal"
        has_text = any(child.get("kind") == "text" for child in children)
        has_image = any(child.get("kind") == "image" for child in children)
        if not (has_text and has_image):
            return [source]

        segments: list[tuple[str, list[dict[str, Any]]]] = []
        text_children: list[dict[str, Any]] = []
        for child in children:
            if child.get("kind") == "text":
                text_children.append(child)
                continue
            if text_children:
                segments.append(("text", text_children))
                text_children = []
            # 每个图片位置独立成块；相同 asset_id 的重复出现也不会被合并。
            segments.append(("image", [child]))
        if text_children:
            segments.append(("text", text_children))

        original_id_index = 0
        if source_block_id == title_block_id:
            original_id_index = next(
                (index for index, (kind, _children) in enumerate(segments) if kind == "text"),
                0,
            )
        result: list[dict[str, Any]] = []
        result_ids: list[str] = []
        for index, (segment_kind, segment_children) in enumerate(segments):
            if source_block_id is not None and index == original_id_index:
                block_id = source_block_id
            else:
                block_id = next_block_id()
            result_ids.append(block_id)
            if segment_kind == "image":
                result.append(
                    {
                        "kind": "paragraph",
                        "block_id": block_id,
                        "children": segment_children,
                    }
                )
                continue
            text_block = {
                key: value
                for key, value in source.items()
                if key not in {"block_id", "children"}
            }
            text_block["block_id"] = block_id
            text_block["children"] = segment_children
            result.append(text_block)

        split_mixed_inline.append(
            {
                "block_id": source_block_id,
                "result_block_ids": result_ids,
            }
        )
        return result

    def normalize_blocks(blocks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw_block in blocks:
            kind = raw_block.get("kind")
            if kind in {"paragraph", "heading"}:
                result.extend(normalize_leaf(raw_block))
                continue
            block = dict(raw_block)
            if kind == "list":
                block["items"] = [
                    {
                        **item,
                        "blocks": normalize_blocks(item.get("blocks", [])),
                    }
                    for item in raw_block.get("items", [])
                ]
            elif kind == "table":
                block["rows"] = [
                    {
                        **row,
                        "cells": [
                            {
                                **cell,
                                "blocks": normalize_blocks(cell.get("blocks", [])),
                            }
                            for cell in row.get("cells", [])
                        ],
                    }
                    for row in raw_block.get("rows", [])
                ]
            result.append(block)
        return result

    normalized["blocks"] = normalize_blocks(normalized["blocks"])
    delivery_document = validate_document(normalized)
    return delivery_document, {
        "policy_version": DELIVERY_POLICY_VERSION,
        "removed_marks": removed_marks,
        "removed_links": removed_links,
        "normalized_headings": normalized_headings,
        "normalized_paragraph_styles": normalized_paragraph_styles,
        "split_mixed_inline": split_mixed_inline,
    }


def project_to_delivery_blocks(
    document: Mapping[str, Any], *, omit_title_block: bool = True
) -> list[dict[str, Any]]:
    """把 v2 canonical 文档投影为带标题层级的安全平台块。

    与历史 ``project_to_v1`` 不同，此投影保留 ``heading.level``。它只接受
    平台执行层已经能表达的简单段落/标题/图片序列；marks、link、图注、浮动
    锚点、列表和表格必须先经过格式能力门禁，不能在这里静默降级成普通文本。
    输出只含公开文本、标题层级和受控 ``asset_id``，绝不包含本机路径。
    """

    normalized = validate_document(document)
    title_block_id = normalized.get("title_block_id")
    projected: list[dict[str, Any]] = []

    def flush_text(
        text_parts: list[str],
        *,
        block_type: str,
        level: int | None,
    ) -> None:
        text = "".join(text_parts)
        if not text:
            return
        item: dict[str, Any] = {
            "type": block_type,
            "text": text,
            "position": len(projected),
        }
        if level is not None:
            item["level"] = level
        projected.append(item)
        text_parts.clear()

    def visit(blocks: Iterable[Mapping[str, Any]]) -> None:
        for block in blocks:
            block_id = block.get("block_id")
            if omit_title_block and title_block_id is not None and block_id == title_block_id:
                continue
            kind = block["kind"]
            if kind not in {"paragraph", "heading"}:
                raise ContentDocumentValidationError(
                    f"平台投影不支持 {kind}；必须先通过格式能力门禁"
                )
            level = block.get("level") if kind == "heading" else None
            style_name = block.get("style_name")
            if kind == "heading":
                # python-docx/Word 的 Heading 2/3 样式是标题层级的来源
                # 证据，不是额外的段落样式能力。自动视觉标题归一化前的旧
                # 冻结版本可能仍携带 neutral ``Normal`` 来源样式；canonical
                # heading level 已单独通过格式能力门，因此允许这一兼容值。
                # 其他自定义或错配 Heading 样式继续 fail-closed。
                expected_style = f"Heading {level}"
                if style_name not in (None, "Normal", expected_style):
                    raise ContentDocumentValidationError(
                        "平台投影不支持与标题层级不匹配的段落样式；必须先通过格式能力门禁"
                    )
            elif style_name not in (None, "Normal"):
                raise ContentDocumentValidationError(
                    "平台投影不支持段落样式；必须先通过格式能力门禁"
                )
            text_parts: list[str] = []
            for child in block["children"]:
                if child["kind"] == "text":
                    if child.get("marks") or child.get("link") is not None:
                        raise ContentDocumentValidationError(
                            "平台投影不支持 marks/link；必须先通过格式能力门禁"
                        )
                    text_parts.append(child["text"])
                    continue

                if child.get("caption") is not None:
                    raise ContentDocumentValidationError(
                        "平台投影不支持图片 caption；必须先通过格式能力门禁"
                    )
                anchor = child.get("anchor")
                if anchor is not None and anchor.get("kind") != "inline":
                    raise ContentDocumentValidationError(
                        "平台投影不支持浮动图片锚点；必须先通过格式能力门禁"
                    )
                flush_text(
                    text_parts,
                    block_type=kind if kind == "heading" else "text",
                    level=level,
                )
                image = {
                    "type": "image",
                    "asset_id": child["asset_id"],
                    "position": len(projected),
                }
                if child.get("alt") is not None:
                    image["alt"] = child["alt"]
                projected.append(image)
            flush_text(
                text_parts,
                block_type=kind if kind == "heading" else "text",
                level=level,
            )

    visit(normalized["blocks"])
    return projected


required_delivery_features = delivery_features


def compatibility(
    document: Mapping[str, Any], capabilities: PlatformCapabilities
) -> CompatibilityResult:
    required = required_features(document)
    missing = required - capabilities.supported
    # exact=False 仍报告缺失项；它只由上层决定是否允许显式降级，默认绝不放行。
    return CompatibilityResult(
        required=required,
        supported=capabilities.supported,
        incompatibilities=frozenset(missing),
        exact=capabilities.exact,
    )


def compute_incompatibilities(
    document: Mapping[str, Any], capabilities: PlatformCapabilities | Iterable[str]
) -> frozenset[str]:
    """计算缺失能力；默认结果非空即应 fail-closed。"""

    if not isinstance(capabilities, PlatformCapabilities):
        capabilities = PlatformCapabilities("anonymous", capabilities)
    return compatibility(document, capabilities).incompatibilities


check_compatibility = compatibility
required_capabilities = required_features


__all__ = [
    "Block",
    "CompatibilityResult",
    "ContentDocument",
    "ContentDocumentError",
    "ContentDocumentValidationError",
    "DELIVERY_POLICY_VERSION",
    "DocumentAnchor",
    "DocumentLink",
    "FEATURE_KEYS",
    "HeadingBlock",
    "ImageNode",
    "LEGACY_PROJECTED",
    "ListBlock",
    "ListItem",
    "MAX_BLOCKS",
    "MAX_NESTING_DEPTH",
    "MAX_TITLE_LENGTH",
    "MAX_TEXT_LENGTH",
    "NATIVE",
    "ParagraphBlock",
    "PlatformCapabilities",
    "SCHEMA_VERSION",
    "TableBlock",
    "TableCell",
    "TableRow",
    "TextNode",
    "V1Projection",
    "canonical_document_json",
    "canonical_json",
    "check_compatibility",
    "compatibility",
    "compute_incompatibilities",
    "content_hash",
    "delivery_heading_levels",
    "document_features",
    "delivery_features",
    "document_hash",
    "hash_document",
    "normalize_for_delivery",
    "project_to_v1",
    "project_to_delivery_blocks",
    "required_capabilities",
    "required_features",
    "required_delivery_features",
    "upgrade_v1",
    "validate_document",
]
