"""草稿、资产、版本冻结与投递计划用例。"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from content_studio.assets import AssetStore, StoredAsset
from content_studio.content_document import (
    ContentDocumentValidationError,
    project_to_v1,
    validate_document,
)
from content_studio.contracts import (
    ContentBlockInput,
    CoverInput,
    CreateDraftRequest,
    PatchDraftRequest,
    ReplaceTargetsRequest,
)
from content_studio.database import ContentDatabase
from content_studio.errors import (
    ContentAssetError,
    DeliveryPlanNotFoundError,
    DraftContentSchemaConflictError,
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
        await self._release_interrupted_target_claims()
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
                    cover_strategy="NONE",
                    cover_asset_id=None,
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
        document = None
        if request.content_schema_version == 2:
            document, blocks = _canonicalize_v2_document(request.document, request.title)
        else:
            blocks = _normalize_blocks(request.blocks or [])
        _validate_draft_content(request.title, blocks, allow_empty=True)
        async with self.database.session() as session:
            draft = ContentDraft(
                draft_id=str(uuid.uuid4()),
                source_type=source_type,
                source_ref=source_ref,
                title=request.title,
                blocks_json=blocks,
                content_schema_version=request.content_schema_version,
                document_json=document,
                cover_strategy="NONE",
                cover_asset_id=None,
                status="ACTIVE",
                revision=1,
            )
            session.add(draft)
            await session.flush()
            await self._validate_asset_references(session, draft.draft_id, blocks)
            cover_strategy, cover_asset_id = await self._resolve_requested_cover(
                session,
                draft.draft_id,
                blocks,
                request.cover,
            )
            draft.cover_strategy = cover_strategy
            draft.cover_asset_id = cover_asset_id
            await self._validate_asset_files(
                session,
                draft.draft_id,
                _document_asset_ids(document) | ({cover_asset_id} if cover_asset_id else set()),
            )
            return await self._draft_payload(session, draft)

    async def patch_draft(self, draft_id: str, request: PatchDraftRequest) -> dict:
        async with self.database.session() as session:
            draft = await self._load_draft(session, draft_id)
            requested_schema = request.content_schema_version or 1
            if draft.content_schema_version == 2 and requested_schema != 2:
                raise DraftContentSchemaConflictError(await self._draft_payload(session, draft))
            document = None
            if requested_schema == 2:
                document, blocks = _canonicalize_v2_document(request.document, request.title)
            else:
                if request.blocks is None:
                    raise DraftValidationError("v1 PATCH 必须提供 blocks")
                blocks = _normalize_blocks(request.blocks)
            _validate_draft_content(request.title, blocks, allow_empty=True)
            await self._validate_asset_references(session, draft_id, blocks)
            cover_strategy, cover_asset_id = await self._resolve_patch_cover(
                session,
                draft,
                blocks,
                request,
            )
            result = await session.execute(
                update(ContentDraft)
                .where(
                    ContentDraft.draft_id == draft_id,
                    ContentDraft.revision == request.revision,
                )
                .values(
                    title=request.title,
                    blocks_json=blocks,
                    content_schema_version=requested_schema,
                    document_json=document,
                    cover_strategy=cover_strategy,
                    cover_asset_id=cover_asset_id,
                    revision=request.revision + 1,
                    updated_at=_utc_now(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.refresh(draft)
                raise DraftRevisionConflictError(await self._draft_payload(session, draft))
            await self._validate_asset_files(
                session,
                draft_id,
                _document_asset_ids(document) | ({cover_asset_id} if cover_asset_id else set()),
            )
            await session.refresh(draft)
            return await self._draft_payload(session, draft)

    async def import_docx(self, data: bytes, filename: str) -> dict:
        title, blocks, stored_assets, document = await self.docx_importer.parse_v2(data, filename)
        return await self._create_with_assets(
            title=title,
            blocks=blocks,
            document=document,
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
            if item.persist_login is not None and item.persist_login != account.persist_login:
                raise DraftValidationError(
                    f"账号 {account.display_name} 的保持登录态策略尚未同步，请重试"
                )
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
                            persist_login=account.persist_login,
                            position=position,
                        )
                    )
                await session.flush()
                await session.refresh(draft)
                result_payload = await self._draft_payload(session, draft)
        except IntegrityError as exc:
            raise DraftTargetConflictError("草稿目标重复或已被并发修改") from exc
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
            cover_strategy, cover_asset_id = await self._validate_cover_state(session, draft)
            await self._validate_asset_files(
                session,
                draft_id,
                {
                    block.get("asset_id")
                    for block in draft.blocks_json
                    if block.get("type") == "image" and block.get("asset_id")
                }
                | ({cover_asset_id} if cover_asset_id else set()),
            )
            for target in targets:
                account = await self.account_service.require_account(
                    target.account_id,
                    target.platform,
                    access,
                    "draft.create" if target.mode == "DRAFT" else "publish.request",
                )
                if account.status != "ACTIVE" or account.session_status != "VALID":
                    raise DraftValidationError(f"账号 {target.account_display_name} 已失效")

            content_hash = _content_hash(
                draft.title,
                draft.blocks_json,
                cover_strategy,
                cover_asset_id,
            )
            await session.execute(
                sqlite_insert(ContentVersion)
                .values(
                    version_id=str(uuid.uuid4()),
                    draft_id=draft_id,
                    source_revision=draft.revision,
                    content_hash=content_hash,
                    title=draft.title,
                    blocks_json=draft.blocks_json,
                    cover_strategy=cover_strategy,
                    cover_asset_id=cover_asset_id,
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

    async def get_delivery_plan(self, plan_id: str, access) -> dict:
        async with self.database.session() as session:
            plan = await self._load_plan(session, plan_id)
            await self._assert_plan_access(session, plan, access, "logs.read")
            return await self._plan_payload(session, plan)

    async def get_plan_execution_context(
        self,
        plan_id: str,
        access,
    ) -> tuple[dict, list[dict]]:
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
            self._assert_plan_owner(plan, access)
            for target in targets:
                access.require(
                    "draft.create" if target.mode == "DRAFT" else "publish.request",
                    target.account_id,
                )
            return {
                "plan_id": plan.plan_id,
                "draft_id": plan.draft_id,
                "title": version.title,
                "blocks": version.blocks_json,
                "cover": public_cover(version.cover_strategy, version.cover_asset_id),
                "content_hash": version.content_hash,
                "version_id": version.version_id,
                "status": plan.status,
            }, [public_plan_target(target) for target in targets]

    async def claim_plan_target(self, plan_id: str, target_id: str) -> str | None:
        """短租约领取执行单创建权，阻止并发点击生成重复平台操作。"""

        claim_id = str(uuid.uuid4())
        now = _utc_now()
        expires_at = now + timedelta(minutes=2)
        retryable_statuses = {
            "READY",
            "CONFIRMATION_REQUIRED",
            "BLOCKED",
            "FAILED",
            "CREATING",
        }
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryPlanTarget)
                .where(
                    DeliveryPlanTarget.plan_id == plan_id,
                    DeliveryPlanTarget.target_id == target_id,
                    DeliveryPlanTarget.operation_id.is_(None),
                    DeliveryPlanTarget.status.in_(retryable_statuses),
                    or_(
                        DeliveryPlanTarget.execution_claim_id.is_(None),
                        DeliveryPlanTarget.execution_claim_expires_at.is_(None),
                        DeliveryPlanTarget.execution_claim_expires_at <= now,
                    ),
                )
                .values(
                    status="CREATING",
                    execution_claim_id=claim_id,
                    execution_claim_expires_at=expires_at,
                    error_code=None,
                    error_message=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            return claim_id if result.rowcount == 1 else None

    async def list_recoverable_plan_operations(
        self,
        account_delivery,
        access,
    ) -> list[dict]:
        """返回已确认但尚未完成的执行单，供进程重启后安全续跑。"""

        async with self.database.session() as session:
            # RUNNING 可能已在平台产生副作用，绝不能自动重试；明确标记为
            # 结果未知，等待人工在平台侧核对。
            active = list(
                (
                    await session.scalars(
                        select(DeliveryPlanTarget).where(
                            DeliveryPlanTarget.operation_id.is_not(None),
                            DeliveryPlanTarget.status.in_({"QUEUED", "RUNNING"}),
                        )
                    )
                ).all()
            )
            recoverable = []
            for row in active:
                operation = await account_delivery.get_operation(
                    row.operation_id,
                    access,
                )
                if operation["status"] == "QUEUED":
                    row.status = "QUEUED"
                    recoverable.append(
                        {
                            "plan_id": row.plan_id,
                            "target_id": row.target_id,
                            "operation_id": row.operation_id,
                        }
                    )
                    continue
                if operation["status"] == "RESULT_UNKNOWN":
                    row.status = "RESULT_UNKNOWN"
                    row.error_code = "DELIVERY_RESULT_UNKNOWN"
                    row.error_message = operation["error_message"]
                    row.updated_at = _utc_now()
            return recoverable

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
        version_id: str,
    ) -> tuple[str, list[dict], list[dict]]:
        """按不可变版本引用解析图文，账号域不保存内容或本机路径副本。"""

        async with self.database.session() as session:
            version = await session.get(ContentVersion, version_id)
            if version is None:
                raise DraftValidationError("投递执行单引用的内容版本不存在")
            draft_id = version.draft_id
            title = version.title
            blocks = version.blocks_json
        _body, platform_blocks, images = await self.build_platform_content(
            draft_id,
            blocks,
        )
        return title, platform_blocks, images

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
            target.execution_claim_id = None
            target.execution_claim_expires_at = None
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
        document: dict | None = None,
        source_type: str,
        source_ref: str,
        assets: list[StoredAsset],
    ) -> dict:
        draft_id = str(uuid.uuid4())
        schema_version = 1
        try:
            if document is not None:
                document, blocks = _canonicalize_v2_document(document, title)
                schema_version = 2
        except Exception:
            for item in assets:
                self.asset_store.remove_if_owned(item.storage_path)
            raise
        first_cover_asset_id = _first_body_image_asset_id(blocks)
        try:
            async with self.database.session() as session:
                draft = ContentDraft(
                    draft_id=draft_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    title=title[:200],
                    blocks_json=blocks,
                    content_schema_version=schema_version,
                    document_json=document,
                    cover_strategy=(
                        "FIRST_BODY_IMAGE" if first_cover_asset_id else "NONE"
                    ),
                    cover_asset_id=first_cover_asset_id,
                    status="ACTIVE",
                    revision=1,
                )
                session.add(draft)
                session.add_all([_asset_model(item, draft_id) for item in assets])
                await session.flush()
                await self._validate_asset_references(session, draft_id, blocks)
                await self._validate_asset_files(
                    session,
                    draft_id,
                    {
                        block.get("asset_id")
                        for block in blocks
                        if block.get("type") == "image" and block.get("asset_id")
                    },
                )
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

    async def _assert_plan_access(
        self, session, plan: DeliveryPlan, access, capability: str
    ) -> None:
        self._assert_plan_owner(plan, access)
        targets = list(
            (
                await session.scalars(
                    select(DeliveryPlanTarget).where(DeliveryPlanTarget.plan_id == plan.plan_id)
                )
            ).all()
        )
        for target in targets:
            access.require(capability, target.account_id)

    @staticmethod
    def _assert_plan_owner(plan: DeliveryPlan, access) -> None:
        if plan.actor_id != access.actor_id or plan.source != access.source:
            access.require("__plan_owner__", "")

    async def _release_interrupted_target_claims(self) -> None:
        """新进程无法继承内存中的创建动作，启动时释放其短租约。"""

        async with self.database.session() as session:
            await session.execute(
                update(DeliveryPlanTarget)
                .where(
                    DeliveryPlanTarget.status == "CREATING",
                    DeliveryPlanTarget.operation_id.is_(None),
                )
                .values(
                    status="READY",
                    execution_claim_id=None,
                    execution_claim_expires_at=None,
                    updated_at=_utc_now(),
                )
                .execution_options(synchronize_session=False)
            )

    async def _resolve_requested_cover(
        self,
        session,
        draft_id: str,
        blocks: list[dict],
        cover: CoverInput,
    ) -> tuple[str, str | None]:
        if cover.strategy == "NONE":
            return "NONE", None
        if cover.strategy == "FIRST_BODY_IMAGE":
            asset_id = _first_body_image_asset_id(blocks)
            if not asset_id:
                raise DraftValidationError("FIRST_BODY_IMAGE 需要正文中至少有一张图片")
            return "FIRST_BODY_IMAGE", asset_id
        await self._require_owned_asset(session, draft_id, cover.asset_id)
        return "EXPLICIT", cover.asset_id

    async def _resolve_patch_cover(
        self,
        session,
        draft: ContentDraft,
        blocks: list[dict],
        request: PatchDraftRequest,
    ) -> tuple[str, str | None]:
        if "cover" in request.model_fields_set:
            if request.cover is None:
                raise DraftValidationError("cover 不能为 null，请显式选择 NONE")
            return await self._resolve_requested_cover(
                session,
                draft.draft_id,
                blocks,
                request.cover,
            )
        if draft.cover_strategy == "NONE":
            return "NONE", None
        if draft.cover_strategy == "FIRST_BODY_IMAGE":
            asset_id = _first_body_image_asset_id(blocks)
            if not asset_id:
                raise DraftValidationError("FIRST_BODY_IMAGE 需要正文中至少有一张图片")
            return "FIRST_BODY_IMAGE", asset_id
        if draft.cover_strategy == "EXPLICIT":
            await self._require_owned_asset(session, draft.draft_id, draft.cover_asset_id)
            return "EXPLICIT", draft.cover_asset_id
        raise DraftValidationError("草稿封面策略无效")

    async def _validate_cover_state(
        self,
        session,
        draft: ContentDraft,
    ) -> tuple[str, str | None]:
        strategy = draft.cover_strategy
        asset_id = draft.cover_asset_id
        if strategy == "NONE":
            if asset_id:
                raise DraftValidationError("NONE 封面不能引用图片资产")
            return "NONE", None
        if strategy == "FIRST_BODY_IMAGE":
            first_asset_id = _first_body_image_asset_id(draft.blocks_json)
            if not first_asset_id or asset_id != first_asset_id:
                raise DraftValidationError(
                    "FIRST_BODY_IMAGE 与当前正文首图不一致，请重新提交草稿"
                )
            await self._require_owned_asset(session, draft.draft_id, asset_id)
            return "FIRST_BODY_IMAGE", asset_id
        if strategy == "EXPLICIT":
            await self._require_owned_asset(session, draft.draft_id, asset_id)
            return "EXPLICIT", asset_id
        raise DraftValidationError("草稿封面策略无效")

    async def _require_owned_asset(
        self,
        session,
        draft_id: str,
        asset_id: str | None,
    ) -> ContentAsset:
        if not asset_id:
            raise ContentAssetError("封面必须引用图片资产")
        asset = await session.get(ContentAsset, asset_id)
        if asset is None or asset.draft_id != draft_id:
            raise ContentAssetError("封面图片资产不属于当前草稿")
        return asset

    async def _validate_asset_references(
        self,
        session,
        draft_id: str,
        blocks: list[dict],
    ) -> None:
        asset_ids = {
            block.get("asset_id")
            for block in blocks
            if block.get("type") == "image" and block.get("asset_id")
        }
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

    async def _validate_asset_files(
        self,
        session,
        draft_id: str,
        asset_ids: set[str],
    ) -> None:
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
            raise ContentAssetError("封面或图片块引用了不属于当前草稿的资产")
        for row in rows:
            self.asset_store.resolve(row.storage_path)

    async def _draft_payload(self, session, draft: ContentDraft) -> dict:
        _validated_stored_content(draft)
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


def public_cover(strategy: str | None, asset_id: str | None) -> dict:
    normalized_strategy = strategy or "NONE"
    resolved_asset_id = asset_id if normalized_strategy != "NONE" else None
    return {
        "strategy": normalized_strategy,
        "asset_id": resolved_asset_id,
        "asset_url": (
            f"/api/content-assets/{resolved_asset_id}" if resolved_asset_id else None
        ),
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
        "content_schema_version": getattr(draft, "content_schema_version", 1),
        "document": (
            draft.document_json
            if getattr(draft, "content_schema_version", 1) == 2
            else None
        ),
        "blocks": blocks,
        "cover": public_cover(draft.cover_strategy, draft.cover_asset_id),
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
        "cover": public_cover(version.cover_strategy, version.cover_asset_id),
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


def _canonicalize_v2_document(
    document: dict | None,
    title: str,
) -> tuple[dict, list[dict]]:
    """校验 v2 canonical，并只从 document 派生旧版投影。

    ``blocks`` 可能仍由旧客户端同时提交，但 v2 的唯一真值始终是
    ``document``；调用方不能用另一份 blocks 覆盖或改变 canonical 内容。
    """

    if not isinstance(document, dict):
        raise DraftValidationError("v2 草稿缺少 document")
    try:
        canonical = validate_document(document)
    except ContentDocumentValidationError as exc:
        raise DraftValidationError(f"v2 document 校验失败: {exc}") from exc
    if canonical["title"] != title:
        raise DraftValidationError("v2 document.title 必须与请求 title 一致")
    projection = project_to_v1(canonical, omit_title_block=True)
    return canonical, projection.blocks


def _document_asset_ids(document: dict | None) -> set[str]:
    """递归收集 v2 文档内所有受控图片资产引用。"""

    if not document:
        return set()
    asset_ids: set[str] = set()

    def visit_block(block: dict) -> None:
        kind = block.get("kind")
        if kind in {"paragraph", "heading"}:
            for child in block.get("children", []):
                if child.get("kind") == "image" and child.get("asset_id"):
                    asset_ids.add(child["asset_id"])
            return
        if kind == "list":
            for item in block.get("items", []):
                for nested in item.get("blocks", []):
                    visit_block(nested)
            return
        if kind == "table":
            for row in block.get("rows", []):
                for cell in row.get("cells", []):
                    for nested in cell.get("blocks", []):
                        visit_block(nested)

    for block in document.get("blocks", []):
        visit_block(block)
    return asset_ids


def _validated_stored_content(draft: ContentDraft) -> tuple[int, dict | None, list[dict]]:
    """校验数据库中的草稿，不对损坏的 v2 数据自动修复。"""

    schema_version = getattr(draft, "content_schema_version", 1)
    if schema_version == 1:
        if draft.document_json is not None:
            raise DraftValidationError("v1 草稿不应包含 document")
        return 1, None, draft.blocks_json
    if schema_version != 2 or not isinstance(draft.document_json, dict):
        raise DraftValidationError("v2 草稿缺少有效 document")
    canonical, projection = _canonicalize_v2_document(draft.document_json, draft.title)
    if canonical != draft.document_json:
        raise DraftValidationError("v2 草稿 document 未保持 canonical 形式")
    if projection != draft.blocks_json:
        raise DraftValidationError("v2 草稿 blocks_json 与 document projection 不一致")
    return 2, canonical, projection


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


def _first_body_image_asset_id(blocks: list[dict]) -> str | None:
    for block in sorted(blocks, key=lambda item: item.get("position", 0)):
        if block.get("type") == "image" and block.get("asset_id"):
            return str(block["asset_id"])
    return None


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


def _content_hash(
    title: str,
    blocks: list[dict],
    cover_strategy: str = "NONE",
    cover_asset_id: str | None = None,
) -> str:
    payload = json.dumps(
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
