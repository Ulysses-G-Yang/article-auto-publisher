"""草稿保存证据链（方案一）专项测试。

覆盖：DraftVerificationEvidence 语义、平台异常携带证据、
DeliveryService 持久化到 delivery_operations.verification_evidence、
operation_payload 解码返回。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from platforms.base import (
    DraftBaselineError,
    DraftResultUnknownError,
    DraftVerificationEvidence,
)

# ---------------------------------------------------------------------------
# 证据模型语义
# ---------------------------------------------------------------------------


class TestEvidenceModel:
    def test_success_path(self) -> None:
        ev = DraftVerificationEvidence()
        ev.mark_save_response(status=200, code="0")
        ev.mark_draft_list(match_count=1)
        ev.mark_entity_binding(bound=True, source="save_response_id", id_match=True)
        ev.mark_reopen(title_match=True, dom_blocks_match=True)
        ev.set_draft_url("https://example.com/draft/1")
        ev.finalize()
        payload = ev.to_dict()

        assert payload["save_response_2xx"] is True
        assert payload["save_http_status"] == 200
        assert payload["draft_list_title_unique"] is True
        assert payload["reopen_title_match"] is True
        assert payload["draft_entity_bound"] is True
        assert payload["draft_entity_source"] == "save_response_id"
        assert payload["unknown"] is False
        assert "保存接口已返回 2xx" in payload["summary"]
        assert "唯一匹配" in payload["summary"]

    def test_unknown_with_draft_present(self) -> None:
        """保存响应未捕获但草稿箱有唯一草稿 → 业务人员可看出草稿在。"""
        ev = DraftVerificationEvidence()
        ev.mark_save_response(status=None)
        ev.mark_draft_list(match_count=1)
        ev.finalize(error_code="DRAFT_RESULT_UNKNOWN")
        payload = ev.to_dict()

        assert payload["save_response_2xx"] is None
        assert payload["draft_list_title_unique"] is True
        assert payload["unknown"] is True
        assert "唯一匹配" in payload["summary"]

    def test_ambiguous_draft_list(self) -> None:
        ev = DraftVerificationEvidence()
        ev.mark_save_response(status=500)
        ev.mark_draft_list(match_count=3)
        ev.finalize(error_code="DRAFT_RESULT_UNKNOWN")
        payload = ev.to_dict()

        assert payload["save_response_2xx"] is False
        assert payload["draft_list_title_unique"] is False
        assert payload["draft_list_match_count"] == 3
        assert "匹配数为 3" in payload["summary"]

    def test_entity_binding_false_is_not_title_success(self) -> None:
        ev = DraftVerificationEvidence()
        ev.mark_draft_list(match_count=1)
        ev.mark_entity_binding(
            bound=False,
            source="save_response_id",
            id_match=False,
        )
        payload = ev.finalize(error_code="DRAFT_RESULT_UNKNOWN").to_dict()
        assert payload["draft_list_title_unique"] is True
        assert payload["draft_entity_bound"] is False
        assert payload["draft_entity_id_match"] is False

    def test_markers_latest_wins_per_step(self) -> None:
        ev = DraftVerificationEvidence()
        ev.mark_save_response(status=200)
        ev.mark_save_response(status=204)
        assert ev.save_response_2xx is True
        assert ev.save_http_status == 204

        ev.mark_draft_list(match_count=1)
        ev.mark_reopen(title_match=True, dom_blocks_match=True)
        assert ev.draft_list_title_unique is True
        assert ev.reopen_dom_blocks_match is True

    def test_redaction_no_sensitive_data(self) -> None:
        """summary 不含路径/凭据/正文。"""
        ev = DraftVerificationEvidence()
        ev.mark_save_response(status=200)
        ev.mark_draft_list(match_count=1)
        ev.set_draft_url("https://example.com/draft/42")
        ev.finalize()
        dumped = str(ev.to_dict())
        assert "Cookie" not in dumped
        assert "用户正文" not in dumped


# ---------------------------------------------------------------------------
# 平台异常携带证据
# ---------------------------------------------------------------------------


class TestEvidenceOnException:
    def test_draft_result_unknown_carries_evidence(self) -> None:
        ev = DraftVerificationEvidence()
        ev.mark_draft_list(match_count=1)
        exc = DraftResultUnknownError(
            "DRAFT_RESULT_UNKNOWN: 测试",
            evidence=ev.finalize(error_code="DRAFT_RESULT_UNKNOWN"),
        )
        assert exc.evidence is not None
        assert exc.evidence.to_dict()["draft_list_title_unique"] is True
        assert exc.error_code == "DRAFT_RESULT_UNKNOWN"

    def test_draft_baseline_error_carries_evidence(self) -> None:
        ev = DraftVerificationEvidence()
        exc = DraftBaselineError(
            "DRAFT_BASELINE_UNAVAILABLE: 测试",
            evidence=ev.finalize(error_code="DRAFT_BASELINE_UNAVAILABLE"),
        )
        assert exc.evidence is not None
        assert exc.error_code == "DRAFT_BASELINE_UNAVAILABLE"

    def test_exception_without_evidence_is_none(self) -> None:
        exc = DraftResultUnknownError("DRAFT_RESULT_UNKNOWN: 无证据")
        assert exc.evidence is None


# ---------------------------------------------------------------------------
# DeliveryService 持久化链路
# ---------------------------------------------------------------------------


def test_delivery_operation_persists_and_decodes_evidence(tmp_path) -> None:
    """验证：证据 → 执行单 JSON 列 → payload 解码返回。"""
    from account_sessions.database import AccountDatabase
    from account_sessions.delivery_service import (
        operation_payload,
    )
    from account_sessions.models import DeliveryOperation

    async def scenario() -> dict:
        database = AccountDatabase(f"sqlite+aiosqlite:///{(tmp_path / 'evidence.db').as_posix()}")
        await database.initialize()
        from account_sessions.models import PlatformAccount

        async with database.session() as session:
            account = PlatformAccount(
                account_id="acc-x",
                platform="weibo",
                display_name="测试账号",
                profile_path=str(tmp_path / "profiles" / "weibo" / "acc-x"),
                session_status="VALID",
            )
            session.add(account)
            operation = DeliveryOperation(
                operation_id="op-evidence-1",
                account_id="acc-x",
                platform="weibo",
                mode="DRAFT",
                source="TEST",
                actor_id="tester",
                title="证据测试",
                body="正文",
                content_version="v1",
                account_display_name_snapshot="测试账号",
                status="RESULT_UNKNOWN",
                error_code="DRAFT_RESULT_UNKNOWN",
                error_message="保存响应未捕获",
                verification_evidence=json.dumps(
                    {
                        "save_response_2xx": None,
                        "draft_list_title_unique": True,
                        "draft_list_match_count": 1,
                        "unknown": True,
                        "summary": "草稿箱存在标题唯一匹配的草稿",
                    },
                    ensure_ascii=False,
                ),
                created_at=datetime.now(timezone.utc),
            )
            session.add(operation)
            await session.flush()
            stored_operation = await session.get(DeliveryOperation, "op-evidence-1")
            stored_account = await session.get(PlatformAccount, "acc-x")
            return {
                "raw": stored_operation.verification_evidence,
                "decoded": operation_payload(stored_operation, stored_account)[
                    "verification_evidence"
                ],
            }
        await database.dispose()

    result = asyncio.run(scenario())
    assert "draft_list_title_unique" in result["raw"]
    assert result["decoded"]["draft_list_title_unique"] is True
    assert result["decoded"]["summary"] == "草稿箱存在标题唯一匹配的草稿"


def test_decode_evidence_invalid_returns_none(tmp_path) -> None:
    """无效 JSON 或缺失字段 → None，不破坏 payload。"""
    from account_sessions.delivery_service import _decode_evidence

    assert _decode_evidence(None) is None
    assert _decode_evidence("") is None
    assert _decode_evidence("not-json{") is None
    assert _decode_evidence('"just-a-string"') is None
    assert _decode_evidence('{"a": 1}') == {"a": 1}
