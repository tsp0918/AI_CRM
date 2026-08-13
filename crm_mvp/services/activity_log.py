"""案件の活動ログ。

StageTransition・ExtractionProposal(全ステータス)・Waiver・
QualificationSlot の検証記録・IngestionSource を時系列にマージする。
「何によって、いつ、どのように状態が変わったか」を追跡できるように
する目的。ExtractionProposal は PENDING だけでなく accepted/rejected/
auto_applied も保持しているため、それ自体が AI 由来書き込みの監査ログ
として機能する(§3.1 の設計がそのままここで活きる)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ActionItem, Engagement, ExtractionProposal, IngestionSource,
    QualificationSlot, StageTransition, Waiver,
)

PROPOSAL_STATUS_LABELS = {
    "pending": "提案(承認待ち)",
    "accepted": "提案を承認",
    "rejected": "提案を却下",
    "auto_applied": "提案を自動適用",
    "superseded": "提案(差し替え済み)",
}

SOURCE_KIND_LABELS = {
    "transcript": "トランスクリプト", "recording": "録画・録音",
    "email": "メール", "free_note": "自由メモ", "crm_sync": "既存CRM取り込み",
    "calendar_sync": "会議同期(Outlook/Teams)",
}


@dataclass(slots=True)
class ActivityItem:
    occurred_at: datetime
    kind: str  # stage_transition / proposal / waiver / verification / source
    title: str
    detail: str
    actor: str | None = None
    extra: dict = field(default_factory=dict)


def _format_actor(raw: str | uuid.UUID | None) -> str | None:
    if raw is None:
        return None
    s = str(raw)
    if s.startswith("ai:"):
        return "AI"
    if s.startswith("human:"):
        s = s.split(":", 1)[1]
    try:
        # 生の UUID(decided_by / verified_by / approved_by 等)
        uuid.UUID(s)
        return f"人({s[:8]})"
    except ValueError:
        return s


def load_activity_log(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> list[ActivityItem]:
    items: list[ActivityItem] = []

    transitions = session.execute(
        select(StageTransition).where(
            StageTransition.tenant_id == tenant_id,
            StageTransition.engagement_id == engagement.id,
        )
    ).scalars().all()
    for t in transitions:
        detail = f"{t.from_stage or '(新規)'} → {t.to_stage}"
        if t.waiver_id:
            detail += "(例外承認 適用)"
        missing = (t.gate_snapshot or {}).get("missing") or []
        if missing:
            detail += f" / 当時の未充足項目: {', '.join(missing)}"
        items.append(ActivityItem(
            occurred_at=t.occurred_at, kind="stage_transition",
            title="ステージ変更", detail=detail, actor=_format_actor(t.written_by),
        ))

    proposals = session.execute(
        select(ExtractionProposal).where(
            ExtractionProposal.tenant_id == tenant_id,
            ExtractionProposal.engagement_id == engagement.id,
        )
    ).scalars().all()
    for p in proposals:
        detail = f"{p.target_type} / {p.field_path}: {p.proposed_value}"
        if p.rationale:
            detail += f" — {p.rationale}"
        if p.evidence_quote:
            detail += f" / 根拠:「{p.evidence_quote}」"
        if p.status == "rejected" and p.corrected_value:
            detail += f" / 訂正値: {p.corrected_value}"
        if p.rep_comment:
            detail += f" / 担当者コメント: {p.rep_comment}"
        actor = (
            _format_actor(f"human:{p.decided_by}") if p.decided_by
            else ("AI" if p.status in ("auto_applied", "pending") else None)
        )
        items.append(ActivityItem(
            occurred_at=p.decided_at or p.created_at, kind="proposal",
            title=PROPOSAL_STATUS_LABELS.get(p.status, p.status),
            detail=detail, actor=actor,
            extra={"model_score": p.model_score},
        ))

    waivers = session.execute(
        select(Waiver).where(
            Waiver.tenant_id == tenant_id, Waiver.engagement_id == engagement.id,
        )
    ).scalars().all()
    for w in waivers:
        items.append(ActivityItem(
            occurred_at=w.approved_at, kind="waiver", title="例外承認 発行",
            detail=w.reason, actor=_format_actor(w.written_by),
        ))

    slots = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == tenant_id,
            QualificationSlot.engagement_id == engagement.id,
            QualificationSlot.verified_at.is_not(None),
        )
    ).scalars().all()
    for s in slots:
        detail = f"{s.criterion} を検証済みに昇格({s.verification_method})"
        if s.evidence_uri:
            detail += f" / 証跡: {s.evidence_uri}"
        if s.verification_note:
            detail += f" / 備考: {s.verification_note}"
        items.append(ActivityItem(
            occurred_at=s.verified_at, kind="verification", title="VERIFIED 昇格",
            detail=detail, actor=_format_actor(s.verified_by),
        ))

    actions = session.execute(
        select(ActionItem).where(
            ActionItem.tenant_id == tenant_id, ActionItem.engagement_id == engagement.id,
        )
    ).scalars().all()
    for a in actions:
        items.append(ActivityItem(
            occurred_at=a.created_at, kind="action_item", title="次の一手をアサイン",
            detail=f"{a.assigned_to} へ: {a.play or a.reason}",
            actor=_format_actor(a.written_by),
        ))
        if a.completed_at:
            detail = f"{a.assigned_to}: {a.play or a.reason}"
            if a.completed_note:
                detail += f" / 備考: {a.completed_note}"
            items.append(ActivityItem(
                occurred_at=a.completed_at,
                title="アクション完了" if a.status == "done" else "アクション却下",
                kind="action_item", detail=detail,
            ))

    sources = session.execute(
        select(IngestionSource).where(
            IngestionSource.tenant_id == tenant_id,
            IngestionSource.engagement_id == engagement.id,
        )
    ).scalars().all()
    for src in sources:
        preview = (src.raw_text or "").strip().replace("\n", " ")[:100]
        detail = f"{SOURCE_KIND_LABELS.get(src.kind, src.kind)}: {preview}"
        if not src.processed_at:
            detail += "(未処理)"
        items.append(ActivityItem(
            occurred_at=src.created_at, kind="source", title="情報投入",
            detail=detail,
        ))

    items.sort(key=lambda i: i.occurred_at, reverse=True)
    return items
