"""アウトバウンド・シーケンス自動化のUI(下書き止まり)(ロードマップ§7)。

シーケンステンプレートの作成・一覧、および「本日分のドラフトを生成する」
手動トリガー(daily_snapshot.py と同じ、定期実行の代替としての手動ボタン)。
Leadへの登録・ドラフトのレビューは leads.py 側の画面から行う。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...enums import SequenceStepChannel, TouchChannel
from ...models import Sequence
from ...services.lead_scoring import HIGH_INTENT_CHANNELS
from ...services.sequences import (
    SEQUENCE_END, create_sequence, generate_due_drafts, list_sequence_summaries,
    sequence_funnel,
)
from .common import base_context, redirect_with_flash
from .leads import LEAD_STATUS_LABELS, TOUCH_CHANNEL_LABELS
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

STEP_CHANNEL_LABELS = {
    "email": "メール", "call_task": "架電タスク", "linkedin": "LinkedIn",
}
DRAFT_STATUS_LABELS = {
    "draft": "下書き", "reviewed": "対応済み", "dismissed": "見送り",
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
    rows = list_sequence_summaries(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="sequences", request=request, flash=flash, flash_type=flash_type,
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


@router.get("/ui/sequences/{sequence_id}", response_class=HTMLResponse)
def sequence_detail(
    request: Request,
    sequence_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    sequence = session.get(Sequence, sequence_id)
    if sequence is None or sequence.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="sequence not found")

    funnel = sequence_funnel(session, ui_session.tenant_id, sequence_id)

    context = base_context(session, ui_session, active_nav="sequences", request=request)
    context.update({
        "sequence": sequence, "funnel": funnel,
        "step_channel_labels": STEP_CHANNEL_LABELS,
        "lead_status_labels": LEAD_STATUS_LABELS,
        "draft_status_labels": DRAFT_STATUS_LABELS,
    })
    return templates.TemplateResponse(request, "sequence_detail.html", context)
