"""SSR による相関図ビュー(HANDOVER.md §5 Phase4-16 のフロント実装)。

バックエンドの JSON API(engagements.py の GET /engagements/{id}/graph 等)
はそのまま外部連携用に残す — このモジュールは画面表示専用の別経路。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...enums import AccessLevel, BuyingCenterRole, Stance
from ...models import Account, Engagement
from ...services.confidence_score import compute_confidence_score, score_reasons
from ...services.contacts import register_contact_and_link
from ...services.graph_export import build_graph_dot, build_graph_json
from ...services.stage_transitions import load_gate_context
from .common import CRITERION_LABELS, base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

STANCE_LABELS = {
    "supporter": "支持", "neutral": "中立", "opponent": "反対", "unknown": "不明",
}
STANCE_COLORS = {
    "supporter": "#c8f7c5", "neutral": "#f7f2c5",
    "opponent": "#f7c5c5", "unknown": "#ffffff",
}
ACCESS_LEVEL_LABELS = {
    "none": "未接触", "contacted": "接触済", "engaged": "関係構築済",
}
ROLE_LABELS = {
    "decider": "決裁者", "champion": "チャンピオン", "coach": "コーチ",
    "user": "ユーザー", "technical_gate": "技術関門", "finance": "財務",
    "initiator": "起案者",
}


@router.get("/ui/engagements/{engagement_id}/graph", response_class=HTMLResponse)
def graph_page(
    request: Request,
    engagement_id: uuid.UUID,
    include_sensitive: bool = False,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")
    account = session.get(Account, engagement.account_id)

    data = build_graph_json(
        session, ui_session.tenant_id, engagement, include_sensitive=include_sensitive,
    )
    dot = build_graph_dot(
        session, ui_session.tenant_id, engagement, include_sensitive=include_sensitive,
    )
    svg = dot.pipe(format="svg").decode("utf-8") if data["nodes"] else None

    label_by_id = {n["id"]: n["label"] for n in data["nodes"]}
    edges = [
        {**e, "from_label": label_by_id.get(e["from"], e["from"]),
         "to_label": label_by_id.get(e["to"], e["to"])}
        for e in data["edges"]
    ]

    gate_ctx = load_gate_context(session, ui_session.tenant_id, engagement)
    score = compute_confidence_score(
        gate_ctx["slots"], gate_ctx["nodes"], gate_ctx["edges"], gate_ctx["roles"],
    )
    reasons = score_reasons(score, CRITERION_LABELS)

    context = base_context(session, ui_session, active_nav="dashboard")
    context.update({
        "include_sensitive": include_sensitive,
        "engagement": engagement,
        "account_name": account.name if account else None,
        "svg": svg,
        "nodes": data["nodes"],
        "edges": edges,
        "stance_labels": STANCE_LABELS,
        "stance_colors": STANCE_COLORS,
        "access_level_labels": ACCESS_LEVEL_LABELS,
        "role_labels": ROLE_LABELS,
        "buying_center_roles": list(BuyingCenterRole),
        "score": score, "score_reasons": reasons,
    })
    return templates.TemplateResponse(request, "graph.html", context)


@router.post("/ui/engagements/{engagement_id}/graph/contacts")
def add_contact_ui(
    engagement_id: uuid.UUID,
    full_name: str = Form(...),
    title: str = Form(""),
    org_unit: str = Form(""),
    email: str = Form(""),
    role: str = Form(""),
    access_level: str = Form("contacted"),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    """新規の関係者(Contact)を登録し、この案件に紐付ける。

    Lead の概念には最低限「誰から/誰に」の情報が伴う。相関図はここから
    育ち始める(§4: 参加者の同定・接触履歴は確認不要でAIが直接書ける
    領域だが、起点の登録は人が行う経路として提供する)。
    """
    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")

    if not full_name.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}/graph", "氏名を入力してください", "error",
        )

    register_contact_and_link(
        session, ui_session.tenant_id, engagement,
        full_name=full_name.strip(), title=title.strip() or None,
        org_unit=org_unit.strip() or None, email=email.strip() or None,
        roles=[role] if role else [], stance=Stance.UNKNOWN,
        access_level=AccessLevel(access_level),
        written_by=f"human:{ui_session.actor_id}",
    )
    session.commit()

    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}/graph", f"{full_name} を関係者として登録しました",
    )
