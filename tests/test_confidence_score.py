"""confidence_score.py のユニットテスト。

客観的クロージング確度スコア = 100 × (0.5×証拠充実度 + 0.3×稟議到達度 + 0.2×鮮度) × 単一窓口係数
既存の gate_engine.path_to_decider / is_single_threaded をそのまま再利用している。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from crm_mvp.enums import AccessLevel, BuyingCenterRole, Confidence, Criterion, EdgeType
from crm_mvp.services import confidence_score as cs

from .conftest import make_slot

NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


class TestEvidenceDepth:
    def test_empty_slots_is_zero(self):
        score = cs.compute_confidence_score({}, {}, [], {}, now=NOW)
        assert score.evidence_depth == 0.0
        assert set(score.missing_criteria) == set(Criterion)

    def test_all_verified_is_one(self):
        slots = {
            c: make_slot(c, Confidence.VERIFIED, decays_at=NOW + timedelta(days=90))
            for c in Criterion
        }
        score = cs.compute_confidence_score(slots, {}, [], {}, now=NOW)
        assert score.evidence_depth == 1.0
        assert score.missing_criteria == []

    def test_mixed_confidence_averages_by_weight(self):
        slots = {
            Criterion.IDENTIFIED_PAIN: make_slot(
                Criterion.IDENTIFIED_PAIN, Confidence.ASSERTED,
            ),
            Criterion.TIMING: make_slot(Criterion.TIMING, Confidence.CORROBORATED),
        }
        score = cs.compute_confidence_score(slots, {}, [], {}, now=NOW)
        # (0.4 + 0.7 + 0*8) / 10
        assert score.evidence_depth == pytest.approx((0.4 + 0.7) / 10)
        assert len(score.missing_criteria) == 8


class TestGovernanceReach:
    def _roles_and_edges(self, decider_access: AccessLevel, edge_confidence: Confidence):
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": decider_access,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": edge_confidence,
             "from_node_id": champion, "to_node_id": decider},
        ]
        return roles, edges

    def test_no_decider_role_present_is_zero(self):
        score = cs.compute_confidence_score({}, {}, [], {}, now=NOW)
        assert score.governance_reach == 0.0
        assert score.decider_reachable is False
        assert score.decider_engaged is False

    def test_corroborated_path_with_engaged_decider_is_full_reach(self):
        roles, edges = self._roles_and_edges(AccessLevel.ENGAGED, Confidence.CORROBORATED)
        score = cs.compute_confidence_score({}, {}, edges, roles, now=NOW)
        assert score.governance_reach == cs.GOVERNANCE_REACH_DECIDER_ENGAGED
        assert score.decider_reachable is True
        assert score.decider_engaged is True

    def test_corroborated_path_with_unengaged_decider_is_partial_reach(self):
        roles, edges = self._roles_and_edges(AccessLevel.NONE, Confidence.CORROBORATED)
        score = cs.compute_confidence_score({}, {}, edges, roles, now=NOW)
        assert score.governance_reach == cs.GOVERNANCE_REACH_CORROBORATED_PATH
        assert score.decider_reachable is True
        assert score.decider_engaged is False

    def test_asserted_only_path_is_low_reach(self):
        roles, edges = self._roles_and_edges(AccessLevel.NONE, Confidence.ASSERTED)
        score = cs.compute_confidence_score({}, {}, edges, roles, now=NOW)
        assert score.governance_reach == cs.GOVERNANCE_REACH_ASSERTED_PATH

    def test_unreachable_decider_is_zero(self):
        decider = uuid.uuid4()
        roles = {decider: {"access_level": AccessLevel.NONE,
                            "roles": [BuyingCenterRole.DECIDER.value]}}
        score = cs.compute_confidence_score({}, {}, [], roles, now=NOW)
        assert score.governance_reach == 0.0
        assert score.decider_reachable is False


class TestFreshness:
    def test_no_filled_slots_is_zero(self):
        score = cs.compute_confidence_score({}, {}, [], {}, now=NOW)
        assert score.freshness == 0.0

    def test_slot_without_decay_counts_as_fresh(self):
        slots = {Criterion.BUDGET: make_slot(Criterion.BUDGET, Confidence.ASSERTED)}
        score = cs.compute_confidence_score(slots, {}, [], {}, now=NOW)
        assert score.freshness == 1.0
        assert score.decaying_soon == []

    def test_slot_decaying_within_window_is_flagged(self):
        slots = {
            Criterion.TIMING: make_slot(
                Criterion.TIMING, Confidence.CORROBORATED,
                decays_at=NOW + timedelta(days=10),
            ),
            Criterion.BUDGET: make_slot(
                Criterion.BUDGET, Confidence.CORROBORATED,
                decays_at=NOW + timedelta(days=200),
            ),
        }
        score = cs.compute_confidence_score(slots, {}, [], {}, now=NOW)
        assert score.freshness == 0.5
        assert score.decaying_soon == [(Criterion.TIMING, NOW + timedelta(days=10))]


class TestSingleThreadMultiplier:
    def test_single_threaded_applies_penalty(self):
        slots = {
            c: make_slot(c, Confidence.VERIFIED, decays_at=NOW + timedelta(days=90))
            for c in Criterion
        }
        roles = {uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []}}
        score = cs.compute_confidence_score(slots, {}, [], roles, now=NOW)
        assert score.single_threaded is True
        assert score.multiplier == cs.SINGLE_THREAD_MULTIPLIER

    def test_multi_threaded_has_no_penalty(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []},
            uuid.uuid4(): {"access_level": AccessLevel.CONTACTED, "roles": []},
        }
        score = cs.compute_confidence_score({}, {}, [], roles, now=NOW)
        assert score.single_threaded is False
        assert score.multiplier == 1.0


class TestComposedScoreAndPoints:
    def test_worked_example_matches_kansai_semiconductor_deal(self):
        """§5 のレポートで使った実データ相当のワークドエグザンプル(≒73点)。"""
        champion, finance, decider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": ["champion"]},
            finance: {"access_level": AccessLevel.ENGAGED, "roles": ["finance"]},
            decider: {"access_level": AccessLevel.NONE, "roles": ["decider"]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.CORROBORATED,
             "from_node_id": champion, "to_node_id": finance},
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.CORROBORATED,
             "from_node_id": finance, "to_node_id": decider},
        ]
        far_future = NOW + timedelta(days=200)
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.VERIFIED, decays_at=far_future),
            Criterion.TIMING: make_slot(
                Criterion.TIMING, Confidence.CORROBORATED, decays_at=NOW + timedelta(days=10)),
            Criterion.IDENTIFIED_PAIN: make_slot(
                Criterion.IDENTIFIED_PAIN, Confidence.CORROBORATED, decays_at=far_future),
            Criterion.METRICS: make_slot(
                Criterion.METRICS, Confidence.CORROBORATED, decays_at=far_future),
            Criterion.COMPETITION: make_slot(
                Criterion.COMPETITION, Confidence.CORROBORATED, decays_at=far_future),
            Criterion.DECISION_CRITERIA: make_slot(
                Criterion.DECISION_CRITERIA, Confidence.CORROBORATED, decays_at=far_future),
            Criterion.PAPER_PROCESS: make_slot(
                Criterion.PAPER_PROCESS, Confidence.CORROBORATED, decays_at=far_future),
            Criterion.BUDGET: make_slot(
                Criterion.BUDGET, Confidence.CORROBORATED, decays_at=far_future),
        }

        score = cs.compute_confidence_score(slots, {}, edges, roles, now=NOW)

        assert score.evidence_depth == pytest.approx((7 * 0.7 + 1.0) / 10)
        assert score.governance_reach == cs.GOVERNANCE_REACH_CORROBORATED_PATH
        assert score.freshness == pytest.approx(7 / 8)
        assert score.single_threaded is False
        assert score.total == 73
        assert set(score.missing_criteria) == {Criterion.CHAMPION, Criterion.DECISION_PROCESS}
        assert score.decaying_soon == [(Criterion.TIMING, NOW + timedelta(days=10))]

    def test_points_sum_to_total_plus_gap(self):
        slots = {
            c: make_slot(c, Confidence.CORROBORATED, decays_at=NOW + timedelta(days=90))
            for c in Criterion
        }
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []},
            uuid.uuid4(): {"access_level": AccessLevel.CONTACTED, "roles": []},
        }
        score = cs.compute_confidence_score(slots, {}, [], roles, now=NOW)
        total_from_points = (
            score.evidence_points + score.governance_points
            + score.freshness_points + score.gap_points
        )
        assert round(total_from_points, 1) == 100.0

    def test_band_thresholds(self):
        high = cs.ConfidenceScore(
            total=80, evidence_depth=1, governance_reach=1, freshness=1,
            single_threaded=False, decider_reachable=True, decider_engaged=True,
        )
        mid = cs.ConfidenceScore(
            total=60, evidence_depth=1, governance_reach=1, freshness=1,
            single_threaded=False, decider_reachable=True, decider_engaged=True,
        )
        low = cs.ConfidenceScore(
            total=59, evidence_depth=1, governance_reach=1, freshness=1,
            single_threaded=False, decider_reachable=True, decider_engaged=True,
        )
        assert high.band == "high"
        assert mid.band == "mid"
        assert low.band == "low"


class TestChampionRoleCriterionConsistency:
    def test_flags_when_champion_role_exists_but_criterion_missing(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED,
                           "roles": [BuyingCenterRole.CHAMPION.value]},
        }
        score = cs.compute_confidence_score({}, {}, [], roles, now=NOW)
        assert score.champion_gap is True
        assert Criterion.CHAMPION in score.missing_criteria

    def test_no_gap_when_champion_role_absent(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED,
                           "roles": [BuyingCenterRole.FINANCE.value]},
        }
        score = cs.compute_confidence_score({}, {}, [], roles, now=NOW)
        assert score.champion_gap is False

    def test_no_gap_when_champion_criterion_is_filled(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED,
                           "roles": [BuyingCenterRole.CHAMPION.value]},
        }
        slots = {
            Criterion.CHAMPION: make_slot(Criterion.CHAMPION, Confidence.ASSERTED),
        }
        score = cs.compute_confidence_score(slots, {}, [], roles, now=NOW)
        assert score.champion_gap is False

    def test_score_reasons_gives_specific_message_and_omits_generic_one(self):
        score = cs.ConfidenceScore(
            total=10, evidence_depth=0.0, governance_reach=0.0, freshness=0.0,
            single_threaded=False, decider_reachable=False, decider_engaged=False,
            missing_criteria=[Criterion.CHAMPION], champion_gap=True,
        )
        reasons = cs.score_reasons(score, {"champion": "チャンピオン"})
        assert any("championとしての実質的なコミットメント" in r for r in reasons)
        assert not any(r.startswith("未入力の評価軸") for r in reasons)


class TestScoreReasons:
    def test_lists_missing_and_decaying_and_governance_gaps(self):
        score = cs.ConfidenceScore(
            total=42, evidence_depth=0.5, governance_reach=0.85, freshness=0.5,
            single_threaded=True, decider_reachable=True, decider_engaged=False,
            missing_criteria=[Criterion.CHAMPION],
            decaying_soon=[(Criterion.TIMING, NOW + timedelta(days=10))],
        )
        labels = {"champion": "チャンピオン", "timing": "タイミング"}
        reasons = cs.score_reasons(score, labels)
        assert any("チャンピオン" in r for r in reasons)
        assert any("タイミング" in r for r in reasons)
        assert any("シングルスレッド" in r for r in reasons)
        assert any("決裁者本人とはまだ関係構築" in r for r in reasons)

    def test_no_reasons_when_score_is_clean(self):
        score = cs.ConfidenceScore(
            total=100, evidence_depth=1, governance_reach=1, freshness=1,
            single_threaded=False, decider_reachable=True, decider_engaged=True,
        )
        assert cs.score_reasons(score, {}) == []
