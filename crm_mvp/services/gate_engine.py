"""ゲート評価エンジン。

`required - known = missing` を計算し、不足をチェックリストではなく
「次の一手」に変換して返す。同じ計算結果がゲート判定と担当者支援の
両方を駆動する = 管理と支援の融合。
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from ..enums import (
    AccessLevel, BuyingCenterRole, Confidence, Criterion, EdgeType,
    GateStrength, Stage,
)
from ..schemas.extraction import ACQUISITION_PLAYS


@dataclass(slots=True)
class MissingItem:
    target_type: str
    field_path: str
    reason: str
    play: str | None = None
    information_value: float = 0.0     # 不確実性の低減量（0-1）
    acquisition_cost: float = 1.0      # 獲得難易度（1が最易）

    @property
    def priority(self) -> float:
        return self.information_value / max(self.acquisition_cost, 0.1)


@dataclass(slots=True)
class GateResult:
    policy_code: str
    strength: GateStrength
    satisfied: bool
    missing: list[MissingItem] = field(default_factory=list)

    @property
    def blocks_transition(self) -> bool:
        return not self.satisfied and self.strength in (
            GateStrength.BLOCK, GateStrength.REQUIRE_APPROVAL
        )

    def next_best_action(self) -> MissingItem | None:
        """常に1件だけ返す。10件出すと担当者は全件を無視する。"""
        if not self.missing:
            return None
        return max(self.missing, key=lambda m: m.priority)


# --- グラフ演算 -------------------------------------------------------------

def path_to_decider(
    nodes: dict[uuid.UUID, dict],
    edges: list[dict],
    roles: dict[uuid.UUID, dict],
    min_confidence: Confidence = Confidence.ASSERTED,
) -> list[uuid.UUID] | None:
    """接触済ノードから決裁者への approves パスを探す。

    パスが存在しなければ「チャンピオンは案件を動かせない」を意味する。
    """
    order = {Confidence.ASSERTED: 1, Confidence.CORROBORATED: 2,
             Confidence.VERIFIED: 3}
    adj: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for e in edges:
        if e["edge_type"] != EdgeType.APPROVES:
            continue
        if order[e["confidence"]] < order[min_confidence]:
            continue
        adj[e["from_node_id"]].append(e["to_node_id"])

    starts = [
        nid for nid, r in roles.items()
        if r["access_level"] in (AccessLevel.CONTACTED, AccessLevel.ENGAGED)
    ]
    deciders = {
        nid for nid, r in roles.items()
        if BuyingCenterRole.DECIDER.value in (r.get("roles") or [])
    }
    if not starts or not deciders:
        return None

    prev: dict[uuid.UUID, uuid.UUID | None] = {s: None for s in starts}
    q = deque(starts)
    while q:
        cur = q.popleft()
        if cur in deciders:
            path = [cur]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            return list(reversed(path))
        for nxt in adj.get(cur, []):
            if nxt not in prev:
                prev[nxt] = cur
                q.append(nxt)
    return None


def shortest_intro_path(
    edges: list[dict],
    roles: dict[uuid.UUID, dict],
    target_node_id: uuid.UUID,
) -> list[uuid.UUID] | None:
    """未接触の重要人物への最短紹介経路。不足の指摘ではなく行動の提示になる。"""
    adj: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for e in edges:
        if e["edge_type"] == EdgeType.CONFLICTS_WITH:
            continue
        adj[e["from_node_id"]].add(e["to_node_id"])
        adj[e["to_node_id"]].add(e["from_node_id"])

    starts = [
        nid for nid, r in roles.items()
        if r["access_level"] == AccessLevel.ENGAGED
    ]
    if not starts or target_node_id in starts:
        return None

    prev: dict[uuid.UUID, uuid.UUID | None] = {s: None for s in starts}
    q = deque(starts)
    while q:
        cur = q.popleft()
        if cur == target_node_id:
            path = [cur]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            return list(reversed(path))
        for nxt in adj.get(cur, ()):
            if nxt not in prev:
                prev[nxt] = cur
                q.append(nxt)
    return None


def is_single_threaded(roles: dict[uuid.UUID, dict]) -> bool:
    """接触ノードが1人だけ = B2B における最頻の失注要因。"""
    touched = [
        r for r in roles.values()
        if r["access_level"] in (AccessLevel.CONTACTED, AccessLevel.ENGAGED)
    ]
    return len(touched) <= 1


def derive_close_date(
    approval_layers: int,
    lead_time_per_layer_days: int = 10,
    legal_review_days: int = 0,
    from_date: datetime | None = None,
) -> datetime:
    """稟議ルートの階層数から現実的なクローズ日を逆算する。

    担当者の希望日ではなく、決裁構造から導かれる日付。
    経営が信用できる着地予測はここから生まれる。
    """
    from datetime import timedelta
    base = from_date or datetime.now()
    days = approval_layers * lead_time_per_layer_days + legal_review_days
    return base + timedelta(days=days)


# --- 評価本体 ---------------------------------------------------------------

INFORMATION_VALUE: dict[Criterion, float] = {
    Criterion.ECONOMIC_BUYER: 1.0,
    Criterion.PAPER_PROCESS: 0.9,
    Criterion.CHAMPION: 0.85,
    Criterion.METRICS: 0.8,
    Criterion.BUDGET: 0.7,
    Criterion.DECISION_CRITERIA: 0.6,
    Criterion.COMPETITION: 0.55,
    Criterion.DECISION_PROCESS: 0.5,
    Criterion.TIMING: 0.4,
    Criterion.IDENTIFIED_PAIN: 0.4,
}

ACQUISITION_COST: dict[Criterion, float] = {
    Criterion.ECONOMIC_BUYER: 2.0,
    Criterion.PAPER_PROCESS: 1.5,
    Criterion.METRICS: 2.5,
    Criterion.BUDGET: 2.0,
    Criterion.CHAMPION: 3.0,
}


def evaluate_gate(
    policy: dict,
    slots: dict[Criterion, "QualificationSlotLike"],
    nodes: dict[uuid.UUID, dict],
    edges: list[dict],
    roles: dict[uuid.UUID, dict],
    compliance: dict[str, dict],
    now: datetime | None = None,
) -> GateResult:
    now = now or datetime.now()
    missing: list[MissingItem] = []

    for cond in policy["conditions"].get("slots", []):
        criterion = Criterion(cond["criterion"])
        required = Confidence(cond.get("min_confidence", "asserted"))
        slot = slots.get(criterion)
        if slot is None or not slot.meets(required, now):
            have = slot.confidence.value if slot else "未入力"
            missing.append(MissingItem(
                target_type="qualification_slot",
                field_path=f"criterion:{criterion.value}",
                reason=f"{criterion.value} が {required.value} に達していない（現在: {have}）",
                play=ACQUISITION_PLAYS.get(criterion),
                information_value=INFORMATION_VALUE.get(criterion, 0.5),
                acquisition_cost=ACQUISITION_COST.get(criterion, 1.0),
            ))

    for cond in policy["conditions"].get("graph", []):
        rule = cond["rule"]
        if rule == "path_to_decider":
            min_conf = Confidence(cond.get("min_confidence", "asserted"))
            if path_to_decider(nodes, edges, roles, min_conf) is None:
                missing.append(MissingItem(
                    target_type="graph_edge",
                    field_path="approves",
                    reason="接触済の人物から決裁者へ到達する稟議ルートが確認できない",
                    play="チャンピオン経由での決裁者への紹介を依頼する",
                    information_value=1.0,
                    acquisition_cost=2.0,
                ))
        elif rule == "min_engaged_contacts":
            engaged = sum(
                1 for r in roles.values()
                if r["access_level"] == AccessLevel.ENGAGED
            )
            if engaged < int(cond["value"]):
                missing.append(MissingItem(
                    target_type="engagement_role",
                    field_path="access_level",
                    reason=f"関係構築済の人物が {engaged} 名（要 {cond['value']} 名）",
                    play="ユーザー部門または品証側にもう1名接点を作る",
                    information_value=0.75,
                    acquisition_cost=1.5,
                ))

    for cond in policy["conditions"].get("compliance", []):
        st = compliance.get(cond["check_type"])
        if cond.get("must_be_fresh") and (st is None or not st.get("is_fresh")):
            missing.append(MissingItem(
                target_type="compliance_status",
                field_path=cond["check_type"],
                reason=f"{cond['check_type']} の確認結果が未取得または期限切れ",
                play="スクリーニングを再実行する（外部連携）",
                information_value=1.0,
                acquisition_cost=0.2,   # ボタン1つで済むので極めて安い
            ))

    return GateResult(
        policy_code=policy["code"],
        strength=GateStrength(policy["strength"]),
        satisfied=not missing,
        missing=missing,
    )


class QualificationSlotLike:
    """型ヒント用のプロトコル代替（models.qualification.QualificationSlot）。"""

    confidence: Confidence

    def meets(self, required: Confidence, now: datetime) -> bool: ...
