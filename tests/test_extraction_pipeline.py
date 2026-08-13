"""extraction_pipeline.py — §3.1/3.2 の不変条件(AIは業務テーブルに直接書かない、
VERIFIEDは人しか付与できない)を機械的に強制している部分のテスト。
"""

from __future__ import annotations

import uuid

import pytest

from crm_mvp.enums import AutonomyMode, Confidence, ProposalStatus, SourceKind
from crm_mvp.schemas.extraction import ExtractedClaim, ExtractionResult
from crm_mvp.services import extraction_pipeline as ep


def make_claim(target_type: str, field_path: str, **overrides) -> ExtractedClaim:
    defaults = dict(
        target_type=target_type,
        field_path=field_path,
        value={"x": 1},
        model_score=0.9,
        rationale="test",
        evidence_quote="発言の引用",
    )
    defaults.update(overrides)
    return ExtractedClaim(**defaults)


class TestExtractedClaimEvidenceRequired:
    def test_missing_evidence_quote_is_rejected(self):
        with pytest.raises(ValueError):
            make_claim("engagement", "expected_close_date", evidence_quote="")

    def test_whitespace_only_evidence_quote_is_rejected(self):
        with pytest.raises(ValueError):
            make_claim("engagement", "expected_close_date", evidence_quote="   ")


class TestNeverAiFields:
    def test_never_ai_fields_are_dropped_from_proposals(self):
        result = ExtractionResult(
            claims=[
                make_claim("qualification_slot", "confidence:verified"),
                make_claim("engagement", "stage"),
                make_claim("engagement", "amount"),
                make_claim("waiver", "approved_by"),
                make_claim("qualification_slot", "criterion:budget"),
            ],
            extractor_version="test-v1",
        )
        rows = ep.to_proposals(result, uuid.uuid4(), uuid.uuid4())

        written_keys = {f"{r['target_type']}:{r['field_path']}" for r in rows}
        assert written_keys == {"qualification_slot:criterion:budget"}

    def test_never_ai_fields_frozenset_matches_handover_invariant(self):
        # §3.2: VERIFIED への昇格・ステージ変更・金額確約・例外承認は人のみ
        assert ep.NEVER_AI_FIELDS == frozenset({
            "qualification_slot:confidence:verified",
            "engagement:stage",
            "engagement:amount",
            "waiver:approved_by",
        })


class TestConfidenceForAiWrite:
    def test_ai_write_caps_at_corroborated(self):
        assert ep.confidence_for_ai_write(None) is Confidence.CORROBORATED
        assert ep.confidence_for_ai_write(
            Confidence.ASSERTED) is Confidence.CORROBORATED

    def test_existing_verified_is_never_downgraded(self):
        assert ep.confidence_for_ai_write(Confidence.VERIFIED) is Confidence.VERIFIED

    def test_existing_verified_survives_db_round_trip(self):
        # DB から読み戻した値はプレーンな str になる(StrEnum インスタンス
        # ではない)。`is` 比較で判定すると VERIFIED を CORROBORATED に
        # 黙って降格させてしまう回帰を防ぐ。
        assert ep.confidence_for_ai_write("verified") == Confidence.VERIFIED


class _Policy:
    def __init__(self, mode: AutonomyMode, auto: bool = False):
        self.mode = mode
        self._auto = auto

    def should_auto_apply(self, model_score: float) -> bool:
        return self._auto


class TestRouteProposals:
    def test_unknown_field_defaults_to_pending(self):
        proposals = [{"target_type": "x", "field_path": "y", "model_score": 0.9}]
        outcome = ep.route_proposals(proposals, {})
        assert outcome.pending == 1
        assert proposals[0]["status"] == ProposalStatus.PENDING

    def test_never_ai_policy_discards_even_if_it_slipped_through(self):
        proposals = [{"target_type": "engagement", "field_path": "stage",
                      "model_score": 0.99}]
        policies = {("engagement", "stage"): _Policy(AutonomyMode.NEVER_AI)}
        outcome = ep.route_proposals(proposals, policies)
        assert outcome.discarded == 1
        assert outcome.auto_applied == 0

    def test_never_ai_policy_discards_with_db_round_tripped_mode(self):
        # DB から読んだ policy.mode はプレーンな str になる。`is` 比較の
        # 回帰でこれが素通りすると NEVER_AI フィールドに自動適用が
        # 起きてしまう(§3.2 違反)。
        proposals = [{"target_type": "engagement", "field_path": "amount",
                      "model_score": 0.99}]
        policies = {("engagement", "amount"): _Policy("never_ai")}
        outcome = ep.route_proposals(proposals, policies)
        assert outcome.discarded == 1
        assert outcome.auto_applied == 0

    def test_trusted_field_auto_applies(self):
        proposals = [{"target_type": "engagement_role", "field_path": "access_level",
                      "model_score": 0.95}]
        policies = {("engagement_role", "access_level"):
                    _Policy(AutonomyMode.AUTO_IF_TRUSTED, auto=True)}
        outcome = ep.route_proposals(proposals, policies)
        assert outcome.auto_applied == 1
        assert proposals[0]["status"] == ProposalStatus.AUTO_APPLIED

    def test_untrusted_field_stays_pending(self):
        proposals = [{"target_type": "qualification_slot",
                      "field_path": "criterion:champion", "model_score": 0.5}]
        policies = {("qualification_slot", "criterion:champion"):
                    _Policy(AutonomyMode.AUTO_IF_TRUSTED, auto=False)}
        outcome = ep.route_proposals(proposals, policies)
        assert outcome.pending == 1

    def test_calendar_sync_forces_confirm_even_when_field_would_auto_apply(self):
        # Outlook/Teams 連携由来の提案は、フィールド側の実績に関わらず
        # 常に確認待ちにする(連携チャネル自体の承認実績がまだ無いため)。
        proposals = [{"target_type": "engagement_role", "field_path": "access_level",
                      "model_score": 0.95}]
        policies = {("engagement_role", "access_level"):
                    _Policy(AutonomyMode.ALWAYS_AUTO, auto=True)}
        outcome = ep.route_proposals(
            proposals, policies, source_kind=SourceKind.CALENDAR_SYNC,
        )
        assert outcome.pending == 1
        assert outcome.auto_applied == 0
        assert proposals[0]["status"] == ProposalStatus.PENDING

    def test_calendar_sync_still_discards_never_ai_fields(self):
        proposals = [{"target_type": "engagement", "field_path": "stage",
                      "model_score": 0.99}]
        policies = {("engagement", "stage"): _Policy(AutonomyMode.NEVER_AI)}
        outcome = ep.route_proposals(
            proposals, policies, source_kind=SourceKind.CALENDAR_SYNC,
        )
        assert outcome.discarded == 1

    def test_non_calendar_source_kind_is_unaffected(self):
        proposals = [{"target_type": "engagement_role", "field_path": "access_level",
                      "model_score": 0.95}]
        policies = {("engagement_role", "access_level"):
                    _Policy(AutonomyMode.ALWAYS_AUTO, auto=True)}
        outcome = ep.route_proposals(
            proposals, policies, source_kind=SourceKind.TRANSCRIPT,
        )
        assert outcome.auto_applied == 1


class TestRecomputeAcceptRate:
    def test_no_samples_returns_zero(self):
        rate, total = ep.recompute_accept_rate(0, 0, 0, 0)
        assert rate == 0.0
        assert total == 0

    def test_auto_reverted_counts_as_rejection(self):
        # 自動適用10件のうち3件が後で手動取り消し -> 承認扱いは7件
        rate, total = ep.recompute_accept_rate(
            accepted=0, rejected=0, auto_applied=10, auto_reverted=3,
        )
        assert total == 13
        assert rate == pytest.approx(7 / 13)

    def test_rate_never_goes_negative(self):
        rate, _ = ep.recompute_accept_rate(
            accepted=0, rejected=0, auto_applied=1, auto_reverted=5,
        )
        assert rate == 0.0
