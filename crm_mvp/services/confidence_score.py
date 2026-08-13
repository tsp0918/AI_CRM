"""客観的クロージング確度スコア。

担当者の自己申告ではなく、既存の証拠強度(QualificationSlot.confidence)・
稟議到達性(gate_engine.path_to_decider)・鮮度(decays_at)・
単一窓口リスク(gate_engine.is_single_threaded)から機械的に算出する。
新しいトラッキングは追加せず、既存モデルの再利用のみで完結させる。

Score = 100 × (0.5×証拠充実度 + 0.3×稟議到達度 + 0.2×鮮度) × 単一窓口係数
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..enums import AccessLevel, BuyingCenterRole, Confidence, Criterion
from .gate_engine import is_single_threaded, path_to_decider

EVIDENCE_WEIGHT = 0.5
GOVERNANCE_WEIGHT = 0.3
FRESHNESS_WEIGHT = 0.2

FRESHNESS_WINDOW_DAYS = 30
SINGLE_THREAD_MULTIPLIER = 0.7

EVIDENCE_DEPTH_BY_CONFIDENCE: dict[Confidence, float] = {
    Confidence.ASSERTED: 0.4,
    Confidence.CORROBORATED: 0.7,
    Confidence.VERIFIED: 1.0,
}

# 決裁者への到達度。到達不可 / assertedのみで到達 / corroborated以上で到達 / 決裁者本人が engaged。
GOVERNANCE_REACH_NONE = 0.0
GOVERNANCE_REACH_ASSERTED_PATH = 0.5
GOVERNANCE_REACH_CORROBORATED_PATH = 0.85
GOVERNANCE_REACH_DECIDER_ENGAGED = 1.0


@dataclass(slots=True)
class ConfidenceScore:
    total: int  # 0-100(丸め済み)
    evidence_depth: float  # 0-1
    governance_reach: float  # 0-1
    freshness: float  # 0-1
    single_threaded: bool
    decider_reachable: bool
    decider_engaged: bool
    missing_criteria: list[Criterion] = field(default_factory=list)
    decaying_soon: list[tuple[Criterion, datetime]] = field(default_factory=list)

    @property
    def multiplier(self) -> float:
        return SINGLE_THREAD_MULTIPLIER if self.single_threaded else 1.0

    @property
    def evidence_points(self) -> float:
        return round(EVIDENCE_WEIGHT * self.evidence_depth * self.multiplier * 100, 1)

    @property
    def governance_points(self) -> float:
        return round(GOVERNANCE_WEIGHT * self.governance_reach * self.multiplier * 100, 1)

    @property
    def freshness_points(self) -> float:
        return round(FRESHNESS_WEIGHT * self.freshness * self.multiplier * 100, 1)

    @property
    def gap_points(self) -> float:
        return round(100 - self.evidence_points - self.governance_points - self.freshness_points, 1)

    @property
    def band(self) -> str:
        """ダッシュボード等でのバッジ色分け。"""
        if self.total >= 80:
            return "high"
        if self.total >= 60:
            return "mid"
        return "low"


def _evidence_depth(slots: dict) -> tuple[float, list[Criterion]]:
    missing: list[Criterion] = []
    total = 0.0
    for criterion in Criterion:
        # Criterion は StrEnum なので、DB 経由でキーがプレーンな str になっていても
        # hash/eq が一致し slots.get(criterion) でそのまま引ける。
        slot = slots.get(criterion)
        if slot is None:
            missing.append(criterion)
            continue
        # DB から読んだ confidence はプレーンな str になりうるため Confidence(...) で正規化する
        # (StrEnum インスタンスでもそのまま同じ結果になる)。
        total += EVIDENCE_DEPTH_BY_CONFIDENCE.get(Confidence(slot.confidence), 0.0)
    return total / len(Criterion), missing


def _governance_reach(nodes: dict, edges: list, roles: dict) -> tuple[float, bool, bool]:
    decider_node_id = next(
        (nid for nid, r in roles.items()
         if BuyingCenterRole.DECIDER.value in (r.get("roles") or [])),
        None,
    )
    if decider_node_id is None:
        return GOVERNANCE_REACH_NONE, False, False

    if path_to_decider(nodes, edges, roles, Confidence.CORROBORATED) is not None:
        decider_engaged = (
            roles.get(decider_node_id, {}).get("access_level") == AccessLevel.ENGAGED
        )
        reach = GOVERNANCE_REACH_DECIDER_ENGAGED if decider_engaged else GOVERNANCE_REACH_CORROBORATED_PATH
        return reach, True, decider_engaged

    if path_to_decider(nodes, edges, roles, Confidence.ASSERTED) is not None:
        return GOVERNANCE_REACH_ASSERTED_PATH, True, False

    return GOVERNANCE_REACH_NONE, False, False


def _freshness(
    slots: dict, now: datetime,
) -> tuple[float, list[tuple[Criterion, datetime]]]:
    filled = [
        (criterion, slots[criterion]) for criterion in Criterion if criterion in slots
    ]
    if not filled:
        return 0.0, []

    decaying_soon: list[tuple[Criterion, datetime]] = []
    fresh_count = 0
    for criterion, slot in filled:
        if slot.decays_at is None:
            fresh_count += 1
            continue
        days_left = (slot.decays_at - now).total_seconds() / 86400
        if days_left > FRESHNESS_WINDOW_DAYS:
            fresh_count += 1
        else:
            decaying_soon.append((criterion, slot.decays_at))

    decaying_soon.sort(key=lambda pair: pair[1])
    return fresh_count / len(filled), decaying_soon


def compute_confidence_score(
    slots: dict, nodes: dict, edges: list, roles: dict,
    now: datetime | None = None,
) -> ConfidenceScore:
    now = now or datetime.now(timezone.utc)

    evidence_depth, missing_criteria = _evidence_depth(slots)
    governance_reach, decider_reachable, decider_engaged = _governance_reach(nodes, edges, roles)
    freshness, decaying_soon = _freshness(slots, now)
    single_threaded = is_single_threaded(roles)
    multiplier = SINGLE_THREAD_MULTIPLIER if single_threaded else 1.0

    raw = (
        EVIDENCE_WEIGHT * evidence_depth
        + GOVERNANCE_WEIGHT * governance_reach
        + FRESHNESS_WEIGHT * freshness
    ) * multiplier

    return ConfidenceScore(
        total=round(raw * 100),
        evidence_depth=evidence_depth,
        governance_reach=governance_reach,
        freshness=freshness,
        single_threaded=single_threaded,
        decider_reachable=decider_reachable,
        decider_engaged=decider_engaged,
        missing_criteria=missing_criteria,
        decaying_soon=decaying_soon,
    )


def score_reasons(score: ConfidenceScore, criterion_labels: dict) -> list[str]:
    """内訳画面向けの説明文(gate_engine.MissingItem.reason と同じ思想)。"""
    reasons: list[str] = []
    if score.missing_criteria:
        names = "、".join(
            criterion_labels.get(c.value, c.value) for c in score.missing_criteria
        )
        reasons.append(f"未入力の評価軸: {names}")
    if score.decaying_soon:
        names = "、".join(
            f"{criterion_labels.get(c.value, c.value)}({d.strftime('%Y-%m-%d')})"
            for c, d in score.decaying_soon
        )
        reasons.append(f"まもなく失効する証拠: {names}")
    if not score.decider_reachable:
        reasons.append("接触済の人物から決裁者へ到達する稟議ルートが確認できない")
    elif not score.decider_engaged:
        reasons.append("決裁者への到達ルートはあるが、決裁者本人とはまだ関係構築できていない")
    if score.single_threaded:
        reasons.append("接触済の人物が1名のみ(シングルスレッド)")
    return reasons
