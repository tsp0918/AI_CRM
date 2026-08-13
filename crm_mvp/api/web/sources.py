"""情報投入フォーム(唯一の入力口。§3.9)。

トランスクリプト・メモ・メールをここに貼り付けると、その場で抽出
パイプラインを実行する(HANDOVER.md §5 Phase2 item5-9)。LLM 未接続の
この環境では claims=0 になる — その旨を投入後の結果に明示する。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import SourceKind
from ...models import Engagement, IngestionSource
from ...ports.extractor import ExtractorPort
from ...services.ingestion_runner import process_source
from ...services.users import list_users
from ..deps import get_extractor
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


def _parse_attendees(raw: str) -> list[dict]:
    """「氏名, メールアドレス」を1行1名で受け取る(Outlook/Teams 連携が
    実際に投げてくる出席者リストの代替入力)。メールは任意。"""
    speakers: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        name = parts[0]
        email = parts[1] if len(parts) > 1 and parts[1] else None
        if name:
            speakers.append({"label": name, "email": email})
    return speakers


@router.get("/ui/sources/new", response_class=HTMLResponse)
def source_new_form(
    request: Request,
    engagement_id: str = "",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagements = session.execute(
        select(Engagement).where(Engagement.tenant_id == ui_session.tenant_id)
        .order_by(Engagement.name)
    ).scalars().all()

    context = base_context(session, ui_session, active_nav="dashboard")
    context.update({
        "engagements": engagements,
        "preselected_engagement_id": engagement_id,
    })
    return templates.TemplateResponse(request, "source_new.html", context)


@router.post("/ui/sources/new")
def source_new_submit(
    engagement_id: str = Form(...),
    kind: str = Form(...),
    raw_text: str = Form(""),
    attendees: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
    extractor: ExtractorPort = Depends(get_extractor),
) -> RedirectResponse:
    eid = uuid.UUID(engagement_id)
    engagement = session.get(Engagement, eid)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")

    if not raw_text.strip():
        return redirect_with_flash(
            f"/ui/sources/new?engagement_id={engagement_id}",
            "本文を入力してください", "error",
        )

    source = IngestionSource(
        tenant_id=ui_session.tenant_id, engagement_id=eid,
        kind=SourceKind(kind), raw_text=raw_text,
    )
    session.add(source)
    session.flush()

    speakers = _parse_attendees(attendees) if kind == SourceKind.CALENDAR_SYNC else None

    try:
        outcome = process_source(
            session, ui_session.tenant_id, source, extractor=extractor,
            speakers=speakers,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", f"処理に失敗しました: {exc}", "error",
        )
    session.commit()

    message = (
        f"取り込み完了: {outcome.claims}件抽出 / {outcome.auto_applied}件自動適用 / "
        f"{outcome.pending}件が承認待ちです"
    )
    if speakers:
        message += f" / 出席者 {outcome.matched_speakers}名を相関図に反映しました"
    if outcome.claims == 0:
        message += "(この環境は LLM 未接続のため自動抽出は行われません)"
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", message)


# --- クイック入力(モバイル/商談直後向けの軽量版) -----------------------------
#
# 通常の情報投入フォームは種別選択・出席者欄など機能が多く、移動中や
# 商談直後にその場で使うには重い。ここでは「案件を選ぶ」「本文を書く」
# 「送る」の3手だけに絞った経路を別途用意する(自由メモ固定・出席者欄なし)。
# 裏側のパイプラインは source_new_submit と完全に同じものを再利用する。

@router.get("/ui/quick-note", response_class=HTMLResponse)
def quick_note_form(
    request: Request,
    engagement_id: str = "",
    owner_user_id: str = "",
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    # 2026-08-14: owner_user_id が付いていれば(ダッシュボードの担当者
    # 絞り込みからサイドバー経由で遷移してきた場合)、その担当者の案件を
    # 「自分の担当」として先に出す。付いていなければ今まで通りフラットな
    # 直近更新20件のみ(後方互換)。
    own_engagements: list[Engagement] = []
    if owner_user_id:
        own_engagements = session.execute(
            select(Engagement).where(
                Engagement.tenant_id == ui_session.tenant_id,
                Engagement.owner_user_id == uuid.UUID(owner_user_id),
            ).order_by(Engagement.updated_at.desc())
        ).scalars().all()
    own_ids = {e.id for e in own_engagements}

    other_engagements = [
        e for e in session.execute(
            select(Engagement).where(Engagement.tenant_id == ui_session.tenant_id)
            .order_by(Engagement.updated_at.desc()).limit(20)
        ).scalars().all()
        if e.id not in own_ids
    ]

    context = base_context(
        session, ui_session, active_nav="quick_note", flash=flash, flash_type=flash_type,
    )
    context.update({
        "own_engagements": own_engagements, "other_engagements": other_engagements,
        "preselected_engagement_id": engagement_id,
        "users": list_users(session, ui_session.tenant_id), "owner_user_id": owner_user_id,
    })
    return templates.TemplateResponse(request, "quick_note.html", context)


def _quick_note_url(owner_user_id: str) -> str:
    return f"/ui/quick-note?owner_user_id={owner_user_id}" if owner_user_id else "/ui/quick-note"


@router.post("/ui/quick-note")
def quick_note_submit(
    engagement_id: str = Form(...),
    raw_text: str = Form(""),
    owner_user_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
    extractor: ExtractorPort = Depends(get_extractor),
) -> RedirectResponse:
    eid = uuid.UUID(engagement_id)
    engagement = session.get(Engagement, eid)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")

    redirect_url = _quick_note_url(owner_user_id)

    if not raw_text.strip():
        return redirect_with_flash(
            redirect_url, "本文を入力してください", "error",
        )

    source = IngestionSource(
        tenant_id=ui_session.tenant_id, engagement_id=eid,
        kind=SourceKind.FREE_NOTE, raw_text=raw_text,
    )
    session.add(source)
    session.flush()

    try:
        outcome = process_source(session, ui_session.tenant_id, source, extractor=extractor)
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(redirect_url, f"処理に失敗しました: {exc}", "error")
    session.commit()

    message = f"{engagement.name} にメモを記録しました({outcome.claims}件抽出)"
    return redirect_with_flash(redirect_url, message)
