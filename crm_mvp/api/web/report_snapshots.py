"""経営レポートのスナップショット履歴UI(2026-08-14)。

任意時点の売上/キャンペーン/シーケンス/パイプラインを「スナップショット」
として保存し、時系列の一覧・詳細・2時点比較ができるようにする。
売上レポート等(reports.py)は常に「今」を見る画面であるのに対し、
このページは過去の保存済み時点を振り返るための画面 — 別の画面として
分離する。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...services.report_snapshot import (
    capture_snapshot, diff_snapshots, get_snapshot, list_snapshots,
)
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/reports/history", response_class=HTMLResponse)
def report_history_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    snapshots = list_snapshots(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="report_history", request=request,
        flash=flash, flash_type=flash_type,
    )
    context.update({"snapshots": snapshots})
    return templates.TemplateResponse(request, "report_history.html", context)


@router.post("/ui/reports/history/capture")
def report_history_capture(
    label: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    snapshot = capture_snapshot(
        session, ui_session.tenant_id, label=label.strip() or None,
        actor=f"human:{ui_session.actor_id}",
    )
    session.commit()
    return redirect_with_flash(
        "/ui/reports/history", f"{snapshot.snapshot_date} のスナップショットを保存しました",
    )


@router.get("/ui/reports/history/compare", response_class=HTMLResponse)
def report_history_compare(
    request: Request,
    from_id: str = "",
    to_id: str = "",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    # 注意: /ui/reports/history/{snapshot_id} より先に定義すること。
    # FastAPIはルート登録順に一致を試すため、後ろに書くと "compare" が
    # snapshot_id(UUID)としてパースされ 422 になる。
    from_snapshot = (
        get_snapshot(session, ui_session.tenant_id, uuid.UUID(from_id)) if from_id else None
    )
    to_snapshot = (
        get_snapshot(session, ui_session.tenant_id, uuid.UUID(to_id)) if to_id else None
    )
    diff = None
    if from_snapshot is not None and to_snapshot is not None:
        diff = diff_snapshots(from_snapshot, to_snapshot)

    context = base_context(
        session, ui_session, active_nav="report_history", request=request,
    )
    context.update({
        "snapshots": list_snapshots(session, ui_session.tenant_id),
        "from_snapshot": from_snapshot, "to_snapshot": to_snapshot, "diff": diff,
        "from_id": from_id, "to_id": to_id,
    })
    return templates.TemplateResponse(request, "report_snapshot_compare.html", context)


@router.get("/ui/reports/history/{snapshot_id}", response_class=HTMLResponse)
def report_history_detail(
    snapshot_id: uuid.UUID,
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    snapshot = get_snapshot(session, ui_session.tenant_id, snapshot_id)
    if snapshot is None:
        return redirect_with_flash(
            "/ui/reports/history", "スナップショットが見つかりません", "error",
        )

    context = base_context(session, ui_session, active_nav="report_history", request=request)
    context.update({"snapshot": snapshot})
    return templates.TemplateResponse(request, "report_snapshot_detail.html", context)
