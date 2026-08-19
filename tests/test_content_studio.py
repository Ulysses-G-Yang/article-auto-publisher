"""统一创作与投递后端验收测试。"""

import asyncio
import hashlib
import io
import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from flask import Flask
from PIL import Image
from sqlalchemy import func, select

from account_sessions.account_service import AccountSessionService
from account_sessions.database import AccountDatabase
from account_sessions.models import DeliveryOperation, PlatformAccount
from account_sessions.permissions import LOCAL_WEB_CONTEXT
from account_sessions.web import AccountSessionRuntimeState
from content_studio.assets import AssetStore
from content_studio.content_document import delivery_features
from content_studio.contracts import (
    CoverInput,
    CreateDraftRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase, sqlite_url
from content_studio.errors import (
    ContentAssetError,
    DeliveryPlanStaleError,
    DraftBatchConfirmationRequiredError,
    DraftContentSchemaConflictError,
    DraftRevisionConflictError,
    DraftTargetConflictError,
    DraftValidationError,
)
from content_studio.importers import DocxImportAdapter, LegacyDatabaseSource
from content_studio.models import ContentAsset, ContentDraft, ContentVersion
from content_studio.platform_format_capabilities import (
    DEFAULT_PLATFORM_FORMAT_CAPABILITIES,
    DELIVERY_PLATFORMS,
    PlatformFormatCapabilities,
    PlatformFormatDeclaration,
)
from content_studio.service import (
    SEED_KEY,
    SEED_TITLE,
    ContentStudioService,
    _content_hash,
)
from content_studio.web import create_content_studio_blueprint


def run(coroutine):
    return asyncio.run(coroutine)


def image_bytes(image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), color=(20, 90, 160)).save(buffer, format=image_format)
    return buffer.getvalue()


def docx_bytes() -> bytes:
    from docx import Document
    from docx.shared import Inches

    document = Document()
    document.add_heading("导入标题", level=1)
    document.add_paragraph("第一段正文")
    picture = io.BytesIO(image_bytes())
    document.add_picture(picture, width=Inches(1))
    document.add_paragraph("第二段正文")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_legacy_database(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "legacy-images"
    root.mkdir()
    image_path = root / "one.png"
    image_path.write_bytes(image_bytes())
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY, filename TEXT, title TEXT,
                content_text TEXT, content_json TEXT, image_count INTEGER,
                char_count INTEGER, status TEXT, created_at TEXT
            );
            CREATE TABLE article_images (
                id INTEGER PRIMARY KEY, article_id INTEGER, filename TEXT,
                local_path TEXT, position_index INTEGER, width INTEGER,
                height INTEGER, file_size INTEGER
            );
            """
        )
        content = {
            "blocks": [
                {"type": "text", "text": "旧文章正文", "position": 0},
                {"type": "image", "image_path": str(image_path), "position": 1},
            ]
        }
        connection.execute(
            "INSERT INTO articles VALUES (1,?,?,?,?,?,?,?,?)",
            (
                "legacy.docx",
                "旧文章标题",
                "旧文章正文",
                json.dumps(content, ensure_ascii=False),
                1,
                5,
                "parsed",
                "2026-08-01 10:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO article_images VALUES (1,1,?,?,?,?,?,?)",
            ("one.png", str(image_path), 1, 32, 16, len(image_bytes())),
        )
    return database_path, root


def make_service(
    tmp_path: Path,
    *,
    account_service=None,
    platform_format_capabilities: PlatformFormatCapabilities | None = None,
) -> ContentStudioService:
    database_path = tmp_path / "content" / "content.db"
    asset_store = AssetStore(tmp_path / "content" / "assets")
    legacy_path, legacy_root = make_legacy_database(tmp_path)
    legacy = LegacyDatabaseSource(
        database_path=legacy_path,
        allowed_image_roots=(legacy_root,),
    )
    return ContentStudioService(
        ContentDatabase(sqlite_url(database_path)),
        asset_store=asset_store,
        legacy_source=legacy,
        docx_importer=DocxImportAdapter(
            asset_store,
            work_root=tmp_path / "content" / "work",
        ),
        account_service=account_service,
        platform_format_capabilities=platform_format_capabilities,
    )


def rich_v2_document(
    title: str,
    asset_id: str,
    *,
    marks: list[str] | None = None,
    style_name: str | None = None,
    link: dict[str, str] | None = None,
    caption: str | None = None,
    anchor_kind: str | None = None,
) -> dict:
    """构造覆盖 v2 canonical 字段的最小富文档 fixture。"""

    text_node = {"kind": "text", "text": "开头"}
    if marks:
        text_node["marks"] = marks
    if link:
        text_node["link"] = link
    image_node = {"kind": "image", "asset_id": asset_id}
    if caption is not None:
        image_node["caption"] = caption
    if anchor_kind is not None:
        image_node["anchor"] = {"kind": anchor_kind}
    paragraph = {
        "kind": "paragraph",
        "block_id": "rich-body",
        "children": [
            text_node,
            image_node,
            {"kind": "text", "text": "结尾"},
        ],
    }
    if style_name is not None:
        paragraph["style_name"] = style_name
    return {
        "schema_version": 2,
        "title": title,
        "source_fidelity": "NATIVE",
        "blocks": [paragraph],
    }


def delivery_v2_document(
    title: str,
    *,
    body_heading: bool = False,
    body_heading_level: int = 2,
    asset_ids: list[str] | None = None,
) -> dict:
    """构造标题独立映射、正文能力可控的投递 fixture。"""

    blocks = [
        {
            "kind": "heading",
            "block_id": "title-block",
            "level": 1,
            "children": [
                {"kind": "text", "text": title, "marks": ["bold"]},
            ],
        }
    ]
    if body_heading:
        blocks.append(
            {
                "kind": "heading",
                "block_id": "body-heading",
                "level": body_heading_level,
                "children": [{"kind": "text", "text": "正文小节"}],
            }
        )
    if asset_ids:
        blocks.extend(
            {
                "kind": "paragraph",
                "block_id": f"image-{index}",
                "children": [{"kind": "image", "asset_id": asset_id}],
            }
            for index, asset_id in enumerate(asset_ids)
        )
    else:
        blocks.append(
            {
                "kind": "paragraph",
                "block_id": "body",
                "children": [{"kind": "text", "text": "普通正文"}],
            }
        )
    return {
        "schema_version": 2,
        "title": title,
        "title_block_id": "title-block",
        "source_fidelity": "NATIVE",
        "blocks": blocks,
    }


def test_seed_is_idempotent_and_database_pragmas(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        first = await service.ensure_seed_draft()
        second = await service.ensure_seed_draft()
        assert first["draft_id"] == second["draft_id"]
        assert first["title"] == SEED_TITLE
        assert first["source_ref"] == SEED_KEY
        async with service.database.session() as session:
            count = await session.scalar(select(func.count(ContentDraft.draft_id)))
            assert count == 1
        async with service.database.engine.connect() as connection:
            journal = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
            timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
            foreign_keys = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        assert str(journal).lower() == "wal"
        assert timeout == 5000
        assert foreign_keys == 1
        await service.database.dispose()

    run(scenario())


def test_platform_format_capabilities_are_explicit_and_fail_closed() -> None:
    registry = PlatformFormatCapabilities()

    assert set(registry.declarations) == set(DELIVERY_PLATFORMS)
    assert all(not declaration.supported for declaration in registry.declarations.values())
    assert registry.get("unknown-platform") is None

    injected = PlatformFormatCapabilities(
        {
            "xiaoheihe": PlatformFormatDeclaration(
                "xiaoheihe", frozenset({"heading"}), frozenset({2, 3})
            )
        }
    )
    assert injected.get("xiaoheihe").supported == frozenset({"heading"})
    assert injected.get("xiaoheihe").heading_levels == frozenset({2, 3})
    assert injected.get("zol").supported == frozenset()
    with pytest.raises(ValueError, match="未知格式能力"):
        PlatformFormatCapabilities({"xiaoheihe": {"not_a_real_feature"}})


def test_default_capabilities_match_real_xiaoheihe_heading_and_image_evidence() -> None:
    assert DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("xiaoheihe").supported == {
        "heading",
        "image_order",
    }
    assert DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("xiaoheihe").heading_levels == {
        2,
        3,
    }
    for platform in DELIVERY_PLATFORMS:
        if platform != "xiaoheihe":
            assert not DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get(platform).supported


def test_exact_word_features_keep_xiaoheihe_heading_plan_blocked() -> None:
    document = delivery_v2_document(
        "Word 标题",
        body_heading=True,
        asset_ids=[
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        ],
    )

    assert delivery_features(document) == frozenset({"heading", "image_order"})
    declaration = DEFAULT_PLATFORM_FORMAT_CAPABILITIES.get("xiaoheihe")
    assert delivery_features(document) - declaration.supported == set()
    assert {2} - declaration.heading_levels == set()


def test_autosave_revision_conflict_returns_server_draft(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="", blocks=[]))
        payload = PatchDraftRequest(
            revision=draft["revision"],
            title="第一版",
            blocks=[{"type": "text", "text": "正文一", "position": 0}],
        )
        saved = await service.patch_draft(draft["draft_id"], payload)
        assert saved["revision"] == 2
        with pytest.raises(DraftRevisionConflictError) as conflict:
            await service.patch_draft(draft["draft_id"], payload)
        assert conflict.value.server_draft["title"] == "第一版"
        assert conflict.value.server_draft["revision"] == 2
        await service.database.dispose()

    run(scenario())


def test_asset_blocks_can_be_reordered_without_exposing_paths(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="图文", blocks=[]))
        first = await service.add_asset(draft["draft_id"], image_bytes(), "一.png")
        second = await service.add_asset(draft["draft_id"], image_bytes("JPEG"), "二.jpg")
        updated = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="图文",
                blocks=[
                    {"type": "image", "asset_id": second["asset_id"], "position": 99},
                    {"type": "text", "text": "中间", "position": 42},
                    {"type": "image", "asset_id": first["asset_id"], "position": 0},
                ],
            ),
        )
        assert [block["position"] for block in updated["blocks"]] == [0, 1, 2]
        assert updated["blocks"][0]["asset_id"] == second["asset_id"]
        assert all("storage_path" not in block for block in updated["blocks"])
        assert "storage_path" not in first
        await service.database.dispose()

    run(scenario())


def test_legacy_copy_preserves_source_rows_and_copies_images(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    legacy_path = service.legacy_source.database_path

    async def scenario():
        await service.initialize()
        before = legacy_path.read_bytes()
        listing = await service.list_legacy_articles(limit=10, offset=0)
        copied = await service.create_from_legacy(1)
        after = legacy_path.read_bytes()
        assert listing["read_only"] is True
        assert listing["total"] == 1
        assert copied["source_type"] == "LEGACY_ARTICLE"
        assert copied["source_ref"] == "1"
        assert [block["type"] for block in copied["blocks"]] == ["text", "image"]
        assert before == after
        await service.database.dispose()

    run(scenario())


def test_docx_import_uses_unified_blocks_and_controlled_assets(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        imported = await service.import_docx(docx_bytes(), "测试文档.docx")
        assert imported["source_type"] == "DOCX"
        assert imported["title"] == "导入标题"
        assert imported["content_schema_version"] == 2
        assert imported["document"]["schema_version"] == 2
        assert [block["type"] for block in imported["blocks"]] == [
            "text",
            "image",
            "text",
        ]
        image = next(block for block in imported["blocks"] if block["type"] == "image")
        assert image["asset_url"].endswith(image["asset_id"])
        assert "local_path" not in json.dumps(imported, ensure_ascii=False)
        await service.database.dispose()
        reopened = ContentStudioService(
            ContentDatabase(sqlite_url(tmp_path / "content" / "content.db")),
            asset_store=AssetStore(tmp_path / "content" / "assets"),
            docx_importer=DocxImportAdapter(
                AssetStore(tmp_path / "content" / "assets"),
                work_root=tmp_path / "content" / "work",
            ),
        )
        await reopened.initialize()
        loaded = await reopened.get_draft(imported["draft_id"])
        await reopened.database.dispose()
        assert loaded["content_schema_version"] == 2
        assert loaded["document"] == imported["document"]
        return imported, loaded

    imported, loaded = run(scenario())
    assert loaded["blocks"] == imported["blocks"]


def test_v2_patch_is_canonical_and_old_client_cannot_downgrade(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(title="v1 草稿", blocks=[])
        )
        asset = await service.add_asset(draft["draft_id"], image_bytes(), "nested.png")
        document = {
            "schema_version": 2,
            "title": "v2 标题",
            "title_block_id": "title-block",
            "source_fidelity": "NATIVE",
            "blocks": [
                {
                    "kind": "heading",
                    "block_id": "title-block",
                    "level": 1,
                    "children": [{"kind": "text", "text": "v2 标题"}],
                },
                {
                    "kind": "table",
                    "block_id": "table-block",
                    "rows": [
                        {
                            "cells": [
                                {
                                    "blocks": [
                                        {
                                            "kind": "paragraph",
                                            "block_id": "cell-block",
                                            "children": [
                                                {"kind": "text", "text": "表格前"},
                                                {
                                                    "kind": "image",
                                                    "asset_id": asset["asset_id"],
                                                    "alt": "嵌套图",
                                                    "caption": "图注",
                                                    "anchor": {"kind": "floating"},
                                                },
                                            ],
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                },
            ],
        }
        upgraded = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="v2 标题",
                # v2 的 document 是唯一真值；故意提供错误 v1 blocks。
                blocks=[{"type": "text", "text": "不会持久化", "position": 0}],
                content_schema_version=2,
                document=document,
            ),
        )
        before = await service.get_draft(draft["draft_id"])
        with pytest.raises(DraftContentSchemaConflictError) as conflict:
            await service.patch_draft(
                draft["draft_id"],
                PatchDraftRequest(
                    revision=upgraded["revision"],
                    title="旧页面覆盖",
                    blocks=[{"type": "text", "text": "旧正文", "position": 0}],
                ),
            )
        after = await service.get_draft(draft["draft_id"])
        assert conflict.value.error_code == "DRAFT_CONTENT_SCHEMA_CONFLICT"
        for field in ("revision", "title", "blocks", "document", "cover", "targets"):
            assert after[field] == before[field]
        assert after["content_schema_version"] == 2
        assert "storage_path" not in json.dumps(after, ensure_ascii=False)

        changed_document = json.loads(json.dumps(document, ensure_ascii=False))
        changed_document["blocks"][1]["rows"][0]["cells"][0]["blocks"][0]["children"][0][
            "text"
        ] = "表格已更新"
        changed = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=after["revision"],
                title="v2 标题",
                blocks=[],
                content_schema_version=2,
                document=changed_document,
            ),
        )
        await service.database.dispose()
        return upgraded, changed

    upgraded, changed = run(scenario())
    assert upgraded["content_schema_version"] == 2
    assert upgraded["blocks"][0]["type"] == "text"
    assert changed["revision"] == upgraded["revision"] + 1
    assert changed["document"]["blocks"][1]["rows"][0]["cells"][0]["blocks"][0][
        "children"
    ][0]["text"] == "表格已更新"


def test_v2_nested_asset_must_belong_to_draft(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="v2", blocks=[]))
        foreign = await service.create_draft(CreateDraftRequest(title="other", blocks=[]))
        foreign_asset = await service.add_asset(foreign["draft_id"], image_bytes(), "foreign.png")
        document = {
            "schema_version": 2,
            "title": "v2",
            "source_fidelity": "NATIVE",
            "blocks": [
                {
                    "kind": "list",
                    "block_id": "list",
                    "ordered": False,
                    "level": 0,
                    "items": [
                        {
                            "blocks": [
                                {
                                    "kind": "paragraph",
                                    "children": [
                                        {"kind": "image", "asset_id": foreign_asset["asset_id"]}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ],
        }
        with pytest.raises(ContentAssetError, match="当前草稿"):
            await service.patch_draft(
                draft["draft_id"],
                PatchDraftRequest(
                    revision=draft["revision"],
                    title="v2",
                    blocks=[],
                    content_schema_version=2,
                    document=document,
                ),
            )
        unchanged = await service.get_draft(draft["draft_id"])
        await service.database.dispose()
        return unchanged

    unchanged = run(scenario())
    assert unchanged["content_schema_version"] == 1
    assert unchanged["revision"] == 1


def test_v2_missing_asset_file_rolls_back_patch_atomically(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="文件回滚", blocks=[]))
        asset = await service.add_asset(draft["draft_id"], image_bytes(), "missing.png")
        async with service.database.session() as session:
            stored = await session.get(ContentAsset, asset["asset_id"])
            Path(stored.storage_path).unlink()
        document = {
            "schema_version": 2,
            "title": "文件回滚",
            "source_fidelity": "NATIVE",
            "blocks": [
                {
                    "kind": "paragraph",
                    "children": [{"kind": "image", "asset_id": asset["asset_id"]}],
                }
            ],
        }
        with pytest.raises(ContentAssetError, match="图片文件不存在"):
            await service.patch_draft(
                draft["draft_id"],
                PatchDraftRequest(
                    revision=draft["revision"],
                    title="文件回滚",
                    content_schema_version=2,
                    document=document,
                ),
            )
        unchanged = await service.get_draft(draft["draft_id"])
        await service.database.dispose()
        return unchanged

    unchanged = run(scenario())
    assert unchanged["content_schema_version"] == 1
    assert unchanged["revision"] == 1
    assert unchanged["blocks"] == []


def test_target_duplicates_and_frozen_version_are_enforced(tmp_path: Path) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        profile = tmp_path / "profile"
        profile.mkdir()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="1001",
            display_name="测试昵称",
            profile_path=str(profile),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(
                title="冻结内容",
                blocks=[{"type": "text", "text": "同一正文", "position": 0}],
            )
        )
        duplicate = ReplaceTargetsRequest(
            revision=draft["revision"],
            targets=[
                {"platform": "xiaoheihe", "account_id": account.account_id, "mode": "DRAFT"},
                {"platform": "xiaoheihe", "account_id": account.account_id, "mode": "PUBLISH"},
            ],
        )
        with pytest.raises(DraftTargetConflictError):
            await service.replace_targets(draft["draft_id"], duplicate, LOCAL_WEB_CONTEXT)

        from content_studio.errors import DraftValidationError

        with pytest.raises(DraftValidationError, match="保持登录态策略尚未同步"):
            await service.replace_targets(
                draft["draft_id"],
                ReplaceTargetsRequest(
                    revision=draft["revision"],
                    targets=[
                        {
                            "platform": "xiaoheihe",
                            "account_id": account.account_id,
                            "mode": "DRAFT",
                            "persist_login": False,
                        }
                    ],
                ),
                LOCAL_WEB_CONTEXT,
            )

        targeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=draft["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        plan = await service.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )
        assert plan["content_version"]
        async with service.database.session() as session:
            versions = await session.scalar(select(func.count(ContentVersion.version_id)))
            assert versions == 1
        await service.database.dispose()
        await account_db.dispose()

    run(scenario())


def test_v1_content_hash_remains_backward_compatible() -> None:
    title = "旧版 hash"
    blocks = [{"type": "text", "text": "正文", "position": 0}]
    cover_strategy = "EXPLICIT"
    cover_asset_id = "asset-legacy"
    legacy_payload = json.dumps(
        {
            "title": title,
            "blocks": blocks,
            "cover": {
                "strategy": cover_strategy,
                "asset_id": cover_asset_id,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()

    assert _content_hash(title, blocks, cover_strategy, cover_asset_id) == expected
    assert (
        _content_hash(
            title,
            blocks,
            cover_strategy,
            cover_asset_id,
            content_schema_version=1,
        )
        == expected
    )


def test_v2_plan_freezes_document_and_hash_is_semantic_and_idempotent(tmp_path: Path) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="v2-plan-user",
            display_name="v2计划账号",
            profile_path=str(tmp_path / "profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()

        draft = await service.create_draft(
            CreateDraftRequest(title="富文档", blocks=[])
        )
        body_asset = await service.add_asset(draft["draft_id"], image_bytes(), "正文.png")
        cover_asset = await service.add_asset(
            draft["draft_id"], image_bytes("JPEG"), "封面.jpg"
        )
        document = rich_v2_document("富文档", body_asset["asset_id"])
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="富文档",
                content_schema_version=2,
                document=document,
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )

        base_plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        repeated_plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        assert repeated_plan["content_version"] == base_plan["content_version"]
        assert base_plan["content_schema_version"] == 2
        assert base_plan["document"] == current["document"]
        assert "storage_path" not in json.dumps(base_plan, ensure_ascii=False)

        base_context, _ = await service.get_plan_execution_context(
            base_plan["plan_id"], LOCAL_WEB_CONTEXT
        )
        assert base_context["content_schema_version"] == 2
        assert base_context["document"] == current["document"]
        base_resolved = await service.resolve_delivery_payload(
            await _version_id_for_hash(service, base_plan["content_version"])
        )

        variants = [
            rich_v2_document("富文档", body_asset["asset_id"], marks=["bold"]),
            rich_v2_document("富文档", body_asset["asset_id"], style_name="Quote"),
            rich_v2_document(
                "富文档",
                body_asset["asset_id"],
                link={"href": "https://example.com", "title": "链接"},
            ),
            rich_v2_document("富文档", body_asset["asset_id"], caption="正文说明"),
            rich_v2_document("富文档", body_asset["asset_id"], anchor_kind="floating"),
        ]
        hashes = {base_plan["content_version"]}
        for variant in variants:
            current = await service.patch_draft(
                draft["draft_id"],
                PatchDraftRequest(
                    revision=current["revision"],
                    title="富文档",
                    content_schema_version=2,
                    document=variant,
                ),
            )
            variant_plan = await service.create_delivery_plan(
                draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
            )
            hashes.add(variant_plan["content_version"])

        cover_document = rich_v2_document("富文档", body_asset["asset_id"])
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=current["revision"],
                title="富文档",
                content_schema_version=2,
                document=cover_document,
                cover=CoverInput(
                    strategy="EXPLICIT",
                    asset_id=cover_asset["asset_id"],
                ),
            ),
        )
        cover_plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        hashes.add(cover_plan["content_version"])

        titled_document = rich_v2_document("富文档改名", body_asset["asset_id"])
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=current["revision"],
                title="富文档改名",
                content_schema_version=2,
                document=titled_document,
            ),
        )
        title_plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        hashes.add(title_plan["content_version"])

        assert len(hashes) == 8
        async with service.database.session() as session:
            versions = list(
                (
                    await session.scalars(
                        select(ContentVersion).where(
                            ContentVersion.draft_id == draft["draft_id"]
                        )
                    )
                ).all()
            )
            assert len(versions) == 8
            base_version = next(
                item for item in versions if item.content_hash == base_plan["content_version"]
            )
            assert base_version.content_schema_version == 2
            assert base_version.document_json == document
            assert base_version.blocks_json == base_context["blocks"]
            frozen_document = base_version.document_json
            frozen_projection = base_version.blocks_json

        # 活动草稿已经多次更新，旧 ContentVersion 和其解析结果仍保持不变。
        frozen_resolved = await service.resolve_delivery_payload(base_version.version_id)
        assert frozen_resolved == base_resolved
        async with service.database.session() as session:
            base_version = await session.get(ContentVersion, base_version.version_id)
            assert base_version.document_json == frozen_document
            assert base_version.blocks_json == frozen_projection
        await service.database.dispose()
        await account_db.dispose()

    run(scenario())


def test_v2_format_gate_is_per_target_and_execute_skips_review_targets(
    tmp_path: Path,
) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = CapturingDelivery()
    service = make_service(
        tmp_path,
        account_service=accounts,
        platform_format_capabilities=PlatformFormatCapabilities(
            {
                "xiaoheihe": PlatformFormatDeclaration(
                    "xiaoheihe", frozenset({"heading"}), frozenset({2})
                )
            }
        ),
    )

    async def scenario():
        await accounts.initialize()
        inserted = []
        for platform in ("xiaoheihe", "zol"):
            account = PlatformAccount(
                account_id=str(uuid.uuid4()),
                platform=platform,
                platform_user_id=f"format-{platform}",
                display_name=f"{platform}格式账号",
                profile_path=str(tmp_path / f"profile-{platform}"),
                status="ACTIVE",
                session_status="VALID",
                persist_login=True,
            )
            async with account_db.session() as session:
                session.add(account)
            inserted.append(account)
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="跨平台格式", blocks=[]))
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="跨平台格式",
                content_schema_version=2,
                document=delivery_v2_document("跨平台格式", body_heading=True),
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": account.platform,
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                    for account in inserted
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        return await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )

    plan = run(scenario())
    by_platform = {target["platform"]: target for target in plan["targets"]}
    assert plan["status"] == "PARTIAL_FAIL"
    assert by_platform["xiaoheihe"]["status"] == "READY"
    assert by_platform["zol"]["status"] == "FORMAT_REVIEW_REQUIRED"
    assert by_platform["zol"]["error_code"] == "CONTENT_FORMAT_UNSUPPORTED"

    fake_state = FakeAccountState(accounts, delivery)
    from content_studio.contracts import ExecuteDeliveryPlanRequest
    from content_studio.web import ContentStudioRuntimeState

    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service
    with pytest.raises(DraftBatchConfirmationRequiredError, match="1 个平台草稿"):
        run(state.execute_plan(plan["plan_id"], ExecuteDeliveryPlanRequest(), LOCAL_WEB_CONTEXT))
    result = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert len(delivery.calls) == 1
    assert delivery.calls[0][0].platform == "xiaoheihe"
    result_by_platform = {target["platform"]: target for target in result["targets"]}
    assert result_by_platform["zol"]["operation_id"] is None
    assert result_by_platform["zol"]["status"] == "FORMAT_REVIEW_REQUIRED"

    run(service.database.dispose())
    run(account_db.dispose())


def test_v2_delivery_format_gate_handles_basic_heading_and_multi_image(
    tmp_path: Path,
) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="format-gate-user",
            display_name="格式门禁账号",
            profile_path=str(tmp_path / "profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()

        async def create_plan(
            title: str,
            *,
            body_heading: bool = False,
            body_heading_level: int = 2,
            image_count: int = 0,
        ) -> dict:
            draft = await service.create_draft(CreateDraftRequest(title=title, blocks=[]))
            asset_ids = []
            for index in range(image_count):
                asset = await service.add_asset(
                    draft["draft_id"], image_bytes(), f"格式图{index}.png"
                )
                asset_ids.append(asset["asset_id"])
            document = delivery_v2_document(
                title,
                body_heading=body_heading,
                body_heading_level=body_heading_level,
                asset_ids=asset_ids,
            )
            current = await service.patch_draft(
                draft["draft_id"],
                PatchDraftRequest(
                    revision=draft["revision"],
                    title=title,
                    content_schema_version=2,
                    document=document,
                ),
            )
            current = await service.replace_targets(
                draft["draft_id"],
                ReplaceTargetsRequest(
                    revision=current["revision"],
                    targets=[
                        {
                            "platform": "xiaoheihe",
                            "account_id": account.account_id,
                            "mode": "DRAFT",
                        }
                    ],
                ),
                LOCAL_WEB_CONTEXT,
            )
            return await service.create_delivery_plan(
                draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
            )

        plain = await create_plan("普通段落")
        single_image = await create_plan("单图正文", image_count=1)
        heading = await create_plan("正文标题", body_heading=True)
        unsupported_h1 = await create_plan(
            "未验证一级标题", body_heading=True, body_heading_level=1
        )
        unsupported_heading = await create_plan(
            "未验证标题", body_heading=True, body_heading_level=4
        )
        image_order_only = await create_plan("多图正文", image_count=2)

        assert plain["status"] == "READY"
        assert plain["targets"][0]["status"] == "READY"
        assert single_image["status"] == "READY"
        assert single_image["targets"][0]["status"] == "READY"
        assert heading["status"] == "READY"
        assert heading["targets"][0]["status"] == "READY"
        assert unsupported_h1["status"] == "FORMAT_REVIEW_REQUIRED"
        assert unsupported_h1["targets"][0]["error_code"] == (
            "CONTENT_FORMAT_UNSUPPORTED"
        )
        assert "heading_levels=1" in unsupported_h1["targets"][0]["error_message"]
        assert unsupported_heading["status"] == "FORMAT_REVIEW_REQUIRED"
        assert unsupported_heading["targets"][0]["error_code"] == (
            "CONTENT_FORMAT_UNSUPPORTED"
        )
        assert "heading_levels=4" in unsupported_heading["targets"][0]["error_message"]
        # 小黑盒的真实证据已覆盖 H2/H3 与交错插图和稳定图片数量。
        assert image_order_only["status"] == "READY"
        assert image_order_only["targets"][0]["status"] == "READY"

        await service.database.dispose()
        await account_db.dispose()

    run(scenario())


def test_format_review_target_does_not_reopen_when_capability_changes(
    tmp_path: Path,
) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = CapturingDelivery()
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="format-later-user",
            display_name="后验格式账号",
            profile_path=str(tmp_path / "profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="后验能力", blocks=[]))
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="后验能力",
                content_schema_version=2,
                document=delivery_v2_document(
                    "后验能力", body_heading=True, body_heading_level=4
                ),
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        service.platform_format_capabilities = PlatformFormatCapabilities(
            {
                "xiaoheihe": PlatformFormatDeclaration(
                    "xiaoheihe", frozenset({"heading"}), frozenset()
                )
            }
        )
        return plan

    plan = run(scenario())
    assert plan["status"] == "FORMAT_REVIEW_REQUIRED"
    fake_state = FakeAccountState(accounts, delivery)
    from content_studio.contracts import ExecuteDeliveryPlanRequest
    from content_studio.web import ContentStudioRuntimeState

    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service
    result = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert delivery.calls == []
    assert result["targets"][0]["status"] == "FORMAT_REVIEW_REQUIRED"
    run(service.database.dispose())
    run(account_db.dispose())


def test_v2_undeclared_weibo_target_is_reviewed_and_never_executed(
    tmp_path: Path,
) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = CapturingDelivery()
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="weibo",
            platform_user_id="weibo-format-user",
            display_name="微博格式账号",
            profile_path=str(tmp_path / "profile-weibo"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(title="微博未声明", blocks=[])
        )
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="微博未声明",
                content_schema_version=2,
                document=delivery_v2_document("微博未声明"),
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": "weibo",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        return await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )

    plan = run(scenario())
    target = plan["targets"][0]
    assert target["platform"] == "weibo"
    assert target["status"] == "FORMAT_REVIEW_REQUIRED"
    assert target["error_code"] == "PLATFORM_FORMAT_CAPABILITIES_UNDECLARED"

    fake_state = FakeAccountState(accounts, delivery)
    from content_studio.contracts import ExecuteDeliveryPlanRequest
    from content_studio.web import ContentStudioRuntimeState

    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service
    result = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert delivery.calls == []
    assert result["targets"][0]["operation_id"] is None
    assert result["targets"][0]["status"] == "FORMAT_REVIEW_REQUIRED"
    run(service.database.dispose())
    run(account_db.dispose())


async def _version_id_for_hash(service: ContentStudioService, content_hash: str) -> str:
    async def lookup() -> str:
        async with service.database.session() as session:
            version = await session.scalar(
                select(ContentVersion).where(ContentVersion.content_hash == content_hash)
            )
            assert version is not None
            return version.version_id

    return await lookup()


def test_v2_resolver_preserves_heading_level_and_inline_anchor_asset(
    tmp_path: Path,
) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="heading-resolver-user",
            display_name="标题解析账号",
            profile_path=str(tmp_path / "profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(CreateDraftRequest(title="解析标题", blocks=[]))
        asset = await service.add_asset(draft["draft_id"], image_bytes(), "inline.png")
        document = {
            "schema_version": 2,
            "title": "解析标题",
            "title_block_id": "title-block",
            "source_fidelity": "NATIVE",
            "blocks": [
                {
                    "kind": "heading",
                    "block_id": "title-block",
                    "level": 1,
                    "children": [{"kind": "text", "text": "解析标题"}],
                },
                {
                    "kind": "heading",
                    "block_id": "body-heading",
                    "level": 2,
                    "children": [{"kind": "text", "text": "正文 H2"}],
                },
                {
                    "kind": "paragraph",
                    "block_id": "inline-image",
                    "children": [
                        {
                            "kind": "image",
                            "asset_id": asset["asset_id"],
                            "anchor": {"kind": "inline"},
                        }
                    ],
                },
            ],
        }
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="解析标题",
                content_schema_version=2,
                document=document,
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        version_id = await _version_id_for_hash(service, plan["content_version"])
        resolved = await service.resolve_delivery_payload(version_id)
        await service.database.dispose()
        await account_db.dispose()
        return plan, resolved, asset["original_filename"]

    plan, resolved, asset_filename = run(scenario())
    assert plan["status"] == "READY"
    assert resolved[1][0] == {
        "type": "heading",
        "text": "正文 H2",
        "position": 0,
        "level": 2,
    }
    assert resolved[1][1]["type"] == "image"
    assert resolved[1][1]["position"] == 1
    assert resolved[2][0]["filename"] == asset_filename


def test_v2_corruption_fails_closed_before_plan_and_operation(tmp_path: Path) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="corrupt-v2-user",
            display_name="损坏测试账号",
            profile_path=str(tmp_path / "profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(title="损坏检查", blocks=[])
        )
        asset = await service.add_asset(draft["draft_id"], image_bytes(), "正文.png")
        document = rich_v2_document("损坏检查", asset["asset_id"])
        current = await service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=draft["revision"],
                title="损坏检查",
                content_schema_version=2,
                document=document,
            ),
        )
        current = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=current["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        async with service.database.session() as session:
            stored_draft = await session.get(ContentDraft, draft["draft_id"])
            storage_original = deepcopy(stored_draft.blocks_json)

        async with service.database.session() as session:
            row = await session.get(ContentDraft, draft["draft_id"])
            row.document_json = None
        with pytest.raises(DraftValidationError, match="缺少有效 document"):
            await service.create_delivery_plan(
                draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
            )

        async with service.database.session() as session:
            row = await session.get(ContentDraft, draft["draft_id"])
            row.document_json = document
            row.blocks_json = [
                *storage_original,
                {"type": "text", "text": "漂移", "position": 99},
            ]
        with pytest.raises(DraftValidationError, match="projection"):
            await service.create_delivery_plan(
                draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
            )
        async with service.database.session() as session:
            assert await session.scalar(select(func.count(ContentVersion.version_id))) == 0
            row = await session.get(ContentDraft, draft["draft_id"])
            row.blocks_json = deepcopy(storage_original)

        plan = await service.create_delivery_plan(
            draft["draft_id"], current["revision"], LOCAL_WEB_CONTEXT
        )
        async with service.database.session() as session:
            version = await session.scalar(
                select(ContentVersion).where(
                    ContentVersion.content_hash == plan["content_version"]
                )
            )
            assert version is not None
            version_id = version.version_id
            version.document_json = None

        with pytest.raises(DraftValidationError, match="缺少有效 document"):
            await service.get_delivery_plan(plan["plan_id"], LOCAL_WEB_CONTEXT)
        with pytest.raises(DraftValidationError, match="缺少有效 document"):
            await service.get_plan_execution_context(plan["plan_id"], LOCAL_WEB_CONTEXT)
        with pytest.raises(DraftValidationError, match="缺少有效 document"):
            await service.resolve_delivery_payload(version_id)

        await service.database.dispose()
        await account_db.dispose()

    run(scenario())


def test_http_contract_redirect_conflict_and_legacy_queue_gate(tmp_path: Path, monkeypatch) -> None:
    account_state = AccountSessionRuntimeState(
        database_url=sqlite_url(tmp_path / "account-http.db"),
        seed_legacy_profiles=False,
        auto_execute=False,
    )
    legacy_path, legacy_root = make_legacy_database(tmp_path)
    app = Flask("content-studio-test")
    app.secret_key = "test"
    app.add_url_rule("/upload", "upload_page", lambda: "studio")
    from account_sessions.web import create_account_session_blueprint

    app.register_blueprint(
        create_account_session_blueprint(
            database_url=sqlite_url(tmp_path / "account-http.db"),
            seed_legacy_profiles=False,
            auto_execute=False,
        )
    )
    account_state = app.extensions["account_sessions"]
    app.register_blueprint(
        create_content_studio_blueprint(
            account_state=account_state,
            database_url=sqlite_url(tmp_path / "content-http.db"),
            asset_root=tmp_path / "assets-http",
            work_root=tmp_path / "work-http",
            legacy_source=LegacyDatabaseSource(
                database_path=legacy_path,
                allowed_image_roots=(legacy_root,),
            ),
        )
    )
    client = app.test_client()
    redirect_response = client.get("/delivery/new?draft_id=abc%26unsafe", follow_redirects=False)
    assert redirect_response.status_code == 302
    assert redirect_response.headers["Location"] == "/upload?draft_id=abc%26unsafe"

    listing = client.get("/api/content-drafts")
    assert listing.status_code == 200
    assert any(row["source_type"] == "SYSTEM_SEED" for row in listing.get_json()["drafts"])
    created = client.post("/api/content-drafts", json={"title": "", "blocks": []})
    assert created.status_code == 201
    draft = created.get_json()
    assert draft["cover"] == {"strategy": "NONE", "asset_id": None, "asset_url": None}
    saved = client.patch(
        f"/api/content-drafts/{draft['draft_id']}",
        json={
            "revision": draft["revision"],
            "title": "保存成功",
            "blocks": [{"type": "text", "text": "保留空格  文本", "position": 0}],
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["blocks"][0]["text"] == "保留空格  文本"
    assert saved.get_json()["cover"]["strategy"] == "NONE"
    conflict = client.patch(
        f"/api/content-drafts/{draft['draft_id']}",
        json={
            "revision": draft["revision"],
            "title": "过期页面",
            "blocks": [{"type": "text", "text": "不会覆盖", "position": 0}],
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "DRAFT_REVISION_CONFLICT"
    assert conflict.get_json()["server_draft"]["title"] == "保存成功"

    v2_document = {
        "schema_version": 2,
        "title": "HTTP v2",
        "source_fidelity": "NATIVE",
        "blocks": [
            {
                "kind": "paragraph",
                "block_id": "http-body",
                "children": [{"kind": "text", "text": "富文本正文"}],
            }
        ],
    }
    v2_created = client.post(
        "/api/content-drafts",
        json={
            "title": "HTTP v2",
            "content_schema_version": 2,
            "document": v2_document,
        },
    )
    assert v2_created.status_code == 201
    v2_payload = v2_created.get_json()
    assert v2_payload["content_schema_version"] == 2
    assert v2_payload["document"]["title"] == "HTTP v2"
    v2_old_patch = client.patch(
        f"/api/content-drafts/{v2_payload['draft_id']}",
        json={
            "revision": v2_payload["revision"],
            "title": "旧页面",
            "blocks": [{"type": "text", "text": "覆盖", "position": 0}],
        },
    )
    assert v2_old_patch.status_code == 409
    assert v2_old_patch.get_json()["error"] == "DRAFT_CONTENT_SCHEMA_CONFLICT"
    v2_after = client.get(f"/api/content-drafts/{v2_payload['draft_id']}").get_json()
    assert v2_after["revision"] == v2_payload["revision"]
    assert v2_after["document"] == v2_payload["document"]

    content_state = app.extensions["content_studio"]
    content_state.close()
    account_state.close()


class FakeAccountState:
    def __init__(self, accounts, delivery) -> None:
        self.accounts = accounts
        self.delivery = delivery
        self.auto_execute = False


class CapturingDelivery:
    def __init__(self) -> None:
        self.calls = []
        self.operation_status = "QUEUED"

    async def request_delivery(self, request, _access, **kwargs):
        from account_sessions.errors import ConfirmationRequiredError

        self.calls.append((request, kwargs))
        if request.mode == "PUBLISH" and not request.confirmation_token:
            raise ConfirmationRequiredError(
                "per-target-token",
                "2099-01-01T00:00:00+00:00",
                {"title": request.article.title},
            )
        return {
            "operation_id": str(uuid.uuid4()),
            "status": "QUEUED",
        }

    async def get_operation(self, operation_id, _access):
        return {
            "operation_id": operation_id,
            "status": self.operation_status,
            "error_message": (
                "服务中断时平台操作正在执行，请人工核对平台结果"
                if self.operation_status == "RESULT_UNKNOWN"
                else None
            ),
        }


class PartiallyFailingDelivery(CapturingDelivery):
    async def request_delivery(self, request, _access, **kwargs):
        self.calls.append((request, kwargs))
        if len(self.calls) == 1:
            raise RuntimeError("token=secret-value platform request failed")
        return {"operation_id": str(uuid.uuid4()), "status": "QUEUED"}


def test_plan_requires_batch_draft_and_per_publish_confirmation(tmp_path: Path) -> None:
    from content_studio.web import ContentStudioRuntimeState

    account_db = AccountDatabase(sqlite_url(tmp_path / "plan-accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = CapturingDelivery()
    fake_state = FakeAccountState(accounts, delivery)
    service = make_service(tmp_path, account_service=accounts)

    async def seed_accounts_and_plan():
        await accounts.initialize()
        inserted = []
        for index, mode in enumerate(("DRAFT", "PUBLISH")):
            account = PlatformAccount(
                account_id=str(uuid.uuid4()),
                platform="xiaoheihe",
                platform_user_id=f"u-{index}",
                display_name=f"账号{index}",
                profile_path=str(tmp_path / f"profile-{index}"),
                status="ACTIVE",
                session_status="VALID",
                persist_login=True,
            )
            async with account_db.session() as session:
                session.add(account)
            inserted.append((account, mode))
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(
                title="多目标",
                blocks=[{"type": "text", "text": "相同冻结内容", "position": 0}],
            )
        )
        targeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=draft["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": mode,
                    }
                    for account, mode in inserted
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        plan = await service.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )
        return draft, targeted, plan

    draft, targeted, plan = run(seed_accounts_and_plan())
    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service

    with pytest.raises(DraftBatchConfirmationRequiredError):
        run(
            state.execute_plan(
                plan["plan_id"],
                __import__(
                    "content_studio.contracts", fromlist=["ExecuteDeliveryPlanRequest"]
                ).ExecuteDeliveryPlanRequest(),
                LOCAL_WEB_CONTEXT,
            )
        )

    from content_studio.contracts import ExecuteDeliveryPlanRequest

    first = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
            LOCAL_WEB_CONTEXT,
        )
    )
    draft_target = next(target for target in first["targets"] if target["mode"] == "DRAFT")
    public_target = next(target for target in first["targets"] if target["mode"] == "PUBLISH")
    assert draft_target["status"] == "QUEUED"
    assert public_target["status"] == "CONFIRMATION_REQUIRED"
    assert public_target["confirmation_token"] == "per-target-token"
    assert delivery.calls[0][1]["frozen_content_hash"] == plan["content_version"]
    assert delivery.calls[0][1]["content_reference"]
    assert delivery.calls[0][1]["content_reference"] != plan["content_version"]
    assert "content_blocks" not in delivery.calls[0][1]
    assert "images" not in delivery.calls[0][1]

    second = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(
                draft_batch_confirmed=True,
                target_ids=[public_target["target_id"]],
                confirmations={public_target["target_id"]: "per-target-token"},
            ),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert (
        next(target for target in second["targets"] if target["mode"] == "PUBLISH")["status"]
        == "QUEUED"
    )

    changed = run(
        service.patch_draft(
            draft["draft_id"],
            PatchDraftRequest(
                revision=targeted["revision"],
                title="内容已修改",
                blocks=[{"type": "text", "text": "新正文", "position": 0}],
            ),
        )
    )
    assert changed["revision"] > plan["draft_revision"]
    with pytest.raises(DeliveryPlanStaleError):
        run(
            service.get_plan_execution_context(
                plan["plan_id"],
                LOCAL_WEB_CONTEXT,
            )
        )

    run(service.database.dispose())
    run(account_db.dispose())


def test_account_operation_keeps_only_content_version_reference(tmp_path: Path) -> None:
    account_db = AccountDatabase(sqlite_url(tmp_path / "reference-accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="reference-user",
            display_name="引用账号",
            profile_path=str(tmp_path / "reference-profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        from account_sessions.contracts import DeliveryRequest
        from account_sessions.delivery_service import DeliveryService

        delivery = DeliveryService(accounts, public_publish_enabled=False)
        operation = await delivery.request_delivery(
            DeliveryRequest.model_validate(
                {
                    "article": {"title": "引用标题", "body": "内部摘要"},
                    "platform": "xiaoheihe",
                    "account_id": account.account_id,
                    "mode": "DRAFT",
                }
            ),
            LOCAL_WEB_CONTEXT,
            frozen_content_hash="a" * 64,
            content_reference="a" * 64,
        )
        async with account_db.session() as session:
            stored = await session.get(DeliveryOperation, operation["operation_id"])
            assert stored.content_reference == "a" * 64
            assert stored.title == "引用标题"
            assert stored.body == "[Content Studio content reference]"
            assert not hasattr(stored, "content_blocks_json")
            assert not hasattr(stored, "images_json")
        await account_db.dispose()

    run(scenario())


def test_unexpected_target_failure_does_not_block_remaining_targets(tmp_path: Path) -> None:
    from content_studio.contracts import ExecuteDeliveryPlanRequest
    from content_studio.web import ContentStudioRuntimeState

    account_db = AccountDatabase(sqlite_url(tmp_path / "partial-accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = PartiallyFailingDelivery()
    fake_state = FakeAccountState(accounts, delivery)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        targets = []
        for index, platform in enumerate(("xiaoheihe", "zol")):
            account = PlatformAccount(
                account_id=str(uuid.uuid4()),
                platform=platform,
                platform_user_id=f"partial-{index}",
                display_name=f"部分失败账号{index}",
                profile_path=str(tmp_path / f"partial-profile-{index}"),
                status="ACTIVE",
                session_status="VALID",
                persist_login=True,
            )
            async with account_db.session() as session:
                session.add(account)
            targets.append(
                {
                    "platform": platform,
                    "account_id": account.account_id,
                    "mode": "DRAFT",
                }
            )
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(
                title="部分失败",
                blocks=[{"type": "text", "text": "正文", "position": 0}],
            )
        )
        targeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(revision=draft["revision"], targets=targets),
            LOCAL_WEB_CONTEXT,
        )
        return await service.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )

    plan = run(scenario())
    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service
    result = run(
        state.execute_plan(
            plan["plan_id"],
            ExecuteDeliveryPlanRequest(draft_batch_confirmed=True),
            LOCAL_WEB_CONTEXT,
        )
    )
    assert [target["status"] for target in result["targets"]] == ["FAILED", "QUEUED"]
    assert result["status"] == "PARTIAL_FAIL"
    assert "secret-value" not in result["targets"][0]["error_message"]
    assert len(delivery.calls) == 2
    run(service.database.dispose())
    run(account_db.dispose())


def test_docx_mixed_text_and_image_keeps_all_blocks(tmp_path: Path) -> None:
    import io

    from docx import Document
    from docx.shared import Inches
    from PIL import Image

    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image_buffer, format="PNG")
    image_path = tmp_path / "inline.png"
    image_path.write_bytes(image_buffer.getvalue())
    document = Document()
    document.core_properties.title = "独立文档标题"
    paragraph = document.add_paragraph()
    paragraph.add_run("图片之前")
    paragraph.add_run().add_picture(str(image_path), width=Inches(0.2))
    paragraph.add_run("图片之后")
    docx_buffer = io.BytesIO()
    document.save(docx_buffer)

    service = make_service(tmp_path)

    async def scenario():
        await service.initialize()
        draft = await service.import_docx(docx_buffer.getvalue(), "mixed.docx")
        await service.database.dispose()
        return draft

    draft = run(scenario())
    assert [block["type"] for block in draft["blocks"]] == ["text", "image", "text"]
    assert [block.get("text") for block in draft["blocks"] if block["type"] == "text"] == [
        "图片之前",
        "图片之后",
    ]


def test_missing_asset_returns_controlled_error(tmp_path: Path) -> None:
    from content_studio.errors import ContentAssetError

    store = AssetStore(tmp_path / "missing-assets")
    with pytest.raises(ContentAssetError, match="图片文件不存在"):
        store.resolve(str(tmp_path / "missing-assets" / "gone.png"))


def test_plan_target_request_is_idempotent_and_running_becomes_unknown(
    tmp_path: Path,
) -> None:
    from content_studio.contracts import ExecuteDeliveryPlanRequest
    from content_studio.web import ContentStudioRuntimeState

    account_db = AccountDatabase(sqlite_url(tmp_path / "idempotent-accounts.db"))
    accounts = AccountSessionService(account_db, seed_legacy_profiles=False)
    delivery = CapturingDelivery()
    fake_state = FakeAccountState(accounts, delivery)
    service = make_service(tmp_path, account_service=accounts)

    async def scenario():
        await accounts.initialize()
        account = PlatformAccount(
            account_id=str(uuid.uuid4()),
            platform="xiaoheihe",
            platform_user_id="idempotent-user",
            display_name="幂等账号",
            profile_path=str(tmp_path / "idempotent-profile"),
            status="ACTIVE",
            session_status="VALID",
            persist_login=True,
        )
        async with account_db.session() as session:
            session.add(account)
        await service.initialize()
        draft = await service.create_draft(
            CreateDraftRequest(
                title="幂等计划",
                blocks=[{"type": "text", "text": "正文", "position": 0}],
            )
        )
        targeted = await service.replace_targets(
            draft["draft_id"],
            ReplaceTargetsRequest(
                revision=draft["revision"],
                targets=[
                    {
                        "platform": "xiaoheihe",
                        "account_id": account.account_id,
                        "mode": "DRAFT",
                    }
                ],
            ),
            LOCAL_WEB_CONTEXT,
        )
        return await service.create_delivery_plan(
            draft["draft_id"], targeted["revision"], LOCAL_WEB_CONTEXT
        )

    plan = run(scenario())
    state = ContentStudioRuntimeState.__new__(ContentStudioRuntimeState)
    state.account_state = fake_state
    state.service = service
    request = ExecuteDeliveryPlanRequest(draft_batch_confirmed=True)
    first = run(state.execute_plan(plan["plan_id"], request, LOCAL_WEB_CONTEXT))
    second = run(state.execute_plan(plan["plan_id"], request, LOCAL_WEB_CONTEXT))
    assert len(delivery.calls) == 1
    assert first["targets"][0]["operation_id"] == second["targets"][0]["operation_id"]

    recoverable = run(
        service.list_recoverable_plan_operations(
            delivery,
            LOCAL_WEB_CONTEXT,
        )
    )
    assert recoverable == [
        {
            "plan_id": plan["plan_id"],
            "target_id": first["targets"][0]["target_id"],
            "operation_id": first["targets"][0]["operation_id"],
        }
    ]
    run(
        service.set_plan_target_result(
            plan["plan_id"],
            first["targets"][0]["target_id"],
            status="RUNNING",
            operation_id=first["targets"][0]["operation_id"],
        )
    )
    delivery.calls.clear()
    delivery.operation_status = "RESULT_UNKNOWN"
    recoverable = run(
        service.list_recoverable_plan_operations(
            delivery,
            LOCAL_WEB_CONTEXT,
        )
    )
    updated = run(service.get_delivery_plan(plan["plan_id"], LOCAL_WEB_CONTEXT))
    assert recoverable == []
    assert updated["targets"][0]["status"] == "RESULT_UNKNOWN"
    assert updated["targets"][0]["error_code"] == "DELIVERY_RESULT_UNKNOWN"
    run(service.database.dispose())
    run(account_db.dispose())
