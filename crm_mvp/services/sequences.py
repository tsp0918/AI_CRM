"""アウトバウンド・シーケンス自動化基盤(下書き止まり)(ロードマップ§7)。

実際の送信は行わない。ステップ内容を生成してSequenceDraftとして残す
ところまでを実装する。差別化要素として2点:
  - 証拠駆動のパーソナライズ: Touchの履歴から直近の高関心度シグナルを
    拾い、本文に反映する({{recent_signal}})。
  - 反応ベースの分岐: 各ステップは「反応があった場合」「無かった場合」の
    次ステップを個別に設定できる(reaction_channels + on_reaction_next_order
    / on_no_reaction_next_order)。両方未設定なら常定どおり step_order+1。

タイミングモデル: あるステップを生成した後、次のチェックインまでの
待機日数は「そのステップの次に定義されている既定ステップ(step_order+1)」
の delay_days を使う(まだ反応の有無が分からない時点で、分岐先ごとに
別の待機時間を先読みすることはできないため)。既定ステップが存在せず、
分岐先だけが定義されている場合は、生成したステップ自身の delay_days を
次のチェックインまでの待機時間として使う。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import SequenceDraftStatus, SequenceEnrollmentStatus
from ..models import Lead, Sequence, SequenceDraft, SequenceEnrollment, SequenceStep, Touch
from .lead_scoring import HIGH_INTENT_CHANNELS

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
SEQUENCE_END = -1  # on_reaction_next_order / on_no_reaction_next_order の「ここで終了」値


def _recent_signal_note(touches: list[Touch]) -> str | None:
    high_intent = sorted(
        (t for t in touches if t.channel in HIGH_INTENT_CHANNELS),
        key=lambda t: t.occurred_at, reverse=True,
    )
    if not high_intent:
        return None
    t = high_intent[0]
    return f"{t.channel}({t.occurred_at.date()})"


def render_step(
    step: SequenceStep, lead: Lead, touches: list[Touch],
) -> tuple[str | None, str, str | None]:
    """テンプレートを実データで埋める。使えない変数は空文字に落とす
    (未知のプレースホルダーで例外にしない)。"""
    recent_signal = _recent_signal_note(touches)
    context = {
        "full_name": lead.full_name,
        "company_name": lead.company_name,
        "title": lead.title or "",
        "recent_signal": recent_signal or "",
    }

    def substitute(template: str) -> str:
        return _PLACEHOLDER.sub(lambda m: context.get(m.group(1), ""), template)

    subject = substitute(step.subject_template) if step.subject_template else None
    body = substitute(step.body_template)
    note = f"直近の高関心度シグナル: {recent_signal}" if recent_signal else None
    return subject, body, note


def _steps_for(session: Session, tenant_id: uuid.UUID, sequence_id: uuid.UUID) -> list[SequenceStep]:
    return session.execute(
        select(SequenceStep).where(
            SequenceStep.tenant_id == tenant_id, SequenceStep.sequence_id == sequence_id,
        ).order_by(SequenceStep.step_order)
    ).scalars().all()


def create_sequence(
    session: Session, tenant_id: uuid.UUID, *, name: str, description: str | None,
    steps: list[dict], actor: str,
) -> Sequence:
    """steps の各 dict は channel / body_template に加え、任意で
    delay_days / subject_template / reaction_channels /
    on_reaction_next_order / on_no_reaction_next_order を持てる。
    """
    sequence = Sequence(
        tenant_id=tenant_id, name=name, description=description, written_by=actor,
    )
    session.add(sequence)
    session.flush()

    for order, step in enumerate(steps):
        session.add(SequenceStep(
            tenant_id=tenant_id, sequence_id=sequence.id, step_order=order,
            channel=step["channel"], delay_days=step.get("delay_days", 0),
            subject_template=step.get("subject_template"),
            body_template=step["body_template"],
            reaction_channels=step.get("reaction_channels") or [],
            on_reaction_next_order=step.get("on_reaction_next_order"),
            on_no_reaction_next_order=step.get("on_no_reaction_next_order"),
        ))
    session.flush()
    return sequence


def enroll_lead(
    session: Session, tenant_id: uuid.UUID, lead: Lead, sequence: Sequence, *, actor: str,
    now: datetime | None = None,
) -> SequenceEnrollment:
    existing = session.execute(
        select(SequenceEnrollment).where(
            SequenceEnrollment.tenant_id == tenant_id,
            SequenceEnrollment.lead_id == lead.id,
            SequenceEnrollment.sequence_id == sequence.id,
            SequenceEnrollment.status == SequenceEnrollmentStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("このLeadは既にこのシーケンスに登録されています")

    steps = _steps_for(session, tenant_id, sequence.id)
    now = now or datetime.now(timezone.utc)
    first_step = min(steps, key=lambda s: s.step_order) if steps else None
    next_action_at = now + timedelta(days=first_step.delay_days) if first_step else None

    enrollment = SequenceEnrollment(
        tenant_id=tenant_id, sequence_id=sequence.id, lead_id=lead.id,
        current_step_order=None, status=SequenceEnrollmentStatus.ACTIVE,
        next_action_at=next_action_at, written_by=actor,
    )
    session.add(enrollment)
    session.flush()
    return enrollment


def opt_out_enrollment(session: Session, enrollment: SequenceEnrollment) -> SequenceEnrollment:
    enrollment.status = SequenceEnrollmentStatus.OPTED_OUT
    enrollment.next_action_at = None
    session.flush()
    return enrollment


def _has_reaction(
    session: Session, tenant_id: uuid.UUID, lead_id: uuid.UUID,
    channels: list[str], since: datetime,
) -> bool:
    if not channels:
        return False
    row = session.execute(
        select(Touch.id).where(
            Touch.tenant_id == tenant_id, Touch.lead_id == lead_id,
            Touch.channel.in_(channels), Touch.occurred_at >= since,
        ).limit(1)
    ).first()
    return row is not None


def _resolve_target_order(
    session: Session, tenant_id: uuid.UUID, enrollment: SequenceEnrollment,
    steps_by_order: dict[int, SequenceStep], now: datetime,
) -> int:
    """次に生成すべき step_order を決める。反応の有無で分岐する。"""
    if enrollment.current_step_order is None:
        return min(steps_by_order)

    last_step = steps_by_order.get(enrollment.current_step_order)
    if last_step is None:
        return SEQUENCE_END  # ステップ定義が変わった等の異常系

    last_draft = session.execute(
        select(SequenceDraft).where(
            SequenceDraft.tenant_id == tenant_id,
            SequenceDraft.enrollment_id == enrollment.id,
            SequenceDraft.step_id == last_step.id,
        ).order_by(SequenceDraft.generated_at.desc()).limit(1)
    ).scalar_one_or_none()
    since = last_draft.generated_at if last_draft else enrollment.created_at

    reacted = _has_reaction(
        session, tenant_id, enrollment.lead_id, last_step.reaction_channels, since,
    )
    if reacted and last_step.on_reaction_next_order is not None:
        return last_step.on_reaction_next_order
    if not reacted and last_step.on_no_reaction_next_order is not None:
        return last_step.on_no_reaction_next_order
    return last_step.step_order + 1


def generate_due_drafts(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None,
) -> list[SequenceDraft]:
    """next_action_at が到来した有効な Enrollment 全件について、次の
    ステップのドラフトを1件ずつ生成する。実行主体は将来の定期実行に
    差し替え可能(daily_snapshot.py と同じ、手動/cron実行を想定)。"""
    now = now or datetime.now(timezone.utc)

    enrollments = session.execute(
        select(SequenceEnrollment).where(
            SequenceEnrollment.tenant_id == tenant_id,
            SequenceEnrollment.status == SequenceEnrollmentStatus.ACTIVE,
            SequenceEnrollment.next_action_at.is_not(None),
            SequenceEnrollment.next_action_at <= now,
        )
    ).scalars().all()

    drafts: list[SequenceDraft] = []
    for enrollment in enrollments:
        steps = _steps_for(session, tenant_id, enrollment.sequence_id)
        steps_by_order = {s.step_order: s for s in steps}
        if not steps_by_order:
            enrollment.status = SequenceEnrollmentStatus.COMPLETED
            enrollment.completed_at = now
            enrollment.next_action_at = None
            continue

        target_order = _resolve_target_order(session, tenant_id, enrollment, steps_by_order, now)
        step = steps_by_order.get(target_order) if target_order != SEQUENCE_END else None
        if step is None:
            enrollment.status = SequenceEnrollmentStatus.COMPLETED
            enrollment.completed_at = now
            enrollment.next_action_at = None
            continue

        lead = session.get(Lead, enrollment.lead_id)
        touches = session.execute(
            select(Touch).where(
                Touch.tenant_id == tenant_id, Touch.lead_id == lead.id,
            ).order_by(Touch.occurred_at.desc())
        ).scalars().all()

        subject, body, note = render_step(step, lead, touches)
        draft = SequenceDraft(
            tenant_id=tenant_id, enrollment_id=enrollment.id, step_id=step.id,
            generated_at=now, channel=step.channel, subject=subject, body=body,
            personalization_note=note, status=SequenceDraftStatus.DRAFT,
        )
        session.add(draft)
        drafts.append(draft)
        enrollment.current_step_order = step.step_order

        default_next = steps_by_order.get(step.step_order + 1)
        has_branch_targets = (
            step.on_reaction_next_order is not None
            or step.on_no_reaction_next_order is not None
        )
        if default_next is not None:
            enrollment.next_action_at = now + timedelta(days=default_next.delay_days)
        elif has_branch_targets:
            enrollment.next_action_at = now + timedelta(days=step.delay_days)
        else:
            enrollment.status = SequenceEnrollmentStatus.COMPLETED
            enrollment.completed_at = now
            enrollment.next_action_at = None

    session.flush()
    return drafts


def mark_draft_reviewed(
    draft: SequenceDraft, *, reviewed_by: str, now: datetime | None = None,
) -> SequenceDraft:
    draft.status = SequenceDraftStatus.REVIEWED
    draft.reviewed_at = now or datetime.now(timezone.utc)
    draft.reviewed_by = reviewed_by
    return draft


def dismiss_draft(
    draft: SequenceDraft, *, reviewed_by: str, now: datetime | None = None,
) -> SequenceDraft:
    draft.status = SequenceDraftStatus.DISMISSED
    draft.reviewed_at = now or datetime.now(timezone.utc)
    draft.reviewed_by = reviewed_by
    return draft
