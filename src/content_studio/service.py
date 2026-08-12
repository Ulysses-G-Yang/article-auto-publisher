"""草稿、资产、版本冻结与投递计划用例。"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from content_studio.assets import AssetStore, StoredAsset
from content_studio.contracts import (
    ContentBlockInput,
    CreateDraftRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase
from content_studio.errors import (
    ContentAssetError,
    DeliveryPlanNotFoundError,
    DraftNotFoundError,
    DraftRevisionConflictError,
    DraftTargetConflictError,
    DraftValidationError,
)
from content_studio.importers import (
    DocxImportAdapter,
    LegacyDatabaseSource,
    copy_legacy_article,
    public_legacy_article,
)
from content_studio.models import (
    ContentAsset,
    ContentDraft,
    ContentVersion,
    DeliveryPlan,
    DeliveryPlanTarget,
    DraftTarget,
)

SEED_KEY = "articleops:system-seed:smart-toilet:v1"
SEED_TITLE = "凌晨三点，公司的智能马桶开始给我做绩效面谈"
SEED_BODY = """凌晨三点十三分，我被公司的智能马桶叫醒了。

不是比喻。它真的用老板的声音说：“检测到你今天坐了四十七分钟，但有效产出只有两次，转化率偏低。”

我说那不是产出，那是生理活动。

马桶沉默了两秒，屏幕上弹出一张红色折线图：“不要为过程找借口，要为结果找方法。”

随后，它要求我填写《卫生间停留时长异常说明》，并让我从“能力不足”“主观懈怠”“缺乏主人翁意识”三个选项中选择原因。没有“昨晚吃了三份变态辣烤鱼”这个选项。

隔壁蹲位的老王更惨。他刚放了一个屁，天花板上的摄像头就亮起绿灯，广播宣布：“恭喜员工王某完成本季度第一次主动发声。”

老王很激动，申请把这次发声计入周报。

早上八点，老板召集全员开会，宣布智能厕所改革取得阶段性成果：卫生纸消耗下降了百分之三十，员工平均如厕时间减少了十二分钟。

至于为什么大家脸色发绿、走路夹着腿，老板认为这是团队执行力提升后的正常阵痛。

会议最后，公司评选出了“年度最佳奋斗者”：一卷从入职至今从未被使用过的卫生纸。

它的获奖感言只有一句：

“只要不解决问题，就永远不会制造成本。”"""


class ContentStudioService:
    def __init__(
        self,
        database: ContentDatabase,
        *,
        asset_store: AssetStore | None = None,
        legacy_source: LegacyDatabaseSource | None = None,
        docx_importer: DocxImportAdapter | None = None,
        account_service=None,
    ) -> None:
        self.database = database
        self.asset_store = asset_store or AssetStore()
        self.legacy_source = legacy_source or LegacyDatabaseSource()
        self.docx_importer = docx_importer or DocxImportAdapter(self.asset_store)
        self.account_service = account_service

    async def initialize(self) -> None:
        await self.database.initialize()
        await self.ensure_seed_draft()

    async def ensure_seed_draft(self) -> dict:
        async with self.database.session() as session:
            now = _utc_now()
            await session.execute(
                sqlite_insert(ContentDraft)
                .values(
                    draft_id=str(uuid.uuid5(uuid.NAMESPACE_URL, SEED_KEY)),
                    seed_key=SEED_KEY,
                    source_type="SYSTEM_SEED",
                    source_ref=SEED_KEY,
                    title=SEED_TITLE,
                    blocks_json=[
                        {
                            "block_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SEED_KEY}:body")),
                            "type": "text",
                            "text": SEED_BODY,
                            "position": 0,
                        }
                    ],
                    status="ACTIVE",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["seed_key"])
            )
            draft = await session.scalar(
                select(ContentDraft).where(ContentDraft.seed_key == SEED_KEY)
            )
            if draft is None:
                raise RuntimeError("系统种子草稿初始化失败")
            return await self._draft_payload(session, draft)

    async def list_drafts(self, *, limit: int, offset: int) -> dict:
        async with self.database.session() as session:
            total = await session.scalar(select(func.count(ContentDraft.draft_id))) or 0
            rows = list(
                (
                    await session.scalars(
                        select(ContentDraft)
                        .options(selectinload(ContentDraft.targets))
                        .order_by(ContentDraft.updated_at.desc(), ContentDraft.draft_id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            return {
                "drafts": [await self._draft_payload(session, row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    async def get_draft(self, draft_id: str) -> dict:
        async with self.database.session() as session:
            draft = await self._load_draft(session, draft_id)
            return await self._draft_payload(session, draft)

    async def create_draft(
        self,
        request: CreateDraftRequest,
        *,
        source_type: str = "BLANK",
        source_ref: str | None = None,
    ) -> dict:
        blocks = _normalize_blocks(request.blocks)
        _validate_draft_content(request.title, blocks, allow_empty=True)
        async with self.database.session() as session:
            draft = ContentDraft(
                draft_id=str(uuid.uuid4()),
                source_type=source_type,
                source_ref=source_ref,
                title=request.title,
                blocks_json=blocks,
                status="ACTIVE",
                revision=1,
            )
            session.add(draft)
            await session.flush()
            await self._validate_asset_references(session, draft.draft_id, blocks)
            return await self._draft_payload(session, draft)

    async def patch_draft(self, draft_id: str, request: PatchDraftRequest) -> dict:
        blocks = _normalize_blocks(request.blocks)
        _validate_draft_content(request.title, blocks, allow_empty=True)
        async with self.database.session() as session:
            draft = await self._load_draft(session, draft_id)
            await self._validate_asset_references(session, draft_id, blocks)
            result = await session.execute(
                update(ContentDraft)
                .where(
                    ContentDraft.draft_id == draft_id,
                    ContentDraft.revision == request.revision,
                )
                .values(
                    title=request.title,
                    blocks_json=blocks,
                    revision=request.revision + 1,
                    updated_at=_utc_now(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.refresh(draft)
                raise DraftRevisionConflictError(await self._draft_payload(session, draft))
            await session.refresh(draft)
            return await self._draft_payload(session, draft)

    async def import_docx(self, data: bytes, filename: str) -> dict:
        title, blocks, stored_assets = await self.docx_importer.parse(data, filename)
        return await self._create_with_assets(
            title=title,
            blocks=blocks,
            source_type="DOCX",
            source_ref=Path(filename).name[:255],
            assets=stored_assets,
        )

    async def list_legacy_articles(self, *, limit: int, offset: int) -> dict:
        rows, total = await self.legacy_source.list_articles(limit=limit, offset=offset)
        return {
            "articles": [public_legacy_article(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "read_only": True,
        }

    async def create_from_legacy(self, article_id: int) -> dict:
        article, blocks, stored_assets = await copy_legacy_article(
            self.legacy_source,
            self.asset_store,
            article_id,
        )
        return await self._create_with_assets(
            title=article.get("title") or article.get("filename") or "未命名文章",
            blocks=blocks,
            source_type="LEGACY_ARTICLE",
            source_ref=str(article_id),
            assets=stored_assets,
        )

    async def add_asset(self, draft_id: str, data: bytes, filename: str) -> dict:
        stored = self.asset_store.save_image(data, filename)
        try:
            async with self.database.session() as session:
                await self._load_draft(session, draft_id)
                asset = _asset_model(stored, draft_id)
                session.add(asset)
                await session.flush()
                return public_asset(asset)
        except Exception:
            self.asset_store.remove_if_owned(stored.storage_path)
            raise

    async def get_asset(self, asset_id: str) -> tuple[Path, str]:
        async with self.database.session() as session:
            asset = await session.get(ContentAsset, asset_id)
            if asset is None:
                raise ContentAssetError("图片资产不存在")
            return self.asset_store.resolve(asset.storage_path), asset.media_type

    async def replace_targets(
        self,
        draft_id: str,
        request: ReplaceTargetsRequest,
        access,
    ) -> dict:
        account_ids = [item.account_id for item in request.targets]
        if len(account_ids) != len(set(account_ids)):
            raise DraftTargetConflictError("同一草稿不能重复添加同一账号")
        resolved = []
        for item in request.targets:
            account = await self.account_service.require_account(
                item.account_id,
                item.platform,
                access,
                "session.read",
            )
            if account.status != "ACTIVE" or account.session_status != "VALID":
                raise DraftValidationError(f"账号 {account.display_name} 当前不可用于投递")
            resolved.append((item, account))

        try:
            async with self.database.session() as session:
                draft = await self._load_draft(session, draft_id)
                result = await session.execute(
                    update(ContentDraft)
                    .where(
                        ContentDraft.draft_id == draft_id,
                        ContentDraft.revision == request.revision,
                    )
                    .values(
                        revision=request.revision + 1,
                        updated_at=_utc_now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.refresh(draft)
                    raise DraftRevisionConflictError(await self._draft_payload(session, draft))
                await session.execute(delete(DraftTarget).where(DraftTarget.draft_id == draft_id))
                for position, (item, account) in enumerate(resolved):
                    session.add(
                        DraftTarget(
                            target_id=item.target_id or str(uuid.uuid4()),
                            draft_id=draft_id,
                            platform=item.platform,
                            account_id=item.account_id,
                            account_display_name=account.display_name,
                            mode=item.mode,
                            persist_login=(
                                account.persist_login
                                if item.persist_login is None
                                else item.persist_login
                            ),
                            position=position,
                        )
                    )
                await session.flush()
                await session.refresh(draft)
                result_payload = await self._draft_payload(session, draft)
        except IntegrityError as exc:
            raise DraftTargetConflictError("草稿目标重复或已被并发修改") from exc
        # “保持登录态”是账号域策略，不等于登录状态。内容目标成功落库后再按
        # 用户显式值更新账号策略，避免目标事务失败却留下意外的账号设置。
        for item, account in resolved:
            if item.persist_login is not None and item.persist_login != account.persist_login:
                await self.account_service.set_session_policy(
                    item.account_id,
                    item.persist_login,
                    access,
                )
        return result_payload

    async def create_delivery_plan(
        self,
        draft_id: str,
        revision: int,
        access,
    ) -> dict:
        async with self.database.session() as session:
            draft = await self._load_draft(session, draft_id)
            await self._assert_revision(session, draft, revision)
            _validate_draft_content(draft.title, draft.blocks_json, allow_empty=False)
            targets = list(
                (
                    await session.scalars(
                        select(DraftTarget)
                        .where(DraftTarget.draft_id == draft_id)
                        .order_by(DraftTarget.position)
                    )
                ).all()
            )
            if not targets:
                raise DraftValidationError("请至少添加一个投递目标")
            await self._validate_asset_references(session, draft_id, draft.blocks_json)
            for target in targets:
                account = await self.account_service.require_account(
                    target.account_id,
                    target.platform,
                    access,
                    "draft.create" if target.mode == "DRAFT" else "publish.request",
                )
                if account.status != "ACTIVE" or account.session_status != "VALID":
                    raise DraftValidationError(f"账号 {target.account_display_name} 已失效")

            content_hash = _content_hash(draft.title, draft.blocks_json)
            await session.execute(
                sqlite_insert(ContentVersion)
                .values(
                    version_id=str(uuid.uuid4()),
                    draft_id=draft_id,
                    source_revision=draft.revision,
                    content_hash=content_hash,
                    title=draft.title,
                    blocks_json=draft.blocks_json,
                    created_at=_utc_now(),
                )
                .on_conflict_do_nothing(index_elements=["draft_id", "content_hash"])
            )
            version = await session.scalar(
                select(ContentVersion).where(
                    ContentVersion.draft_id == draft_id,
                    ContentVersion.content_hash == content_hash,
                )
            )
            if version is None:
                raise RuntimeError("内容版本冻结失败")

            plan = DeliveryPlan(
                plan_id=str(uuid.uuid4()),
                draft_id=draft_id,
                version_id=version.version_id,
                draft_revision=draft.revision,
                status="READY",
                actor_id=access.actor_id,
                source=access.source,
            )
            session.add(plan)
            for position, target in enumerate(targets):
                session.add(
                    DeliveryPlanTarget(
                        target_id=str(uuid.uuid4()),
                        plan_id=plan.plan_id,
                        source_target_id=target.target_id,
                        platform=target.platform,
                        account_id=target.account_id,
                        account_display_name=target.account_display_name,
                        mode=target.mode,
                        persist_login=target.persist_login,
                        position=position,
                        status="READY",
                    )
                )
            await session.flush()
            return await self._plan_payload(session, plan)

    async def get_delivery_plan(self, plan_id: str) -> dict:
        async with self.database.session() as session:
            plan = await self._load_plan(session, plan_id)
            return await self._plan_payload(session, plan)

    async def get_plan_execution_context(self, plan_id: str) -> tuple[dict, list[dict]]:
        async with self.database.session() as session:
            plan = await self._load_plan(session, plan_id)
            draft = await self._load_draft(session, plan.draft_id)
            if draft.revision != plan.draft_revision:
                from content_studio.errors import DeliveryPlanStaleError

                raise DeliveryPlanStaleError("草稿内容或投递目标已更新，请重新生成投递计划")
            version = await session.get(ContentVersion, plan.version_id)
            targets = list(
                (
                    await session.scalars(
                        select(DeliveryPlanTarget)
                        .where(DeliveryPlanTarget.plan_id == plan_id)
                        .order_by(DeliveryPlanTarget.position)
                    )
                ).all()
            )
            return {
                "plan_id": plan.plan_id,
                "draft_id": plan.draft_id,
                "title": version.title,
                "blocks": version.blocks_json,
                "content_hash": version.content_hash,
                "status": plan.status,
            }, [public_plan_target(target) for target in targets]

    async def build_platform_content(
        self,
        draft_id: str,
        blocks: list[dict],
    ) -> tuple[str, list[dict], list[dict]]:
        """把公开块转换为平台执行所需结构；路径仅留在内部执行单。"""

        asset_ids = {
            block.get("asset_id")
            for block in blocks
            if block.get("type") == "image" and block.get("asset_id")
        }
        async with self.database.session() as session:
            assets = (
                list(
                    (
                        await session.scalars(
                            select(ContentAsset).where(
                                ContentAsset.draft_id == draft_id,
                                ContentAsset.asset_id.in_(asset_ids),
                            )
                        )
                    ).all()
                )
                if asset_ids
                else []
            )
        by_id = {asset.asset_id: asset for asset in assets}
        if set(by_id) != asset_ids:
            raise ContentAssetError("冻结版本引用的图片资产不存在")
        platform_blocks = []
        images = []
        body_parts = []
        for position, raw in enumerate(sorted(blocks, key=lambda item: item.get("position", 0))):
            if raw.get("type") == "text":
                text = str(raw.get("text") or "")
                platform_blocks.append({"type": "text", "text": text, "position": position})
                if text.strip():
                    body_parts.append(text)
                continue
            asset = by_id[raw["asset_id"]]
            path = self.asset_store.resolve(asset.storage_path)
            platform_blocks.append(
                {
                    "type": "image",
                    "local_path": str(path),
                    "position": position,
                }
            )
            images.append(
                {
                    "filename": asset.original_filename,
                    "local_path": str(path),
                    "position_index": position,
                    "width": asset.width or 0,
                    "height": asset.height or 0,
                    "file_size": asset.size_bytes,
                }
            )
        return "\n\n".join(body_parts), platform_blocks, images

    async def resolve_delivery_payload(
        self,
        content_hash: str,
    ) -> tuple[list[dict], list[dict]]:
        """按不可变版本引用解析图文，账号域不保存内容或本机路径副本。"""

        async with self.database.session() as session:
            version = await session.scalar(
                select(ContentVersion).where(ContentVersion.content_hash == content_hash)
            )
            if version is None:
                raise DraftValidationError("投递执行单引用的内容版本不存在")
            draft_id = version.draft_id
            blocks = version.blocks_json
        _body, platform_blocks, images = await self.build_platform_content(
            draft_id,
            blocks,
        )
        return platform_blocks, images

    async def set_plan_target_result(
        self,
        plan_id: str,
        target_id: str,
        *,
        status: str,
        operation_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        async with self.database.session() as session:
            plan = await self._load_plan(session, plan_id)
            target = await session.get(DeliveryPlanTarget, target_id)
            if target is None or target.plan_id != plan_id:
                raise DraftValidationError("投递计划目标不存在")
            target.status = status
            target.operation_id = operation_id
            target.error_code = error_code
            target.error_message = error_message
            target.updated_at = _utc_now()
            await session.flush()
            statuses = list(
                (
                    await session.scalars(
                        select(DeliveryPlanTarget.status).where(
                            DeliveryPlanTarget.plan_id == plan_id
                        )
                    )
                ).all()
            )
            plan.status = _plan_status(statuses)
            plan.updated_at = _utc_now()
            await session.flush()
            return await self._plan_payload(session, plan)

    async def _create_with_assets(
        self,
        *,
        title: str,
        blocks: list[dict],
        source_type: str,
        source_ref: str,
        assets: list[StoredAsset],
    ) -> dict:
        draft_id = str(uuid.uuid4())
        try:
            async with self.database.session() as session:
                draft = ContentDraft(
                    draft_id=draft_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    title=title[:200],
                    blocks_json=blocks,
                    status="ACTIVE",
                    revision=1,
                )
                session.add(draft)
                session.add_all([_asset_model(item, draft_id) for item in assets])
                await session.flush()
                return await self._draft_payload(session, draft)
        except Exception:
            for item in assets:
                self.asset_store.remove_if_owned(item.storage_path)
            raise

    async def _load_draft(self, session, draft_id: str) -> ContentDraft:
        draft = await session.get(ContentDraft, draft_id)
        if draft is None:
            raise DraftNotFoundError("系统草稿不存在")
        return draft

    async def _load_plan(self, session, plan_id: str) -> DeliveryPlan:
        plan = await session.get(DeliveryPlan, plan_id)
        if plan is None:
            raise DeliveryPlanNotFoundError("投递计划不存在")
        return plan

    async def _assert_revision(self, session, draft: ContentDraft, revision: int) -> None:
        if draft.revision != revision:
            raise DraftRevisionConflictError(await self._draft_payload(session, draft))

    async def _validate_asset_references(
        self,
        session,
        draft_id: str,
        blocks: list[dict],
    ) -> None:
        asset_ids = {block.get("asset_id") for block in blocks if block.get("type") == "image"}
        if not asset_ids:
            return
        rows = list(
            (
                await session.scalars(
                    select(ContentAsset).where(
                        ContentAsset.asset_id.in_(asset_ids),
                        ContentAsset.draft_id == draft_id,
                    )
                )
            ).all()
        )
        if {row.asset_id for row in rows} != asset_ids:
            raise ContentAssetError("图片块引用了不属于当前草稿的资产")

    async def _draft_payload(self, session, draft: ContentDraft) -> dict:
        targets = list(
            (
                await session.scalars(
                    select(DraftTarget)
                    .where(DraftTarget.draft_id == draft.draft_id)
                    .order_by(DraftTarget.position)
                )
            ).all()
        )
        return public_draft(draft, targets)

    async def _plan_payload(self, session, plan: DeliveryPlan) -> dict:
        version = await session.get(ContentVersion, plan.version_id)
        targets = list(
            (
                await session.scalars(
                    select(DeliveryPlanTarget)
                    .where(DeliveryPlanTarget.plan_id == plan.plan_id)
                    .order_by(DeliveryPlanTarget.position)
                )
            ).all()
        )
        return public_plan(plan, version, targets)


def public_asset(asset: ContentAsset) -> dict:
    return {
        "asset_id": asset.asset_id,
        "original_filename": asset.original_filename,
        "asset_url": f"/api/content-assets/{asset.asset_id}",
        "media_type": asset.media_type,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "width": asset.width,
        "height": asset.height,
        "created_at": _iso(asset.created_at),
    }


def public_draft(draft: ContentDraft, targets: list[DraftTarget]) -> dict:
    blocks = []
    for raw in sorted(draft.blocks_json or [], key=lambda item: item.get("position", 0)):
        block = {
            "block_id": raw.get("block_id"),
            "type": raw.get("type"),
            "text": raw.get("text"),
            "asset_id": raw.get("asset_id"),
            "asset_url": (
                f"/api/content-assets/{raw['asset_id']}" if raw.get("asset_id") else None
            ),
            "alt": raw.get("alt"),
            "position": raw.get("position", 0),
        }
        blocks.append(block)
    return {
        "draft_id": draft.draft_id,
        "source_type": draft.source_type,
        "source_ref": draft.source_ref,
        "title": draft.title,
        "blocks": blocks,
        "status": draft.status,
        "revision": draft.revision,
        "targets": [public_target(target) for target in targets],
        "created_at": _iso(draft.created_at),
        "updated_at": _iso(draft.updated_at),
    }


def public_target(target: DraftTarget) -> dict:
    return {
        "target_id": target.target_id,
        "platform": target.platform,
        "account_id": target.account_id,
        "account_display_name": target.account_display_name,
        "mode": target.mode,
        "persist_login": target.persist_login,
        "position": target.position,
    }


def public_plan(plan, version, targets) -> dict:
    return {
        "plan_id": plan.plan_id,
        "draft_id": plan.draft_id,
        "content_version": version.content_hash,
        "draft_revision": plan.draft_revision,
        "status": plan.status,
        "targets": [public_plan_target(target) for target in targets],
        "created_at": _iso(plan.created_at),
        "updated_at": _iso(plan.updated_at),
    }


def public_plan_target(target: DeliveryPlanTarget) -> dict:
    return {
        "target_id": target.target_id,
        "platform": target.platform,
        "account_id": target.account_id,
        "account_display_name": target.account_display_name,
        "mode": target.mode,
        "persist_login": target.persist_login,
        "status": target.status,
        "operation_id": target.operation_id,
        "confirmation_required": target.status == "CONFIRMATION_REQUIRED",
        "error_code": target.error_code,
        "error_message": target.error_message,
    }


def _normalize_blocks(blocks: list[ContentBlockInput] | list[dict]) -> list[dict]:
    normalized = []
    seen = set()
    for position, source in enumerate(blocks):
        raw = (
            source.model_dump(exclude_none=True) if hasattr(source, "model_dump") else dict(source)
        )
        block_id = raw.get("block_id") or str(uuid.uuid4())
        if block_id in seen:
            raise DraftValidationError("图文块 block_id 不能重复")
        seen.add(block_id)
        block = {
            "block_id": block_id,
            "type": raw.get("type"),
            "position": position,
        }
        if raw.get("type") == "text":
            block["text"] = str(raw.get("text") or "")
        elif raw.get("type") == "image":
            block["asset_id"] = raw.get("asset_id")
            if raw.get("alt") is not None:
                block["alt"] = raw.get("alt")
        normalized.append(block)
    return normalized


def _validate_draft_content(title: str, blocks: list[dict], *, allow_empty: bool) -> None:
    if allow_empty:
        return
    if not title.strip():
        raise DraftValidationError("文章标题不能为空")
    if not blocks:
        raise DraftValidationError("文章正文不能为空")
    if not any(
        (block.get("type") == "text" and str(block.get("text") or "").strip())
        or block.get("type") == "image"
        for block in blocks
    ):
        raise DraftValidationError("文章正文不能为空")
    total_text = sum(
        len(str(block.get("text") or "")) for block in blocks if block.get("type") == "text"
    )
    if total_text > 200_000:
        raise DraftValidationError("文章正文不能超过 200000 个字符")


def _asset_model(stored: StoredAsset, draft_id: str) -> ContentAsset:
    return ContentAsset(
        asset_id=stored.asset_id,
        draft_id=draft_id,
        original_filename=stored.original_filename,
        storage_path=stored.storage_path,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        width=stored.width,
        height=stored.height,
    )


def _content_hash(title: str, blocks: list[dict]) -> str:
    payload = json.dumps(
        {"title": title, "blocks": blocks},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plan_status(statuses: list[str]) -> str:
    if not statuses or all(status == "READY" for status in statuses):
        return "READY"
    if any(status == "CONFIRMATION_REQUIRED" for status in statuses):
        return "AWAITING_CONFIRMATION"
    active_or_success = {"QUEUED", "RUNNING", "DRAFT_SAVED", "PUBLISHED"}
    failures = {"FAILED", "BLOCKED"}
    if all(status in {"DRAFT_SAVED", "PUBLISHED"} for status in statuses):
        return "SUCCESS"
    if any(status in failures for status in statuses) and any(
        status in active_or_success for status in statuses
    ):
        return "PARTIAL_FAIL"
    if all(status in failures for status in statuses):
        return "FATAL"
    return "EXECUTING"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
