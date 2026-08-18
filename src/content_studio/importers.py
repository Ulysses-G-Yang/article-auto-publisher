"""DOCX 与旧文章到统一图文块的适配层。

这里调用现有 ``DocxParser``，不复制其 Word XML 解析逻辑。旧数据库也只通过
公开查询方法读取；原文章、图片、任务和日志都不会被修改。
"""

import asyncio
import io
import json
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Protocol

from content_studio.assets import AssetStore, StoredAsset
from content_studio.content_document import (
    ContentDocumentValidationError,
    project_to_v1,
    validate_document,
)
from content_studio.errors import ContentAssetError, DraftNotFoundError
from content_studio.runtime_paths import default_work_root

MAX_DOCX_BYTES = 50 * 1024 * 1024
MAX_DOCX_ARCHIVE_ENTRIES = 10_000
MAX_DOCX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


class LegacySource(Protocol):
    async def list_articles(self, *, limit: int, offset: int) -> tuple[list[dict], int]: ...

    async def get_article(self, article_id: int) -> dict | None: ...

    async def get_article_images(self, article_id: int) -> list[dict]: ...


class LegacyDatabaseSource:
    """现役 sqlite3 Database 的只读投影。"""

    def __init__(
        self,
        database=None,
        *,
        database_path: str | Path | None = None,
        allowed_image_roots=None,
    ) -> None:
        self.database = database
        if database is None:
            if database_path is None:
                from config import get_config

                database_path = get_config()["paths"]["database"]
            self.database_path = Path(database_path).expanduser().resolve()
        else:
            self.database_path = None
        if allowed_image_roots is None:
            from config import get_config

            allowed_image_roots = (get_config()["paths"]["images"],)
        self.allowed_image_roots = tuple(
            Path(path).expanduser().resolve() for path in allowed_image_roots
        )

    async def list_articles(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        if self.database is not None:
            rows = await asyncio.to_thread(self.database.get_all_articles)
            total = len(rows)
            return rows[offset : offset + limit], total
        return await asyncio.to_thread(self._list_articles_read_only, limit, offset)

    async def get_article(self, article_id: int) -> dict | None:
        if self.database is not None:
            return await asyncio.to_thread(self.database.get_article, article_id)
        return await asyncio.to_thread(self._get_article_read_only, article_id)

    async def get_article_images(self, article_id: int) -> list[dict]:
        if self.database is not None:
            return await asyncio.to_thread(self.database.get_article_images, article_id)
        return await asyncio.to_thread(self._get_images_read_only, article_id)

    def _connect_read_only(self):
        if self.database_path is None or not self.database_path.is_file():
            raise DraftNotFoundError("旧文章数据库不存在")
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _list_articles_read_only(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self._connect_read_only() as connection:
            total = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM articles ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def _get_article_read_only(self, article_id: int) -> dict | None:
        with self._connect_read_only() as connection:
            row = connection.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return dict(row) if row else None

    def _get_images_read_only(self, article_id: int) -> list[dict]:
        with self._connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM article_images WHERE article_id=? ORDER BY position_index, id",
                (article_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_image(self, path: str) -> bytes:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not any(_is_within(resolved, root) for root in self.allowed_image_roots):
            raise ContentAssetError("旧文章图片路径超出允许的只读目录")
        return resolved.read_bytes()


class DocxImportAdapter:
    def __init__(
        self,
        asset_store: AssetStore,
        *,
        work_root: str | Path | None = None,
        parser_factory=None,
    ) -> None:
        self.asset_store = asset_store
        self.work_root = Path(work_root or default_work_root()).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        if parser_factory is None:
            from core.docx_parser import DocxParser

            parser_factory = DocxParser
        self.parser_factory = parser_factory

    async def parse(self, data: bytes, filename: str) -> tuple[str, list[dict], list[StoredAsset]]:
        return await asyncio.to_thread(self._parse_sync, data, filename)

    async def parse_v2(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[str, list[dict], list[StoredAsset], dict]:
        """导入 DOCX 富结构；旧 ``parse`` 契约保持不变。"""

        return await asyncio.to_thread(self._parse_v2_sync, data, filename)

    async def parse_document_v2(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[str, list[dict], list[StoredAsset], dict]:
        """``parse_v2`` 的语义化别名，便于工作台按文档命名调用。"""

        return await self.parse_v2(data, filename)

    def _parse_sync(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[str, list[dict], list[StoredAsset]]:
        if not data:
            raise ContentAssetError("DOCX 文件为空")
        if len(data) > MAX_DOCX_BYTES:
            raise ContentAssetError("DOCX 文件不能超过 50MB")
        if not filename.lower().endswith(".docx"):
            raise ContentAssetError("仅支持 .docx 文件")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_DOCX_ARCHIVE_ENTRIES:
                    raise ContentAssetError("DOCX 内部文件数量超过安全限制")
                if sum(item.file_size for item in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ContentAssetError("DOCX 解压后内容超过 250MB 安全限制")
        except zipfile.BadZipFile as exc:
            raise ContentAssetError("文件不是有效的 DOCX 文档") from exc
        with tempfile.TemporaryDirectory(prefix="docx-", dir=self.work_root) as temp_dir:
            temp_root = Path(temp_dir).resolve()
            upload = temp_root / "source.docx"
            upload.write_bytes(data)
            try:
                parser = self.parser_factory(images_dir=str(temp_root / "images"))
            except TypeError:
                # 测试替身和早期兼容解析器可能尚未接受注入参数。
                parser = self.parser_factory()
                parser.images_dir = str(temp_root / "images")
            parsed = parser.parse(str(upload))
            title = parser.get_title_from_article(parsed)
            stored_assets: list[StoredAsset] = []
            blocks: list[dict] = []
            try:
                for position, source in enumerate(parsed.blocks):
                    block_id = str(uuid.uuid4())
                    if source.type in {"text", "heading"} and source.text:
                        blocks.append(
                            {
                                "block_id": block_id,
                                "type": "text",
                                "text": source.text,
                                "position": position,
                            }
                        )
                        continue
                    if source.type != "image" or not source.image_path:
                        continue
                    image_path = Path(source.image_path).resolve(strict=True)
                    if not _is_within(image_path, temp_root):
                        raise ContentAssetError("DOCX 解析器返回了工作目录之外的图片")
                    stored = self.asset_store.save_image(
                        image_path.read_bytes(),
                        source.image_filename or image_path.name,
                    )
                    stored_assets.append(stored)
                    blocks.append(
                        {
                            "block_id": block_id,
                            "type": "image",
                            "asset_id": stored.asset_id,
                            "alt": source.image_filename or "文档图片",
                            "position": position,
                        }
                    )
                # The title is persisted separately from the body.  Remove only
                # the first matching text block so repeated in-body headings stay
                # intact, then close the position gap.
                normalized_title = _normalize_title(title)
                if normalized_title:
                    for index, block in enumerate(blocks):
                        if block.get("type") != "text":
                            continue
                        if _normalize_title(block.get("text")) == normalized_title:
                            del blocks[index]
                        break
                return title, _normalize_positions(blocks), stored_assets
            except Exception:
                _remove_stored_assets(self.asset_store, stored_assets)
                raise

    def _parse_v2_sync(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[str, list[dict], list[StoredAsset], dict]:
        self._validate_docx_payload(data, filename)
        with tempfile.TemporaryDirectory(prefix="docx-v2-", dir=self.work_root) as temp_dir:
            temp_root = Path(temp_dir).resolve()
            upload = temp_root / "source.docx"
            upload.write_bytes(data)
            try:
                parser = self.parser_factory(images_dir=str(temp_root / "images"))
            except TypeError:
                parser = self.parser_factory()
                parser.images_dir = str(temp_root / "images")

            stored_assets: list[StoredAsset] = []
            try:
                parsed = parser.parse_document_v2(str(upload))
                document = self._resolve_v2_assets(
                    parsed.document_v2,
                    temp_root,
                    stored_assets,
                )
                try:
                    document = validate_document(document)
                except ContentDocumentValidationError as exc:
                    raise ContentAssetError(f"DOCX 富结构校验失败: {exc}") from exc
                projection = project_to_v1(document, omit_title_block=True)
                return document["title"], projection.blocks, stored_assets, document
            except ContentAssetError:
                _remove_stored_assets(self.asset_store, stored_assets)
                raise
            except Exception as exc:
                # 解析器、关系读取或受控 asset 转换的任何普通失败都必须
                # 进入统一的 fail-closed 错误边界，并先删除本批已落盘资产。
                # 不捕获 BaseException，避免吞掉取消、退出和中断信号。
                _remove_stored_assets(self.asset_store, stored_assets)
                raise ContentAssetError(f"DOCX 富结构解析失败: {exc}") from exc

    @staticmethod
    def _validate_docx_payload(data: bytes, filename: str) -> None:
        if not data:
            raise ContentAssetError("DOCX 文件为空")
        if len(data) > MAX_DOCX_BYTES:
            raise ContentAssetError("DOCX 文件不能超过 50MB")
        if not filename.lower().endswith(".docx"):
            raise ContentAssetError("仅支持 .docx 文件")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_DOCX_ARCHIVE_ENTRIES:
                    raise ContentAssetError("DOCX 内部文件数量超过安全限制")
                if sum(item.file_size for item in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ContentAssetError("DOCX 解压后内容超过 250MB 安全限制")
        except zipfile.BadZipFile as exc:
            raise ContentAssetError("文件不是有效的 DOCX 文档") from exc

    def _resolve_v2_assets(
        self,
        document: dict,
        temp_root: Path,
        stored_assets: list[StoredAsset],
    ) -> dict:
        """将解析器内部图片引用替换为受控 UUID，绝不外泄临时路径。"""

        def resolve_inline(node: dict) -> dict:
            if node.get("kind") == "text":
                return dict(node)
            if node.get("kind") != "image":
                raise ContentAssetError("DOCX 富结构包含未知 inline 节点")
            source_value = node.get("__source_path")
            if not isinstance(source_value, str) or not source_value:
                raise ContentAssetError("DOCX 图片缺少解析引用")
            image_path = Path(source_value).expanduser().resolve(strict=True)
            if not _is_within(image_path, temp_root):
                raise ContentAssetError("DOCX 解析器返回了工作目录之外的图片")
            filename = str(node.get("__original_filename") or image_path.name)
            stored = self.asset_store.save_image(image_path.read_bytes(), filename)
            stored_assets.append(stored)
            result = {
                "kind": "image",
                "asset_id": stored.asset_id,
                "width": stored.width,
                "height": stored.height,
            }
            for field in ("alt", "caption", "anchor"):
                if node.get(field) is not None:
                    result[field] = node[field]
            return result

        def resolve_block(block: dict) -> dict:
            kind = block.get("kind")
            if kind in {"paragraph", "heading"}:
                result = {key: value for key, value in block.items() if not key.startswith("__")}
                result["children"] = [resolve_inline(child) for child in block.get("children", [])]
                return result
            if kind == "list":
                result = {key: value for key, value in block.items() if not key.startswith("__")}
                result["items"] = [
                    {"blocks": [resolve_block(nested) for nested in item.get("blocks", [])]}
                    for item in block.get("items", [])
                ]
                return result
            if kind == "table":
                result = {key: value for key, value in block.items() if not key.startswith("__")}
                result["rows"] = [
                    {
                        "cells": [
                            {
                                **{
                                    key: value
                                    for key, value in cell.items()
                                    if key != "blocks" and not key.startswith("__")
                                },
                                "blocks": [
                                    resolve_block(nested) for nested in cell.get("blocks", [])
                                ],
                            }
                            for cell in row.get("cells", [])
                        ]
                    }
                    for row in block.get("rows", [])
                ]
                return result
            raise ContentAssetError("DOCX 富结构包含未知 block")

        result = {key: value for key, value in document.items() if not key.startswith("__")}
        result["blocks"] = [resolve_block(block) for block in document.get("blocks", [])]
        return result


async def copy_legacy_article(
    source: LegacyDatabaseSource,
    asset_store: AssetStore,
    article_id: int,
) -> tuple[dict, list[dict], list[StoredAsset]]:
    article = await source.get_article(article_id)
    if article is None:
        raise DraftNotFoundError("旧文章不存在")
    image_rows = await source.get_article_images(article_id)
    image_by_path = {str(Path(row["local_path"]).expanduser().resolve()): row for row in image_rows}
    try:
        content = json.loads(article.get("content_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        content = {}
    source_blocks = content.get("blocks") if isinstance(content, dict) else None
    if not isinstance(source_blocks, list):
        source_blocks = []

    blocks: list[dict] = []
    assets: list[StoredAsset] = []
    try:
        for raw in source_blocks:
            if not isinstance(raw, dict):
                continue
            block_type = raw.get("type")
            position = len(blocks)
            if block_type in {"text", "heading"} and str(raw.get("text") or "").strip():
                blocks.append(
                    {
                        "block_id": str(uuid.uuid4()),
                        "type": "text",
                        "text": str(raw["text"]),
                        "position": position,
                    }
                )
                continue
            if block_type != "image":
                continue
            candidate = str(raw.get("image_path") or "")
            row = (
                image_by_path.get(str(Path(candidate).expanduser().resolve()))
                if candidate
                else None
            )
            if row is None:
                raw_position = raw.get("position")
                row = next(
                    (item for item in image_rows if item.get("position_index") == raw_position),
                    None,
                )
            if row is None:
                raise ContentAssetError("旧文章图片块缺少对应的受控图片记录")
            data = source.read_image(row["local_path"])
            stored = asset_store.save_image(data, row.get("filename") or "legacy-image")
            assets.append(stored)
            blocks.append(
                {
                    "block_id": str(uuid.uuid4()),
                    "type": "image",
                    "asset_id": stored.asset_id,
                    "alt": row.get("filename") or "文章图片",
                    "position": position,
                }
            )
    except Exception:
        _remove_stored_assets(asset_store, assets)
        raise

    if not blocks and str(article.get("content_text") or "").strip():
        blocks = [
            {
                "block_id": str(uuid.uuid4()),
                "type": "text",
                "text": str(article["content_text"]),
                "position": 0,
            }
        ]
    return article, _normalize_positions(blocks), assets


def public_legacy_article(article: dict) -> dict:
    return {
        "article_id": article.get("id"),
        "title": article.get("title") or article.get("filename") or "未命名文章",
        "filename": article.get("filename"),
        "image_count": article.get("image_count") or 0,
        "char_count": article.get("char_count") or 0,
        "status": article.get("status"),
        "created_at": article.get("created_at"),
    }


def _normalize_positions(blocks: list[dict]) -> list[dict]:
    for position, block in enumerate(blocks):
        block["position"] = position
    return blocks


def _normalize_title(value: object) -> str:
    return " ".join(str(value or "").split())


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _remove_stored_assets(
    asset_store: AssetStore,
    assets: list[StoredAsset],
) -> None:
    for asset in assets:
        try:
            asset_store.remove_if_owned(asset.storage_path)
        except OSError:
            # 清理异常不能覆盖真正的导入失败原因。
            pass
