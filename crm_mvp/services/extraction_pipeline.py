"""取り込みパイプライン。

  IngestionSource 投入
    → ゲート評価で missing を算出
    → missing だけを抽出対象スキーマとして LLM に渡す
    → ExtractedClaim を ExtractionProposal に変換
    → FieldAutonomyPolicy に従って自動適用 or 確認待ちへ
    → ゲート再評価 → 次の一手を1件返す

「毎回すべてを抽出させない」ことが、精度・コスト・提案の関連性の
3点を同時に改善する。AI_TM の required - known = missing と同じ骨格。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from ..enums import (
    AutonomyMode, Confidence, Criterion, ProposalStatus, SourceKind,
)
from ..schemas.extraction import (
    ACQUISITION_PLAYS, CRITERION_SCHEMAS, ExtractedClaim, ExtractionRequest,
    ExtractionResult, ExtractionTarget,
)
from .gate_engine import GateResult, MissingItem

# AI が触ってはいけないフィールド。VERIFIED への昇格と金額の確約は人のみ。
NEVER_AI_FIELDS: frozenset[str] = frozenset({
    "qualification_slot:confidence:verified",
    "engagement:stage",
    "engagement:amount",
    "waiver:approved_by",
})


def build_targets(gate_results: list[GateResult]) -> list[ExtractionTarget]:
    """未充足の項目だけを抽出指示に変換する。"""
    seen: set[tuple[str, str]] = set()
    targets: list[ExtractionTarget] = []
    items: list[MissingItem] = [m for g in gate_results for m in g.missing]
    items.sort(key=lambda m: m.priority, reverse=True)

    for m in items:
        key = (m.target_type, m.field_path)
        if key in seen:
            continue
        seen.add(key)

        schema: dict = {}
        if m.field_path.startswith("criterion:"):
            criterion = Criterion(m.field_path.split(":", 1)[1])
            schema = CRITERION_SCHEMAS.get(criterion, {})

        targets.append(ExtractionTarget(
            target_type=m.target_type,
            field_path=m.field_path,
            description=m.reason,
            json_schema=schema,
            acquisition_hint=m.play,
        ))

    # missing が無い場合でも、関係グラフの更新は常に拾う
    targets.append(ExtractionTarget(
        target_type="graph_edge",
        field_path="approves",
        description="発言から読み取れる承認・上申の関係（誰が誰に上げるか）",
        json_schema={
            "type": "object",
            "properties": {
                "from_name": {"type": "string"},
                "to_name": {"type": "string"},
                "sequence": {"type": "integer"},
            },
        },
    ))
    targets.append(ExtractionTarget(
        target_type="engagement_role",
        field_path="stance",
        description="各参加者の態度（支持・中立・反対）。役割とは別に判定する",
        json_schema={"type": "object",
                     "properties": {"node_name": {"type": "string"},
                                    "stance": {"type": "string"}}},
    ))
    return targets


SYSTEM_PROMPT = """\
あなたは B2B 営業支援システムの抽出エンジンです。
与えられた商談ログから、指定された項目のみを抽出してください。

厳守事項:
1. 指定された targets 以外の項目は抽出しない。
2. すべての抽出結果に evidence_quote（ログ中の実際の発言）を付ける。
   根拠を示せない項目は出力しない。推測で埋めない。
3. 顧客側の発言と、当方の発言を区別する。当方の希望は根拠にならない。
4. 態度(stance)は発言内容から判定し、口調や親しさから判断しない。
5. model_score は「この抽出が正しい確率」であり、
   「その情報が案件にとって良い内容か」ではない。
6. next_question は、この商談で次に確認すべきことを1つだけ返す。
出力は指定された JSON スキーマのみ。前置きも後書きも付けない。
"""


@dataclass(slots=True)
class ApplyOutcome:
    auto_applied: int = 0
    pending: int = 0
    discarded: int = 0


def to_proposals(
    result: ExtractionResult,
    source_id: uuid.UUID,
    engagement_id: uuid.UUID,
    current_values: dict[str, dict] | None = None,
) -> list[dict]:
    """ExtractedClaim を ExtractionProposal のレコード辞書へ変換。"""
    current_values = current_values or {}
    rows: list[dict] = []
    for claim in result.claims:
        key = f"{claim.target_type}:{claim.field_path}"
        if key in NEVER_AI_FIELDS:
            continue
        rows.append({
            "source_id": source_id,
            "engagement_id": engagement_id,
            "target_type": claim.target_type,
            "field_path": claim.field_path,
            "current_value": current_values.get(key),
            "proposed_value": claim.value,
            "model_score": claim.model_score,
            "rationale": claim.rationale,
            "evidence_quote": claim.evidence_quote,
            "evidence_offset": list(claim.evidence_offset)
            if claim.evidence_offset else None,
            "status": ProposalStatus.PENDING,
        })
    return rows


def route_proposals(
    proposals: list[dict],
    policies: dict[tuple[str, str], "FieldAutonomyPolicyLike"],
    *, source_kind: SourceKind | None = None,
) -> ApplyOutcome:
    """自動適用か確認待ちかを振り分ける。

    ここで人が「AIにどこまで任せるか」を数値で決める必要はない。
    フィールドごとの承認率実績が閾値を超えたものから順に自動化されていく。

    ただし CALENDAR_SYNC(Outlook/Teams 等の自動連携)由来の提案は、
    その連携チャネル自体の承認実績がまだ無いため、フィールド側の
    自動適用ポリシーに関わらず常に確認待ちにする。
    """
    outcome = ApplyOutcome()
    force_confirm = source_kind == SourceKind.CALENDAR_SYNC
    for p in proposals:
        policy = policies.get((p["target_type"], p["field_path"]))
        if policy is None:
            p["status"] = ProposalStatus.PENDING
            outcome.pending += 1
            continue
        # `==` を使うこと。DB から読んだ policy.mode はプレーンな str に
        # なるため、`is` 比較だと NEVER_AI が素通りして書き込み禁止
        # フィールドに AI が書ける状態になってしまう(§3.2 違反)。
        if policy.mode == AutonomyMode.NEVER_AI:
            outcome.discarded += 1
            continue
        if force_confirm or not policy.should_auto_apply(p["model_score"]):
            p["status"] = ProposalStatus.PENDING
            outcome.pending += 1
        else:
            p["status"] = ProposalStatus.AUTO_APPLIED
            outcome.auto_applied += 1
    return outcome


def confidence_for_ai_write(existing: Confidence | None) -> Confidence:
    """AI 由来の書き込みは CORROBORATED が上限。

    VERIFIED への昇格は、顧客側の文書・発言を人が添付した場合のみ。
    ここを緩めると、AI が埋めた数字で経営が意思決定する事故が起きる。
    """
    # `==` を使うこと。DB から読んだ既存値はプレーンな str になるため、
    # `is` 比較だと VERIFIED が常に見逃されて CORROBORATED に
    # 黙って降格してしまう(§3.2: VERIFIED は人しか付与できない、の裏側で
    # 「一度 VERIFIED になったものを AI が壊さない」も守るべき不変条件)。
    if existing == Confidence.VERIFIED:
        return Confidence.VERIFIED
    return Confidence.CORROBORATED


def recompute_accept_rate(
    accepted: int, rejected: int, auto_applied: int, auto_reverted: int
) -> tuple[float, int]:
    """承認率の再計算。自動適用後の手動取り消しは不承認として扱う。"""
    total = accepted + rejected + auto_applied + auto_reverted
    if total == 0:
        return 0.0, 0
    ok = accepted + (auto_applied - auto_reverted)
    return max(0.0, ok / total), total


class FieldAutonomyPolicyLike:
    mode: AutonomyMode

    def should_auto_apply(self, model_score: float) -> bool: ...
