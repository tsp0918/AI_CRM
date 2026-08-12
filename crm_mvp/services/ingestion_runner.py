"""IngestionSource → ExtractionProposal までの一連処理
(HANDOVER.md §5 Phase2, item 7,8,9)。

  1. 次のステージゲートの評価から missing を集め、抽出対象を動的生成
     (build_targets)
  2. ExtractorPort で抽出(未接続なら NullExtractor が claims=[] を返す)
  3. to_proposals で ExtractionProposal レコード化(NEVER_AI_FIELDS を除外)
  4. FieldAutonomyPolicy に従い自動適用/保留を振り分け(route_proposals)
  5. 自動適用分は apply_proposal で業務テーブルへ反映する
     (この経路以外から AI が業務テーブルに書き込むことはない — §3.1)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ProposalStatus
from ..models import Engagement, ExtractionProposal, FieldAutonomyPolicy, IngestionSource
from ..ports.extractor import ExtractorPort, NullExtractor
from ..schemas.extraction import ExtractionRequest
from .apply_proposal import apply_proposal
from .extraction_pipeline import build_targets, route_proposals, to_proposals
from .speaker_matching import match_speakers
from .stage_transitions import evaluate_stage_gate, next_stage


@dataclass(slots=True)
class ProcessingOutcome:
    claims: int
    auto_applied: int
    pending: int
    discarded: int
    matched_speakers: int


def _load_policies(
    session: Session, tenant_id: uuid.UUID, proposal_rows: list[dict],
) -> dict[tuple[str, str], FieldAutonomyPolicy]:
    keys = {(r["target_type"], r["field_path"]) for r in proposal_rows}
    if not keys:
        return {}
    rows = session.execute(
        select(FieldAutonomyPolicy).where(FieldAutonomyPolicy.tenant_id == tenant_id)
    ).scalars().all()
    return {
        (p.target_type, p.field_path): p
        for p in rows if (p.target_type, p.field_path) in keys
    }


def process_source(
    session: Session, tenant_id: uuid.UUID, source: IngestionSource,
    extractor: ExtractorPort | None = None,
    speakers: list[dict] | None = None,
) -> ProcessingOutcome:
    """未処理の IngestionSource を1件処理する（同期実行。ワーカーの代替）。

    非同期ワーカー化(outbox パターン)は将来の拡張であり、この関数自体は
    どちらの呼び出し方からも使える形にしてある。
    """
    if source.processed_at is not None:
        raise ValueError(f"source already processed: {source.id}")
    if source.engagement_id is None:
        raise ValueError("source has no engagement_id; nothing to extract against")

    extractor = extractor or NullExtractor()
    engagement = session.get(Engagement, source.engagement_id)
    if engagement is None:
        raise ValueError(f"engagement not found: {source.engagement_id}")

    matched = []
    if speakers:
        matched = match_speakers(session, tenant_id, engagement.account_id, speakers)
        source.participants = [m.to_dict() for m in matched]

    target_stage = next_stage(engagement.stage)
    gate_results = []
    if target_stage is not None:
        _, gate_result = evaluate_stage_gate(
            session, tenant_id, engagement, target_stage,
        )
        gate_results = [gate_result]

    targets = build_targets(gate_results)
    request = ExtractionRequest(
        source_id=source.id, engagement_id=engagement.id,
        transcript=source.raw_text or "", occurred_at=source.occurred_at,
        known_participants=[m.to_dict() for m in matched],
        known_nodes=[],
        targets=targets,
    )
    result = extractor.extract(request)

    proposal_rows = to_proposals(result, source.id, engagement.id)
    for row in proposal_rows:
        row["tenant_id"] = tenant_id
    policies = _load_policies(session, tenant_id, proposal_rows)
    outcome = route_proposals(proposal_rows, policies)

    persisted: list[ExtractionProposal] = []
    for row in proposal_rows:
        proposal = ExtractionProposal(**row)
        session.add(proposal)
        persisted.append(proposal)
    session.flush()

    for proposal in persisted:
        if proposal.status == ProposalStatus.AUTO_APPLIED:
            apply_proposal(session, proposal)

    source.processed_at = datetime.now(timezone.utc)
    source.extractor_version = result.extractor_version
    session.flush()

    return ProcessingOutcome(
        claims=len(result.claims), auto_applied=outcome.auto_applied,
        pending=outcome.pending, discarded=outcome.discarded,
        matched_speakers=len(matched),
    )
