""".docx 文档解析器 —— 提取文本 + 图片，保持顺序和位置关系"""

import json
import os
import re
from dataclasses import dataclass, field

from docx import Document
from PIL import Image

from config import get_config

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_TEXT = f"{{{W_NS}}}t"
W_TAB = f"{{{W_NS}}}tab"
W_BREAK = f"{{{W_NS}}}br"
W_CARRIAGE_RETURN = f"{{{W_NS}}}cr"
A_BLIP = f"{{{A_NS}}}blip"


@dataclass
class ContentBlock:
    """内容块：文本或图片"""

    type: str  # "text" | "image" | "heading"
    text: str | None = None
    style_name: str | None = None
    image_filename: str | None = None
    image_path: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    position: int = 0


@dataclass
class ParsedArticle:
    """解析后的文章"""

    filename: str
    original_path: str
    blocks: list[ContentBlock] = field(default_factory=list)
    plain_text: str = ""
    image_count: int = 0
    char_count: int = 0
    document_title: str | None = None


class DocxParser:
    def __init__(self, images_dir: str | None = None):
        cfg = get_config()
        self.images_dir = images_dir or cfg["paths"]["images"]
        os.makedirs(self.images_dir, exist_ok=True)

    def parse(self, filepath: str) -> ParsedArticle:
        """解析 docx 文件，返回结构化文章"""
        filename = os.path.basename(filepath)
        doc = Document(filepath)

        article = ParsedArticle(
            filename=filename,
            original_path=filepath,
            document_title=self._normalize_title(doc.core_properties.title) or None,
        )

        # 创建文章专属图片目录
        article_images_dir = os.path.join(self.images_dir, os.path.splitext(filename)[0])
        os.makedirs(article_images_dir, exist_ok=True)

        # 遍历 document body 的所有子元素，保持原始顺序
        body = doc.element.body
        position = 0
        text_parts = []

        # 建立图片关系映射: rId -> image_part
        image_parts = {}
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                image_parts[rel.rId] = rel.target_part

        for child in body:
            # 处理段落
            if child.tag.endswith("}p"):
                para = self._find_paragraph(doc, child)
                if para is None:
                    continue

                # Walk the run XML in document order.  Text runs are accumulated
                # until an inline image boundary, so spaces at run boundaries are
                # preserved and adjacent runs become one text block.
                paragraph_parts = self._paragraph_parts(para, image_parts)

                # 部分 Word 生成器不会把纯文字暴露为标准 Run，保留段落级兜底。
                if not paragraph_parts and para.text.strip():
                    paragraph_parts.append(("text", para.text))

                style_name = para.style.name if para.style else None
                is_heading = self._is_heading_style(style_name)
                for part_type, value in paragraph_parts:
                    if part_type == "text":
                        block = ContentBlock(
                            type="heading" if is_heading else "text",
                            text=value,
                            style_name=para.style.name if para.style else None,
                            position=position,
                        )
                        article.blocks.append(block)
                        text_parts.append(value)
                        article.char_count += len(value)
                        position += 1
                        continue

                    image_part = value
                    img_filename = os.path.basename(image_part.partname)
                    img_path = os.path.join(article_images_dir, img_filename)
                    if not os.path.exists(img_path):
                        with open(img_path, "wb") as f:
                            f.write(image_part.blob)
                    width, height = self._get_image_size(img_path)
                    article.blocks.append(
                        ContentBlock(
                            type="image",
                            image_filename=img_filename,
                            image_path=img_path,
                            image_width=width,
                            image_height=height,
                            position=position,
                        )
                    )
                    article.image_count += 1
                    position += 1

            # 处理表格
            elif child.tag.endswith("}tbl"):
                table_text = self._extract_table_text(child)
                if table_text.strip():
                    block = ContentBlock(
                        type="text",
                        text=table_text.strip(),
                        position=position,
                    )
                    article.blocks.append(block)
                    text_parts.append(table_text.strip())
                    article.char_count += len(table_text.strip())
                    position += 1

        article.plain_text = "\n".join(text_parts)
        return article

    def _find_paragraph(self, doc, element):
        """根据 XML 元素查找对应的 python-docx Paragraph 对象"""
        for para in doc.paragraphs:
            if para._element is element:
                return para
        return None

    def _paragraph_parts(self, para, image_parts):
        """Return text/image parts while preserving paragraph XML order."""
        parts = []
        text_buffer = []

        def flush_text() -> None:
            if not text_buffer:
                return
            text = "".join(text_buffer)
            text_buffer.clear()
            if text.strip():
                parts.append(("text", text))

        for run in para.runs:
            for node in run._element.iter():
                if node.tag == W_TEXT:
                    text_buffer.append(node.text or "")
                elif node.tag == W_TAB:
                    text_buffer.append("\t")
                elif node.tag in {W_BREAK, W_CARRIAGE_RETURN}:
                    text_buffer.append("\n")
                elif node.tag == A_BLIP:
                    embed = node.get(f"{{{R_NS}}}embed")
                    image_part = image_parts.get(embed)
                    if image_part is not None:
                        flush_text()
                        parts.append(("image", image_part))

        flush_text()
        return parts

    @staticmethod
    def _normalize_title(value: str | None) -> str:
        """Normalize title whitespace without changing non-whitespace text."""
        return " ".join(str(value or "").split())

    @staticmethod
    def _is_heading_style(style_name: str | None) -> bool:
        normalized = " ".join(str(style_name or "").split()).casefold()
        return normalized == "title" or bool(re.fullmatch(r"heading\s*[1-9]", normalized))

    @staticmethod
    def _is_document_title_style(style_name: str | None) -> bool:
        normalized = " ".join(str(style_name or "").split()).casefold()
        return normalized == "title" or bool(re.fullmatch(r"heading\s*1", normalized))

    def _extract_table_text(self, tbl_element) -> str:
        """提取表格文本"""
        rows = tbl_element.findall(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tr"
        )
        lines = []
        for row in rows:
            cells = row.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tc")
            cell_texts = []
            for cell in cells:
                paras = cell.findall(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                )
                cell_text = " ".join(
                    "".join(
                        t.text or ""
                        for t in p.iterfind(
                            ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                        )
                    )
                    for p in paras
                )
                cell_texts.append(cell_text.strip())
            lines.append(" | ".join(cell_texts))
        return "\n".join(lines)

    def _get_image_size(self, img_path: str) -> tuple:
        """获取图片尺寸"""
        try:
            with Image.open(img_path) as img:
                return img.size
        except Exception:
            return (0, 0)

    def to_json(self, article: ParsedArticle) -> str:
        """将解析结果转为 JSON"""
        data = {
            "filename": article.filename,
            "original_path": article.original_path,
            "plain_text": article.plain_text,
            "char_count": article.char_count,
            "image_count": article.image_count,
            "blocks": [
                {
                    "type": b.type,
                    "text": b.text,
                    "style_name": b.style_name,
                    "image_filename": b.image_filename,
                    "image_path": b.image_path,
                    "image_width": b.image_width,
                    "image_height": b.image_height,
                    "position": b.position,
                }
                for b in article.blocks
            ],
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def get_title_from_article(self, article: ParsedArticle) -> str:
        """Extract the selected title using core properties/style/body priority."""
        core_title = article.document_title or ""
        if core_title:
            return core_title

        for block in article.blocks:
            if (
                block.type == "heading"
                and self._is_document_title_style(block.style_name)
                and block.text
            ):
                return self._normalize_title(block.text)

        # Fall back to the first non-empty body block, without truncating it.
        for block in article.blocks:
            if block.type == "text" and block.text and block.text.strip():
                return self._normalize_title(block.text)

        return os.path.splitext(article.filename)[0]
