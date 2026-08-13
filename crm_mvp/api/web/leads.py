"""Lead獲得・スコアリングのUI(ロードマップ§7)。

Marketing/Inside Sales が案件化前の見込み客を管理する入口。Touch(接点)を
積むと自動でスコアが再計算され、案件化(コンバージョン)で既存の
Account/Contact/Engagementへ引き継がれる。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import (
    LeadSourceChannel, LeadStatus, SequenceEnrollmentStatus, TouchChannel,
)
from ...models import Account, Lead, Sequence, SequenceDraft, SequenceEnrollment, SequenceStep
from ...services.lead_lifecycle import (
    convert_lead, disqualify_lead, list_touches, maybe_promote_to_mql,
    promote_lead, record_touch,
)
from ...services.lead_scoring import compute_lead_score
from ...services.sequences import dismiss_draft, enroll_lead, mark_draft_reviewed, opt_out_enrollment
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

LEAD_STATUS_LABELS = {
    "new": "新規", "working": "接触中", "mql": "MQL", "sql": "SQL",
    "converted": "案件化済み", "disqualified": "対象外",
}
SOURCE_CHANNEL_LABELS = {
    "inbound": "インバウンド", "outbound": "アウトバウンド", "event": "イベント",
    "referral": "紹介", "content": "コンテンツ", "partner": "パートナー",
}
STEP_CHANNEL_LABELS = {
    "email": "メール", "call_task": "架電タスク", "linkedin": "LinkedIn",
}
TOUCH_CHANNEL_LABELS = {
    "form_submit": "フォーム送信", "content_download": "資料DL",
    "event_attendance": "イベント参加", "call_attempted": "架電(不通)",
    "call_connected": "架電(通話)", "email_open": "メール開封",
    "email_click": "メールクリック", "referral_touch": "紹介",
    "ad_click": "広告クリック",
}
NEXT_STATUS: dict[str, tuple[str, str]] = {
    "new": ("working", "接触を開始する"),
    "working": ("mql", "MQLにする"),
    "mql": ("sql", "SQLにする(AEへ)"),
}


def _pipeline_view(session: Session, tenant_id: uuid.UUID, enrollment: SequenceEnrollment) -> list[dict]:
    """Enrollment 1件分のステップ進捗を可視化用に組み立てる。

    ドラフトが存在するステップは実行済み(現在地は current の1件のみ)、
    order が現在地以下でドラフトが無いものは分岐でスキップされたステップ、
    それより先は未到達として区別する。
    """
    steps = session.execute(
        select(SequenceStep).where(
            SequenceStep.tenant_id == tenant_id, SequenceStep.sequence_id == enrollment.sequence_id,
        ).order_by(SequenceStep.step_order)
    ).scalars().all()
    drafts = session.execute(
        select(SequenceDraft).where(
            SequenceDraft.tenant_id == tenant_id, SequenceDraft.enrollment_id == enrollment.id,
        ).order_by(SequenceDraft.generated_at)
    ).scalars().all()
    draft_by_step_id = {d.step_id: d for d in drafts}

    view = []
    for step in steps:
        draft = draft_by_step_id.get(step.id)
        if draft is not None:
            status = "current" if step.step_order == enrollment.current_step_order else "done"
        elif enrollment.current_step_order is not None and step.step_order <= enrollment.current_step_order:
            status = "skipped"
        else:
            status = "upcoming"
        view.append({"step": step, "draft": draft, "status": status})
    return view


def _score_and_context(session: Session, tenant_id: uuid.UUID, lead: Lead) -> dict:
    account = session.get(Account, lead.matched_account_id) if lead.matched_account_id else None
    touches = list_touches(session, tenant_id, lead.id)
    score = compute_lead_score(lead, account, touches)
    return {
        "score": score, "touches": touches,
        "suggest_mql": maybe_promote_to_mql(lead, score),
    }


@router.get("/ui/leads", response_class=HTMLResponse)
def leads_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    leads = session.execute(
        select(Lead).where(Lead.tenant_id == ui_session.tenant_id)
        .order_by(Lead.updated_at.desc())
    ).scalars().all()

    rows = []
    for lead in leads:
        ctx = _score_and_context(session, ui_session.tenant_id, lead)
        rows.append({"lead": lead, "score": ctx["score"]})

    available_sequences = session.execute(
        select(Sequence).where(
            Sequence.tenant_id == ui_session.tenant_id, Sequence.is_active.is_(True),
        ).order_by(Sequence.name)
    ).scalars().all()

    context = base_context(
        session, ui_session, active_nav="leads", flash=flash, flash_type=flash_type,
    )
    context.update({
        "rows": rows, "lead_status_labels": LEAD_STATUS_LABELS,
        "available_sequences": available_sequences,
    })
    return templates.TemplateResponse(request, "leads.html", context)


@router.post("/ui/leads/bulk-enroll")
async def bulk_enroll_ui(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    form = await request.form()
    lead_ids = form.getlist("lead_ids")
    sequence_id = form.get("sequence_id")

    if not lead_ids:
        return redirect_with_flash("/ui/leads", "Leadを1件以上選択してください", "error")
    if not sequence_id:
        return redirect_with_flash("/ui/leads", "登録先のシーケンスを選択してください", "error")

    sequence = session.get(Sequence, uuid.UUID(sequence_id))
    if sequence is None or sequence.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="sequence not found")

    enrolled, skipped = 0, 0
    for raw_id in lead_ids:
        lead = session.get(Lead, uuid.UUID(raw_id))
        if lead is None or lead.tenant_id != ui_session.tenant_id:
            continue
        try:
            enroll_lead(session, ui_session.tenant_id, lead, sequence, actor=f"human:{ui_session.actor_id}")
            enrolled += 1
        except ValueError:
            skipped += 1

    session.commit()
    message = f"{enrolled}件を「{sequence.name}」に登録しました"
    if skipped:
        message += f"(登録済みのため{skipped}件をスキップ)"
    return redirect_with_flash("/ui/leads", message)


@router.get("/ui/leads/new", response_class=HTMLResponse)
def lead_new_form(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    context = base_context(session, ui_session, active_nav="leads")
    context.update({"source_channels": list(LeadSourceChannel)})
    return templates.TemplateResponse(request, "lead_new.html", context)


@router.post("/ui/leads/new")
def lead_new_submit(
    company_name: str = Form(...),
    full_name: str = Form(...),
    title: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    source_channel: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not company_name.strip() or not full_name.strip():
        return redirect_with_flash(
            "/ui/leads/new", "会社名と氏名を入力してください", "error",
        )

    lead = Lead(
        tenant_id=ui_session.tenant_id, company_name=company_name.strip(),
        full_name=full_name.strip(), title=title.strip() or None,
        email=email.strip() or None, phone=phone.strip() or None,
        source_channel=LeadSourceChannel(source_channel) if source_channel else None,
        owner=ui_session.actor_name, written_by=f"human:{ui_session.actor_id}",
    )
    session.add(lead)
    session.commit()

    return redirect_with_flash(f"/ui/leads/{lead.id}", "Leadを登録しました")


def _get_lead_or_404(session: Session, ui_session: UiSession, lead_id: uuid.UUID) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None or lead.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.get("/ui/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    request: Request,
    lead_id: uuid.UUID,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    ctx = _score_and_context(session, ui_session.tenant_id, lead)
    next_status = NEXT_STATUS.get(lead.status)

    available_sequences = session.execute(
        select(Sequence).where(
            Sequence.tenant_id == ui_session.tenant_id, Sequence.is_active.is_(True),
        ).order_by(Sequence.name)
    ).scalars().all()

    enrollments = session.execute(
        select(SequenceEnrollment).where(
            SequenceEnrollment.tenant_id == ui_session.tenant_id,
            SequenceEnrollment.lead_id == lead.id,
        ).order_by(SequenceEnrollment.created_at.desc())
    ).scalars().all()
    sequence_names = {
        s.id: s.name for s in session.execute(
            select(Sequence).where(Sequence.tenant_id == ui_session.tenant_id)
        ).scalars()
    }
    enrollment_ids = [e.id for e in enrollments]
    drafts = []
    if enrollment_ids:
        drafts = session.execute(
            select(SequenceDraft).where(
                SequenceDraft.tenant_id == ui_session.tenant_id,
                SequenceDraft.enrollment_id.in_(enrollment_ids),
            ).order_by(SequenceDraft.generated_at.desc())
        ).scalars().all()
    pipelines = {
        e.id: _pipeline_view(session, ui_session.tenant_id, e) for e in enrollments
    }

    context = base_context(
        session, ui_session, active_nav="leads", flash=flash, flash_type=flash_type,
    )
    context.update({
        "lead": lead, "score": ctx["score"], "touches": ctx["touches"],
        "suggest_mql": ctx["suggest_mql"], "next_status": next_status,
        "lead_status_labels": LEAD_STATUS_LABELS,
        "source_channel_labels": SOURCE_CHANNEL_LABELS,
        "touch_channel_labels": TOUCH_CHANNEL_LABELS,
        "touch_channels": list(TouchChannel),
        "available_sequences": available_sequences,
        "enrollments": enrollments, "sequence_names": sequence_names,
        "drafts": drafts, "step_channel_labels": STEP_CHANNEL_LABELS,
        "active_enrollment_status": SequenceEnrollmentStatus.ACTIVE,
        "pipelines": pipelines,
    })
    return templates.TemplateResponse(request, "lead_detail.html", context)


@router.post("/ui/leads/{lead_id}/touches")
def add_touch_ui(
    lead_id: uuid.UUID,
    channel: TouchChannel = Form(...),
    note: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    record_touch(
        session, ui_session.tenant_id, lead, channel=channel,
        source_system="manual", actor=ui_session.actor_name,
        raw_payload={"note": note.strip()} if note.strip() else None,
    )
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", "接点を記録しました")


@router.post("/ui/leads/{lead_id}/advance")
def advance_status_ui(
    lead_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    next_status = NEXT_STATUS.get(lead.status)
    if next_status is None:
        return redirect_with_flash(f"/ui/leads/{lead_id}", "これ以上進められません", "error")

    promote_lead(session, lead, LeadStatus(next_status[0]))
    session.commit()
    return redirect_with_flash(
        f"/ui/leads/{lead_id}",
        f"ステータスを「{LEAD_STATUS_LABELS.get(next_status[0], next_status[0])}」にしました",
    )


@router.post("/ui/leads/{lead_id}/convert")
def convert_lead_ui(
    lead_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    try:
        engagement = convert_lead(
            session, ui_session.tenant_id, lead, actor=f"human:{ui_session.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/leads/{lead_id}", str(exc), "error")
    session.commit()

    return redirect_with_flash(
        f"/ui/engagements/{engagement.id}", f"{lead.company_name} を案件化しました",
    )


@router.post("/ui/leads/{lead_id}/disqualify")
def disqualify_lead_ui(
    lead_id: uuid.UUID,
    reason: str = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    if not reason.strip():
        return redirect_with_flash(f"/ui/leads/{lead_id}", "対象外にする理由を入力してください", "error")

    disqualify_lead(session, lead, reason=reason.strip())
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", "対象外にしました")


# --- アウトバウンド・シーケンス(下書き止まり) ---------------------------------

@router.post("/ui/leads/{lead_id}/sequences")
def enroll_sequence_ui(
    lead_id: uuid.UUID,
    sequence_id: uuid.UUID = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    lead = _get_lead_or_404(session, ui_session, lead_id)
    sequence = session.get(Sequence, sequence_id)
    if sequence is None or sequence.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="sequence not found")

    try:
        enroll_lead(
            session, ui_session.tenant_id, lead, sequence,
            actor=f"human:{ui_session.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/leads/{lead_id}", str(exc), "error")
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", f"「{sequence.name}」に登録しました")


def _get_enrollment_or_404(
    session: Session, ui_session: UiSession, lead_id: uuid.UUID, enrollment_id: uuid.UUID,
) -> SequenceEnrollment:
    enrollment = session.execute(
        select(SequenceEnrollment).where(
            SequenceEnrollment.tenant_id == ui_session.tenant_id,
            SequenceEnrollment.id == enrollment_id, SequenceEnrollment.lead_id == lead_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="enrollment not found")
    return enrollment


@router.post("/ui/leads/{lead_id}/sequences/{enrollment_id}/opt-out")
def opt_out_sequence_ui(
    lead_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    enrollment = _get_enrollment_or_404(session, ui_session, lead_id, enrollment_id)
    opt_out_enrollment(session, enrollment)
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", "シーケンスへの登録を解除しました")


def _get_draft_or_404(
    session: Session, ui_session: UiSession, lead_id: uuid.UUID, draft_id: uuid.UUID,
) -> SequenceDraft:
    draft = session.execute(
        select(SequenceDraft).join(
            SequenceEnrollment, SequenceDraft.enrollment_id == SequenceEnrollment.id,
        ).where(
            SequenceDraft.tenant_id == ui_session.tenant_id, SequenceDraft.id == draft_id,
            SequenceEnrollment.lead_id == lead_id,
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return draft


@router.post("/ui/leads/{lead_id}/drafts/{draft_id}/review")
def review_draft_ui(
    lead_id: uuid.UUID,
    draft_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    draft = _get_draft_or_404(session, ui_session, lead_id, draft_id)
    mark_draft_reviewed(draft, reviewed_by=f"human:{ui_session.actor_id}")
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", "レビュー済みにしました")


@router.post("/ui/leads/{lead_id}/drafts/{draft_id}/dismiss")
def dismiss_draft_ui(
    lead_id: uuid.UUID,
    draft_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    draft = _get_draft_or_404(session, ui_session, lead_id, draft_id)
    dismiss_draft(draft, reviewed_by=f"human:{ui_session.actor_id}")
    session.commit()
    return redirect_with_flash(f"/ui/leads/{lead_id}", "このステップは送らないことにしました")
