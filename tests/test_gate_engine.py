"""gate_engine.py のユニットテスト。

HANDOVER.md §9 で名指しされている検証対象:
  - path_to_decider / shortest_intro_path / is_single_threaded / derive_close_date
  - 最終交渉ゲートで paper_process と competition の不足を検出し、
    優先度計算により次の一手を1件返すこと
  - 契約発行ゲートで verified 強度の稟議ルート不在により block すること
  - 稟議3階層＋法務レビューからクローズ日を逆算すること
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from crm_mvp.enums import (
    AccessLevel, BuyingCenterRole, Confidence, Criterion, EdgeType,
    GateStrength,
)
from crm_mvp.services import gate_engine as ge
from crm_mvp.services.seed_policies import MANUFACTURING_TEMPLATE

from .conftest import make_slot


def policy_by_code(code: str) -> dict:
    return next(p for p in MANUFACTURING_TEMPLATE if p["code"] == code)


# --- path_to_decider ---------------------------------------------------------

class TestPathToDecider:
    def test_no_deciders_or_no_contacted_returns_none(self):
        assert ge.path_to_decider({}, [], {}) is None

    def test_finds_multi_hop_approves_path(self):
        champion, middle, decider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            middle: {"access_level": AccessLevel.NONE, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.ASSERTED,
             "from_node_id": champion, "to_node_id": middle},
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.ASSERTED,
             "from_node_id": middle, "to_node_id": decider},
        ]
        path = ge.path_to_decider({}, edges, roles)
        assert path == [champion, middle, decider]

    def test_min_confidence_filters_out_weak_edges(self):
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.ASSERTED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        # トポロジー上はつながっているが、ASSERTED は VERIFIED を満たさない
        assert ge.path_to_decider({}, edges, roles,
                                   min_confidence=Confidence.VERIFIED) is None
        # ASSERTED を要求すれば見つかる
        assert ge.path_to_decider({}, edges, roles,
                                   min_confidence=Confidence.ASSERTED) is not None

    def test_uncontacted_champion_cannot_reach_decider(self):
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.NONE, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.VERIFIED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        assert ge.path_to_decider({}, edges, roles) is None


# --- shortest_intro_path ------------------------------------------------------

class TestShortestIntroPath:
    def test_finds_path_through_non_approves_edges(self):
        engaged, hop, target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        roles = {engaged: {"access_level": AccessLevel.ENGAGED}}
        edges = [
            {"edge_type": EdgeType.INFLUENCES,
             "from_node_id": engaged, "to_node_id": hop},
            {"edge_type": EdgeType.REPORTS_TO,
             "from_node_id": hop, "to_node_id": target},
        ]
        path = ge.shortest_intro_path(edges, roles, target)
        assert path == [engaged, hop, target]

    def test_conflicts_with_edges_are_not_traversed(self):
        engaged, target = uuid.uuid4(), uuid.uuid4()
        roles = {engaged: {"access_level": AccessLevel.ENGAGED}}
        edges = [
            {"edge_type": EdgeType.CONFLICTS_WITH,
             "from_node_id": engaged, "to_node_id": target},
        ]
        assert ge.shortest_intro_path(edges, roles, target) is None

    def test_returns_none_when_target_already_engaged(self):
        target = uuid.uuid4()
        roles = {target: {"access_level": AccessLevel.ENGAGED}}
        assert ge.shortest_intro_path([], roles, target) is None

    def test_returns_none_without_engaged_starting_point(self):
        contacted, target = uuid.uuid4(), uuid.uuid4()
        roles = {contacted: {"access_level": AccessLevel.CONTACTED}}
        edges = [{"edge_type": EdgeType.INFLUENCES,
                  "from_node_id": contacted, "to_node_id": target}]
        # CONTACTED は起点にならない（ENGAGED のみ）
        assert ge.shortest_intro_path(edges, roles, target) is None


# --- is_single_threaded -------------------------------------------------------

class TestIsSingleThreaded:
    def test_zero_contacts_is_single_threaded(self):
        assert ge.is_single_threaded({}) is True

    def test_one_contact_is_single_threaded(self):
        roles = {uuid.uuid4(): {"access_level": AccessLevel.ENGAGED}}
        assert ge.is_single_threaded(roles) is True

    def test_two_contacts_is_not_single_threaded(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED},
            uuid.uuid4(): {"access_level": AccessLevel.CONTACTED},
        }
        assert ge.is_single_threaded(roles) is False

    def test_uncontacted_nodes_do_not_count(self):
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED},
            uuid.uuid4(): {"access_level": AccessLevel.NONE},
            uuid.uuid4(): {"access_level": AccessLevel.NONE},
        }
        assert ge.is_single_threaded(roles) is True


# --- derive_close_date --------------------------------------------------------

class TestDeriveCloseDate:
    def test_three_layers_plus_legal_review(self):
        base = datetime(2026, 8, 11)
        result = ge.derive_close_date(
            approval_layers=3, legal_review_days=5, from_date=base,
        )
        assert result == base + timedelta(days=3 * 10 + 5)

    def test_default_lead_time_per_layer_is_ten_days(self):
        base = datetime(2026, 8, 11)
        result = ge.derive_close_date(approval_layers=1, from_date=base)
        assert result == base + timedelta(days=10)

    def test_custom_lead_time_per_layer(self):
        base = datetime(2026, 8, 11)
        result = ge.derive_close_date(
            approval_layers=2, lead_time_per_layer_days=7, from_date=base,
        )
        assert result == base + timedelta(days=14)


# --- MissingItem.priority / GateResult.next_best_action -----------------------

class TestMissingItemPriority:
    def test_priority_is_value_over_cost(self):
        item = ge.MissingItem(
            target_type="x", field_path="y", reason="z",
            information_value=0.8, acquisition_cost=2.0,
        )
        assert item.priority == pytest.approx(0.4)

    def test_zero_cost_is_floored_to_avoid_div_by_zero(self):
        item = ge.MissingItem(
            target_type="x", field_path="y", reason="z",
            information_value=0.5, acquisition_cost=0.0,
        )
        assert item.priority == pytest.approx(0.5 / 0.1)


class TestGateResultNextBestAction:
    def test_returns_none_when_nothing_missing(self):
        result = ge.GateResult(
            policy_code="x", strength=GateStrength.WARN, satisfied=True,
        )
        assert result.next_best_action() is None

    def test_always_returns_exactly_one_item(self):
        low = ge.MissingItem(target_type="a", field_path="a", reason="",
                              information_value=0.1, acquisition_cost=1.0)
        high = ge.MissingItem(target_type="b", field_path="b", reason="",
                               information_value=0.9, acquisition_cost=1.0)
        result = ge.GateResult(
            policy_code="x", strength=GateStrength.WARN, satisfied=False,
            missing=[low, high],
        )
        action = result.next_best_action()
        assert action is high

    def test_blocks_transition_only_for_block_and_require_approval(self):
        missing = [ge.MissingItem(target_type="a", field_path="a", reason="")]
        for strength, blocks in [
            (GateStrength.ADVISORY, False),
            (GateStrength.WARN, False),
            (GateStrength.REQUIRE_APPROVAL, True),
            (GateStrength.BLOCK, True),
        ]:
            result = ge.GateResult(
                policy_code="x", strength=strength, satisfied=False,
                missing=missing,
            )
            assert result.blocks_transition is blocks

        satisfied_result = ge.GateResult(
            policy_code="x", strength=GateStrength.BLOCK, satisfied=True,
        )
        assert satisfied_result.blocks_transition is False


# --- evaluate_gate: 製造業テンプレートを使った統合的な検証 ---------------------

class TestEvaluateGateNegotiation:
    """HANDOVER.md の疎通確認シナリオ: paper_process と competition の不足検出。"""

    def _satisfied_graph(self) -> tuple[list[dict], dict]:
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.CORROBORATED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        return edges, roles

    def test_detects_missing_paper_process_and_competition(self):
        policy = policy_by_code("stage.negotiation")
        edges, roles = self._satisfied_graph()
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.CORROBORATED,
                value={"node_id": "x"},
            ),
        }
        result = ge.evaluate_gate(policy, slots, {}, edges, roles, {})

        assert result.satisfied is False
        missing_fields = {m.field_path for m in result.missing}
        assert missing_fields == {
            "criterion:paper_process", "criterion:competition",
        }
        # graph 条件は満たしているので graph_edge の不足は出ない
        assert all(m.target_type != "graph_edge" for m in result.missing)

    def test_next_best_action_prefers_higher_priority_paper_process(self):
        policy = policy_by_code("stage.negotiation")
        edges, roles = self._satisfied_graph()
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.CORROBORATED,
                value={"node_id": "x"},
            ),
        }
        result = ge.evaluate_gate(policy, slots, {}, edges, roles, {})
        action = result.next_best_action()

        assert action is not None
        assert action.field_path == "criterion:paper_process"

    def test_require_approval_blocks_when_unsatisfied(self):
        policy = policy_by_code("stage.negotiation")
        result = ge.evaluate_gate(policy, {}, {}, [], {}, {})
        assert result.blocks_transition is True


class TestEvaluateGateContract:
    """契約発行ゲート: verified 強度の稟議ルート不在により block する。"""

    def test_blocks_on_missing_verified_path_to_decider(self):
        policy = policy_by_code("artifact.contract")
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        # CORROBORATED しかない -> verified 要求を満たさない
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.CORROBORATED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.VERIFIED,
                value={"node_id": "x"},
            ),
            Criterion.PAPER_PROCESS: make_slot(
                Criterion.PAPER_PROCESS, Confidence.VERIFIED,
                value={"approval_layers": 3},
            ),
        }
        compliance = {
            "anti_social": {"is_fresh": True},
            "credit": {"is_fresh": True},
            "export_control": {"is_fresh": True},
        }
        result = ge.evaluate_gate(policy, slots, {}, edges, roles, compliance)

        assert result.satisfied is False
        assert result.blocks_transition is True
        assert [m.field_path for m in result.missing] == ["approves"]

    def test_satisfied_when_all_verified_and_compliance_fresh(self):
        policy = policy_by_code("artifact.contract")
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.VERIFIED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.VERIFIED,
                value={"node_id": "x"},
            ),
            Criterion.PAPER_PROCESS: make_slot(
                Criterion.PAPER_PROCESS, Confidence.VERIFIED,
                value={"approval_layers": 3},
            ),
        }
        compliance = {
            "anti_social": {"is_fresh": True},
            "credit": {"is_fresh": True},
            "export_control": {"is_fresh": True},
        }
        result = ge.evaluate_gate(policy, slots, {}, edges, roles, compliance)

        assert result.satisfied is True
        assert result.blocks_transition is False

    def test_stale_compliance_blocks_even_with_verified_graph(self):
        policy = policy_by_code("artifact.contract")
        champion, decider = uuid.uuid4(), uuid.uuid4()
        roles = {
            champion: {"access_level": AccessLevel.ENGAGED, "roles": []},
            decider: {"access_level": AccessLevel.NONE,
                      "roles": [BuyingCenterRole.DECIDER.value]},
        }
        edges = [
            {"edge_type": EdgeType.APPROVES, "confidence": Confidence.VERIFIED,
             "from_node_id": champion, "to_node_id": decider},
        ]
        slots = {
            Criterion.ECONOMIC_BUYER: make_slot(
                Criterion.ECONOMIC_BUYER, Confidence.VERIFIED,
                value={"node_id": "x"},
            ),
            Criterion.PAPER_PROCESS: make_slot(
                Criterion.PAPER_PROCESS, Confidence.VERIFIED,
                value={"approval_layers": 3},
            ),
        }
        # anti_social の結果が無い（未取得） -> ブロック
        compliance = {
            "credit": {"is_fresh": True},
            "export_control": {"is_fresh": True},
        }
        result = ge.evaluate_gate(policy, slots, {}, edges, roles, compliance)

        assert result.satisfied is False
        assert "anti_social" in [m.field_path for m in result.missing]


class TestEvaluateGateQualified:
    """入口ゲート(ADVISORY)は不足があっても遷移をブロックしない。"""

    def test_advisory_never_blocks_even_when_unsatisfied(self):
        policy = policy_by_code("stage.qualified")
        result = ge.evaluate_gate(policy, {}, {}, [], {}, {})
        assert result.satisfied is False
        assert result.blocks_transition is False

    def test_satisfied_when_pain_and_timing_asserted(self):
        policy = policy_by_code("stage.qualified")
        slots = {
            Criterion.IDENTIFIED_PAIN: make_slot(
                Criterion.IDENTIFIED_PAIN, Confidence.ASSERTED,
            ),
            Criterion.TIMING: make_slot(Criterion.TIMING, Confidence.ASSERTED),
        }
        result = ge.evaluate_gate(policy, slots, {}, [], {}, {})
        assert result.satisfied is True


class TestEvaluateGateMinEngagedContacts:
    def test_single_threaded_deal_is_flagged_missing(self):
        policy = policy_by_code("stage.proposal")
        engaged = uuid.uuid4()
        roles = {engaged: {"access_level": AccessLevel.ENGAGED, "roles": []}}
        slots = {
            Criterion.METRICS: make_slot(Criterion.METRICS, Confidence.ASSERTED),
            Criterion.DECISION_CRITERIA: make_slot(
                Criterion.DECISION_CRITERIA, Confidence.ASSERTED),
            Criterion.BUDGET: make_slot(Criterion.BUDGET, Confidence.ASSERTED),
        }
        result = ge.evaluate_gate(policy, slots, {}, [], roles, {})
        assert result.satisfied is False
        assert any(m.target_type == "engagement_role" for m in result.missing)

    def test_two_engaged_contacts_satisfies_condition(self):
        policy = policy_by_code("stage.proposal")
        roles = {
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []},
            uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []},
        }
        slots = {
            Criterion.METRICS: make_slot(Criterion.METRICS, Confidence.ASSERTED),
            Criterion.DECISION_CRITERIA: make_slot(
                Criterion.DECISION_CRITERIA, Confidence.ASSERTED),
            Criterion.BUDGET: make_slot(Criterion.BUDGET, Confidence.ASSERTED),
        }
        result = ge.evaluate_gate(policy, slots, {}, [], roles, {})
        assert result.satisfied is True
