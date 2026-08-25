"""DOCX v2 富结构导入的离线回归测试。

测试只在临时目录生成和读取 DOCX；不接触真实账号、浏览器 Profile 或平台接口。
"""

import asyncio
import io
import json
from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

from content_studio.assets import AssetStore
from content_studio.content_document import (
    delivery_features,
    delivery_heading_levels,
    normalize_for_delivery,
    project_to_delivery_blocks,
)
from content_studio.errors import ContentAssetError
from content_studio.importers import DocxImportAdapter
from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
)
from core.docx_parser import DocxParser


def _image_path(tmp_path: Path, name: str = "source.png") -> Path:
    path = tmp_path / name
    Image.new("RGB", (32, 24), "#2d6cdf").save(path, format="PNG")
    return path


def _add_hyperlink(paragraph, text: str, target: str) -> None:
    relationship_id = paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    bold = OxmlElement("w:b")
    run_properties.append(bold)
    run.append(run_properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _make_rich_doc(tmp_path: Path, *, unsafe_link: str | None = None) -> bytes:
    document = Document()
    document.core_properties.title = "核心标题"

    title = document.add_paragraph("核心标题")
    title.runs[0].bold = True

    marked = document.add_paragraph()
    marked.add_run("粗体").bold = True
    marked.add_run("斜体").italic = True
    marked.add_run("下划线").underline = True
    marked.add_run("删除线").font.strike = True
    _add_hyperlink(marked, "安全链接", unsafe_link or "https://example.test/reference")

    mixed = document.add_paragraph()
    mixed.add_run("图片前")
    image_run = mixed.add_run()
    image_run.add_picture(str(_image_path(tmp_path)), width=Inches(0.3))
    doc_pr = image_run._r.xpath(".//wp:docPr")[0]
    doc_pr.set("descr", "正文示意图")
    mixed.add_run("图片后")

    captioned = document.add_paragraph()
    captioned_image_run = captioned.add_run()
    captioned_image_run.add_picture(str(_image_path(tmp_path, "captioned.png")), width=Inches(0.25))
    captioned_doc_pr = captioned_image_run._r.xpath(".//wp:docPr")[0]
    captioned_doc_pr.set("descr", "带说明图片")
    document.add_paragraph("正文示意图说明", style="Caption")

    heading = document.add_heading("二级章节", level=2)
    heading.runs[0].font.italic = True

    document.add_paragraph("第一项", style="List Number")
    document.add_paragraph("第二项", style="List Number")

    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).paragraphs[0].add_run("单元格文字")
    table_image_run = table.cell(0, 1).paragraphs[0].add_run()
    table_image_run.add_picture(str(_image_path(tmp_path, "table.png")), width=Inches(0.2))

    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _make_floating_image_doc(tmp_path: Path) -> bytes:
    document = Document()
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    run.add_picture(str(_image_path(tmp_path)))
    inline = run._r.xpath(".//wp:inline")[0]
    inline.tag = qn("wp:anchor")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _make_visual_heading_doc(
    tmp_path: Path,
    *,
    body_points: float,
    heading_points: float,
    include_nonstructural_marks: bool = True,
) -> bytes:
    document = Document()

    title = document.add_paragraph()
    title_run = title.add_run("视觉主标题")
    title_run.bold = True
    title_run.font.size = Pt(20)

    body = document.add_paragraph()
    body_run = body.add_run("第一段普通正文")
    body_run.font.size = Pt(body_points)

    image_paragraph = document.add_paragraph()
    image_paragraph.add_run().add_picture(
        str(_image_path(tmp_path, "visual-heading.png")),
        width=Inches(0.3),
    )

    visual_heading = document.add_paragraph()
    visual_heading_run = visual_heading.add_run("视觉二级标题")
    visual_heading_run.bold = True
    visual_heading_run.font.size = Pt(heading_points)

    second_body = document.add_paragraph()
    second_body_run = second_body.add_run("第二段普通正文")
    second_body_run.font.size = Pt(body_points)

    second_image_paragraph = document.add_paragraph()
    second_image_paragraph.add_run().add_picture(
        str(_image_path(tmp_path, "visual-heading-second.png")),
        width=Inches(0.3),
    )

    if include_nonstructural_marks:
        callout = document.add_paragraph()
        callout_run = callout.add_run("同字号粗体提示不能冒充标题")
        callout_run.bold = True
        callout_run.font.size = Pt(body_points)

        mixed = document.add_paragraph()
        mixed_bold = mixed.add_run("局部粗体")
        mixed_bold.bold = True
        mixed_bold.font.size = Pt(heading_points)
        mixed_plain = mixed.add_run("仍然是正文")
        mixed_plain.font.size = Pt(heading_points)

    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _run(coro):
    return asyncio.run(coro)


def test_parser_v2_preserves_rich_order_marks_anchor_and_caption(tmp_path: Path) -> None:
    source = tmp_path / "rich.docx"
    source.write_bytes(_make_rich_doc(tmp_path))
    parsed = DocxParser(images_dir=str(tmp_path / "parser-images")).parse_document_v2(str(source))
    document = parsed.document_v2

    assert document["schema_version"] == 2
    assert document["title"] == "核心标题"
    assert document["title_block_id"] == document["blocks"][0]["block_id"]

    marked_children = document["blocks"][1]["children"]
    assert marked_children[0]["marks"] == ["bold"]
    assert marked_children[1]["marks"] == ["italic"]
    assert marked_children[2]["marks"] == ["underline"]
    assert marked_children[3]["marks"] == ["strike"]
    assert marked_children[4]["link"]["href"] == "https://example.test/reference"

    mixed_children = document["blocks"][2]["children"]
    assert [child["kind"] for child in mixed_children] == ["text", "image", "text"]
    assert mixed_children[1]["alt"] == "正文示意图"
    captioned_image = document["blocks"][3]["children"][0]
    assert captioned_image["alt"] == "带说明图片"
    assert captioned_image["caption"] == "正文示意图说明"

    assert any(block["kind"] == "heading" and block["level"] == 2 for block in document["blocks"])
    list_block = next(block for block in document["blocks"] if block["kind"] == "list")
    assert list_block["ordered"] is True
    assert len(list_block["items"]) == 2
    table_block = next(block for block in document["blocks"] if block["kind"] == "table")
    assert table_block["rows"][0]["cells"][1]["blocks"][0]["children"][0]["kind"] == "image"


def test_visible_h1_overrides_stale_core_title_and_is_omitted_from_delivery(
    tmp_path: Path,
) -> None:
    document = Document()
    document.core_properties.title = "PS5卧室外接显示器，别只盯HDMI 2.1"
    visible_title = "PS5卧室外接显示器，别只盯着HDMI 2.1（高速接口标准）"
    document.add_heading(visible_title, level=1)
    document.add_paragraph("这是用于确定正文结构并验证标题不会重复进入正文的引导段。")
    document.add_heading("27英寸更适合卧室桌面", level=2)
    document.add_paragraph("这是二级标题之后的正文内容。")
    source = tmp_path / "stale-core-title.docx"
    document.save(source)

    parsed = DocxParser(
        images_dir=str(tmp_path / "parser-images")
    ).parse_document_v2(str(source))
    rich_document = parsed.document_v2

    assert rich_document["title"] == visible_title
    assert rich_document["title_block_id"] == rich_document["blocks"][0]["block_id"]

    delivery_document, _loss_report = normalize_for_delivery(rich_document)
    delivery_blocks = project_to_delivery_blocks(delivery_document)
    assert visible_title not in {
        block.get("text") for block in delivery_blocks if block.get("text")
    }
    assert delivery_heading_levels(delivery_document) == frozenset({2})


def test_parser_inherits_base_style_and_preserves_source_heading_one_semantics(
    tmp_path: Path,
) -> None:
    document = Document()
    document.styles["Normal"].font.size = Pt(20)
    body_base = document.styles.add_style("Article Body Base", WD_STYLE_TYPE.PARAGRAPH)
    body_base.font.size = Pt(11)
    body_style = document.styles.add_style("Article Body", WD_STYLE_TYPE.PARAGRAPH)
    body_style.base_style = body_base

    document.add_heading("唯一主标题", level=1)
    document.add_paragraph("用于确定正文基准字号的普通正文。", style=body_style)
    for index in range(1, 6):
        document.add_heading(f"错误一级标题{index}", level=1)
        document.add_paragraph(f"第{index}节正文。", style=body_style)

    visual_heading = document.add_paragraph(style=body_style)
    visual_heading_run = visual_heading.add_run("继承样式后的视觉二级标题")
    visual_heading_run.font.size = Pt(14)

    mixed = document.add_paragraph(style=body_style)
    mixed.add_run("图片前")
    mixed.add_run().add_picture(
        str(_image_path(tmp_path, "base-style-mixed.png")),
        width=Inches(0.3),
    )
    mixed.add_run("图片后")
    source = tmp_path / "base-style-heading-one.docx"
    document.save(source)

    parsed = DocxParser(
        images_dir=str(tmp_path / "parser-images")
    ).parse_document_v2(str(source))
    blocks = parsed.document_v2["blocks"]
    blocks_by_text = {
        "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if child.get("kind") == "text"
        ): block
        for block in blocks
        if block.get("kind") in {"paragraph", "heading"}
    }

    assert parsed.document_v2["title"] == "唯一主标题"
    assert parsed.document_v2["title_block_id"] == blocks_by_text["唯一主标题"][
        "block_id"
    ]
    source_h1_blocks = [
        block
        for block in blocks
        if block.get("kind") == "heading" and block.get("level") == 1
    ]
    assert source_h1_blocks == [
        blocks_by_text["唯一主标题"],
        *(blocks_by_text[f"错误一级标题{index}"] for index in range(1, 6)),
    ]
    for index in range(1, 6):
        section = blocks_by_text[f"错误一级标题{index}"]
        assert section["level"] == 1
        assert section["style_name"] == "Heading 1"
    assert blocks_by_text["继承样式后的视觉二级标题"]["level"] == 2
    assert [child["kind"] for child in blocks_by_text["图片前图片后"]["children"]] == [
        "text",
        "image",
        "text",
    ]


def test_parser_v2_marks_floating_image(tmp_path: Path) -> None:
    source = tmp_path / "floating.docx"
    source.write_bytes(_make_floating_image_doc(tmp_path))
    parsed = DocxParser(images_dir=str(tmp_path / "parser-images")).parse_document_v2(str(source))
    image = parsed.document_v2["blocks"][0]["children"][0]
    assert image["kind"] == "image"
    assert image["anchor"] == {"kind": "floating"}


def test_parser_v2_rejects_formula_instead_of_silently_dropping_it(
    tmp_path: Path,
) -> None:
    document = Document()
    paragraph = document.add_paragraph("公式前")
    paragraph._p.append(OxmlElement("m:oMath"))
    source = tmp_path / "formula.docx"
    document.save(source)

    with pytest.raises(ValueError, match="公式无法安全映射"):
        DocxParser(
            images_dir=str(tmp_path / "parser-images")
        ).parse_document_v2(str(source))


@pytest.mark.parametrize("container", ["body", "table-cell"])
def test_parser_v2_rejects_direct_math_paragraphs(
    tmp_path: Path,
    container: str,
) -> None:
    document = Document()
    formula = OxmlElement("m:oMathPara")
    if container == "body":
        document.element.body.append(formula)
    else:
        document.add_table(rows=1, cols=1).cell(0, 0)._tc.append(formula)
    source = tmp_path / f"formula-{container}.docx"
    document.save(source)

    with pytest.raises(ValueError, match="公式无法安全映射"):
        DocxParser(
            images_dir=str(tmp_path / "parser-images")
        ).parse_document_v2(str(source))


@pytest.mark.parametrize(
    ("body_points", "heading_points"),
    [(11, 14), (12, 15.5)],
)
def test_parser_v2_promotes_only_strong_visual_heading_evidence(
    tmp_path: Path,
    body_points: float,
    heading_points: float,
) -> None:
    source = tmp_path / "visual-heading.docx"
    source.write_bytes(
        _make_visual_heading_doc(
            tmp_path,
            body_points=body_points,
            heading_points=heading_points,
        )
    )

    parsed = DocxParser(
        images_dir=str(tmp_path / "parser-images")
    ).parse_document_v2(str(source))
    blocks_by_text = {
        "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if child.get("kind") == "text"
        ): block
        for block in parsed.document_v2["blocks"]
        if block.get("kind") in {"paragraph", "heading"}
    }

    title = blocks_by_text["视觉主标题"]
    assert title["kind"] == "heading"
    assert title["level"] == 1
    assert title["children"][0]["marks"] == ["bold"]

    heading = blocks_by_text["视觉二级标题"]
    assert heading["kind"] == "heading"
    assert heading["level"] == 2
    assert "style_name" not in heading
    assert heading["children"][0]["marks"] == ["bold"]

    callout = blocks_by_text["同字号粗体提示不能冒充标题"]
    assert callout["kind"] == "heading"
    assert callout["level"] == 2
    assert callout["children"][0]["marks"] == ["bold"]

    mixed = blocks_by_text["局部粗体仍然是正文"]
    assert mixed["kind"] == "heading"
    assert mixed["level"] == 2
    assert mixed["children"][0]["marks"] == ["bold"]
    assert "marks" not in mixed["children"][1]


@pytest.mark.parametrize(
    ("bold_text", "plain_text", "expected_kind"),
    [
        ("粗体七字符甲乙", "普通三", "heading"),
        ("粗体六字符甲", "普通四字", "paragraph"),
    ],
)
def test_visual_heading_uses_character_weighted_seventy_percent_threshold(
    tmp_path: Path,
    bold_text: str,
    plain_text: str,
    expected_kind: str,
) -> None:
    document = Document()
    title = document.add_paragraph()
    title_run = title.add_run("视觉主标题")
    title_run.bold = True
    title_run.font.size = Pt(20)
    body = document.add_paragraph("这是一段用于确定正文基准字号的普通正文内容。")
    body.runs[0].font.size = Pt(11)
    candidate = document.add_paragraph()
    bold_run = candidate.add_run(bold_text)
    bold_run.bold = True
    bold_run.font.size = Pt(11)
    plain_run = candidate.add_run(plain_text)
    plain_run.font.size = Pt(11)
    output = tmp_path / "bold-ratio.docx"
    document.save(output)

    parsed = DocxParser(images_dir=str(tmp_path / "parser-images")).parse_document_v2(
        str(output)
    )
    candidate_block = next(
        block
        for block in parsed.document_v2["blocks"]
        if "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if child.get("kind") == "text"
        )
        == bold_text + plain_text
    )

    assert candidate_block["kind"] == expected_kind
    if expected_kind == "heading":
        assert candidate_block["level"] == 2


def test_visual_color_is_auxiliary_and_does_not_promote_plain_body(
    tmp_path: Path,
) -> None:
    document = Document()
    title = document.add_paragraph()
    title_run = title.add_run("视觉主标题")
    title_run.bold = True
    title_run.font.size = Pt(20)
    body = document.add_paragraph("这是一段用于确定正文基准字号的普通正文内容。")
    body.runs[0].font.size = Pt(11)
    colored = document.add_paragraph("只有颜色变化的普通提示")
    colored.runs[0].font.size = Pt(11)
    colored.runs[0].font.color.rgb = RGBColor(0x22, 0x66, 0xAA)
    output = tmp_path / "colored-body.docx"
    document.save(output)

    parsed = DocxParser(images_dir=str(tmp_path / "parser-images")).parse_document_v2(
        str(output)
    )
    colored_block = next(
        block
        for block in parsed.document_v2["blocks"]
        if "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if child.get("kind") == "text"
        )
        == "只有颜色变化的普通提示"
    )

    assert colored_block["kind"] == "paragraph"


def test_visual_heading_normalization_unblocks_verified_zol_contract(
    tmp_path: Path,
) -> None:
    data = _make_visual_heading_doc(
        tmp_path,
        body_points=11,
        heading_points=14,
        include_nonstructural_marks=False,
    )
    adapter = DocxImportAdapter(
        AssetStore(tmp_path / "assets"),
        work_root=tmp_path / "work",
    )

    _title, _blocks, assets, document = _run(
        adapter.parse_v2(data, "visual-heading.docx")
    )
    declaration = DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("zol")
    delivery_document, report = normalize_for_delivery(document)

    assert len(assets) == 2
    assert "marks" in delivery_features(document)
    assert report["removed_marks"]
    assert delivery_features(delivery_document) == frozenset({"heading", "image_order"})
    assert delivery_heading_levels(delivery_document) == frozenset({2})
    assert delivery_features(delivery_document) - declaration.supported == set()
    assert delivery_heading_levels(delivery_document) - declaration.heading_levels == set()
    projected = project_to_delivery_blocks(delivery_document)
    assert [block["type"] for block in projected] == [
        "text",
        "image",
        "heading",
        "text",
        "image",
    ]
    assert projected[2] == {
        "type": "heading",
        "text": "视觉二级标题",
        "position": 2,
        "level": 2,
    }


def test_importer_resolves_assets_before_validation_and_projects_order(tmp_path: Path) -> None:
    data = _make_rich_doc(tmp_path)
    adapter = DocxImportAdapter(
        AssetStore(tmp_path / "assets"),
        work_root=tmp_path / "work",
    )
    title, blocks, assets, document = _run(adapter.parse_v2(data, "rich.docx"))

    assert title == "核心标题"
    assert len(assets) == 3
    assert document["source_fidelity"] == "NATIVE"
    assert all(len(asset.asset_id) == 36 for asset in assets)
    assert all(
        "asset_id" in child
        for block in document["blocks"]
        if block.get("children")
        for child in block["children"]
        if child["kind"] == "image"
    )
    public_json = json.dumps(document, ensure_ascii=False)
    assert "__source_path" not in public_json
    assert "storage_path" not in public_json
    assert [block["position"] for block in blocks] == list(range(len(blocks)))
    assert [block["type"] for block in blocks[1:4]] == ["text", "image", "text"]
    assert blocks[0]["text"] == "粗体斜体下划线删除线安全链接"


def test_importer_rejects_unsafe_hyperlink_and_removes_assets(tmp_path: Path) -> None:
    data = _make_rich_doc(tmp_path, unsafe_link="javascript:alert(1)")
    asset_root = tmp_path / "assets"
    adapter = DocxImportAdapter(AssetStore(asset_root), work_root=tmp_path / "work")

    with pytest.raises(ContentAssetError, match="安全|解析"):
        _run(adapter.parse_v2(data, "unsafe.docx"))
    assert not list(asset_root.rglob("*"))


def test_importer_keeps_unassociated_caption_paragraph(tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("Caption 文本", style="Caption")
    source = io.BytesIO()
    document.save(source)
    adapter = DocxImportAdapter(AssetStore(tmp_path / "assets"), work_root=tmp_path / "work")

    _title, _blocks, _assets, rich = _run(adapter.parse_v2(source.getvalue(), "caption.docx"))
    caption_blocks = [block for block in rich["blocks"] if block.get("style_name") == "Caption"]
    assert len(caption_blocks) == 1
