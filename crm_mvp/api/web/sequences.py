"""アウトバウンド・シーケンス自動化のUI(下書き止まり)(ロードマップ§7)。

シーケンステンプレートの作成・一覧、および「本日分のドラフトを生成する」
手動トリガー(daily_snapshot.py と同じ、定期実行の代替としての手動ボタン)。
Leadへの登録・ドラフトのレビューは leads.py 側の画面から行う。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import SequenceEnrollmentStatus, SequenceStepChannel, TouchChannel
from ...models import Sequence, SequenceEnrollment, SequenceStep
from ...services.lead_scoring import HIGH_INTENT_CHANNELS
from ...services.sequences import SEQUENCE_END, create_sequence, generate_due_drafts
from .common import base_context, redirect_with_flash
from .leads import TOUCH_CHANNEL_LABELS
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

STEP_CHANNEL_LABELS = {
    "email": "メール", "call_task": "架電タスク", "linkedin": "LinkedIn",
}

# シーケンス作成フォームは固定5行(JSなしで組める現実的な上限)。
MAX_STEP_ROWS = 5


def _parse_branch_target(raw: str | None) -> int | None:
    if not raw:
        return None
    if raw == "end":
        return SEQUENCE_END
    return int(raw)


@router.get("/ui/sequences", response_class=HTMLResponse)
def sequences_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    sequences = session.execute(
        select(Sequence).where(Sequence.tenant_id == ui_session.tenant_id)
        .order_by(Sequence.created_at.desc())
    ).scalars().all()

    rows = []
    for seq in sequences:
        step_count = session.execute(
            select(func.count()).select_from(SequenceStep).where(
                SequenceStep.tenant_id == ui_session.tenant_id,
                SequenceStep.sequence_id == seq.id,
            )
        ).scalar_one()
        active_count = session.execute(
            select(func.count()).select_from(SequenceEnrollment).where(
                SequenceEnrollment.tenant_id == ui_session.tenant_id,
                SequenceEnrollment.sequence_id == seq.id,
                SequenceEnrollment.status == SequenceEnrollmentStatus.ACTIVE,
            )
        ).scalar_one()
        rows.append({"sequence": seq, "step_count": step_count, "active_count": active_count})

    context = base_context(
        session, ui_session, active_nav="sequences", flash=flash, flash_type=flash_type,
    )
    context.update({
        "rows": rows, "step_channels": list(SequenceStepChannel),
        "step_channel_labels": STEP_CHANNEL_LABELS,
        "step_row_range": range(MAX_STEP_ROWS),
        "touch_channels": list(TouchChannel),
        "touch_channel_labels": TOUCH_CHANNEL_LABELS,
        "default_reaction_channels": {c.value for c in HIGH_INTENT_CHANNELS},
    })
    return templates.TemplateResponse(request, "sequences.html", context)


@router.post("/ui/sequences/new")
async def sequence_new_submit(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    if not name:
        return redirect_with_flash("/ui/sequences", "シーケンス名を入力してください", "error")

    steps: list[dict] = []
    for i in range(MAX_STEP_ROWS):
        channel = form.get(f"channel_{i}")
        body = (form.get(f"body_{i}") or "").strip()
        if not channel or not body:
            continue
        steps.append({
            "channel": SequenceStepChannel(channel),
            "delay_days": int(form.get(f"delay_days_{i}") or 0),
            "subject_template": (form.get(f"subject_{i}") or "").strip() or None,
            "body_template": body,
            "reaction_channels": form.getlist(f"reaction_channels_{i}"),
            "on_reaction_next_order": _parse_branch_target(form.get(f"on_reaction_next_{i}")),
            "on_no_reaction_next_order": _parse_branch_target(form.get(f"on_no_reaction_next_{i}")),
        })

    if not steps:
        return redirect_with_flash(
            "/ui/sequences", "少なくとも1ステップの本文を入力してください", "error",
        )

    create_sequence(
        session, ui_session.tenant_id, name=name, description=description or None,
        steps=steps, actor=f"human:{ui_session.actor_id}",
    )
    session.commit()
    return redirect_with_flash("/ui/sequences", f"シーケンス「{name}」を作成しました")


@router.post("/ui/sequences/generate-due")
def generate_due_ui(
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    drafts = generate_due_drafts(session, ui_session.tenant_id)
    session.commit()
    return redirect_with_flash("/ui/sequences", f"{len(drafts)}件のドラフトを生成しました")
