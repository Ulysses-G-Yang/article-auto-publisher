""".docx 文档解析器 —— 提取文本 + 图片，保持顺序和位置关系"""
import os
import io
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from PIL import Image

from config import get_config


@dataclass
class ContentBlock:
    """内容块：文本或图片"""
    type: str  # "text" | "image" | "heading"
    text: Optional[str] = None
    style_name: Optional[str] = None
    image_filename: Optional[str] = None
    image_path: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    position: int = 0


@dataclass
class ParsedArticle:
    """解析后的文章"""
    filename: str
    original_path: str
    blocks: List[ContentBlock] = field(default_factory=list)
    plain_text: str = ""
    image_count: int = 0
    char_count: int = 0


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

                # 检查段落中的图片
                has_image = False
                for run in para.runs:
                    drawings = run._element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing")
                    blips = run._element.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")

                    for blip in blips:
                        embed = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                        if embed and embed in image_parts:
                            image_part = image_parts[embed]
                            img_filename = os.path.basename(image_part.partname)
                            img_path = os.path.join(article_images_dir, img_filename)

                            if not os.path.exists(img_path):
                                with open(img_path, "wb") as f:
                                    f.write(image_part.blob)

                            width, height = self._get_image_size(img_path)
                            article.image_count += 1

                            block = ContentBlock(
                                type="image",
                                image_filename=img_filename,
                                image_path=img_path,
                                image_width=width,
                                image_height=height,
                                position=position,
                            )
                            article.blocks.append(block)
                            position += 1
                            has_image = True

                # 处理段落中的图片（另一种嵌入方式：内联形状）
                if not has_image:
                    inline_shapes = para._element.findall(
                        ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing"
                    )
                    for drawing in inline_shapes:
                        blips = drawing.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
                        for blip in blips:
                            embed = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                            if embed and embed in image_parts:
                                image_part = image_parts[embed]
                                img_filename = os.path.basename(image_part.partname)
                                img_path = os.path.join(article_images_dir, img_filename)

                                if not os.path.exists(img_path):
                                    with open(img_path, "wb") as f:
                                        f.write(image_part.blob)

                                width, height = self._get_image_size(img_path)
                                article.image_count += 1

                                block = ContentBlock(
                                    type="image",
                                    image_filename=img_filename,
                                    image_path=img_path,
                                    image_width=width,
                                    image_height=height,
                                    position=position,
                                )
                                article.blocks.append(block)
                                position += 1
                                has_image = True

                # 如果没有图片，处理为文本块
                if not has_image:
                    text = para.text.strip()
                    if text:
                        # 判断是否为标题
                        is_heading = para.style and para.style.name and ("Heading" in para.style.name or "Title" in para.style.name)
                        block_type = "heading" if is_heading else "text"
                        block = ContentBlock(
                            type=block_type,
                            text=text,
                            style_name=para.style.name if para.style else None,
                            position=position,
                        )
                        article.blocks.append(block)
                        text_parts.append(text)
                        article.char_count += len(text)
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

    def _extract_table_text(self, tbl_element) -> str:
        """提取表格文本"""
        rows = tbl_element.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tr")
        lines = []
        for row in rows:
            cells = row.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tc")
            cell_texts = []
            for cell in cells:
                paras = cell.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
                cell_text = " ".join(
                    "".join(t.text or "" for t in p.iterfind(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
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
        """从文章中提取可能的标题（第一个标题块或第一段文本的前30字）"""
        for block in article.blocks:
            if block.type == "heading" and block.text:
                return block.text[:50]

        # 取第一段非空文本
        for block in article.blocks:
            if block.type == "text" and block.text:
                return block.text[:30] + ("..." if len(block.text) > 30 else "")

        return article.filename.replace(".docx", "")
