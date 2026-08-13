"""SSR による相関図ビュー(HANDOVER.md §5 Phase4-16 のフロント実装)。

FastAPI + Jinja2Templates によるサーバーサイドレンダリングに統一する方針
のため、当初試作した React Flow 版は廃止しこちらに一本化した。
バックエンドの JSON API(engagements.py の GET /engagements/{id}/graph 等)
はそのまま外部連携用に残す — このモジュールは画面表示専用の別経路。

認証・セッションが無い MVP の制約上(README.md 参照)、tenant_id は
クエリパラメータで受け取る。本番でセッション認証が入ったら、
ここをセッションからの導出に置き換えること。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Account, Engagement
from ..services.graph_export import build_graph_dot, build_graph_json
from .deps import get_session

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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


@router.get("/ui/graph", response_class=HTMLResponse)
def graph_page(
    request: Request,
    tenant_id: str = "",
    engagement_id: str = "",
    include_sensitive: bool = False,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    context: dict = {
        "tenant_id": tenant_id,
        "engagement_id": engagement_id,
        "include_sensitive": include_sensitive,
        "error": None,
        "engagement": None,
        "account_name": None,
        "svg": None,
        "nodes": [],
        "edges": [],
        "stance_labels": STANCE_LABELS,
        "stance_colors": STANCE_COLORS,
        "access_level_labels": ACCESS_LEVEL_LABELS,
        "role_labels": ROLE_LABELS,
    }

    if tenant_id and engagement_id:
        context.update(
            _load_graph(session, tenant_id, engagement_id, include_sensitive)
        )

    return templates.TemplateResponse(request, "graph.html", context)


def _load_graph(
    session: Session, tenant_id_raw: str, engagement_id_raw: str,
    include_sensitive: bool = False,
) -> dict:
    try:
        tenant_id = uuid.UUID(tenant_id_raw)
        engagement_id = uuid.UUID(engagement_id_raw)
    except ValueError:
        return {"error": "tenant_id / engagement_id は UUID 形式で入力してください"}

    # §7.4: RLS ポリシーが参照する session 変数をこの画面用にも設定する
    # (このルーターだけ X-Tenant-Id ヘッダを使わないため deps.get_tenant_scoped_session
    # を経由できず、tenant_id が判明した時点でここで直接 SET する)。
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )

    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != tenant_id:
        return {"error": "指定した engagement が見つかりません"}

    account = session.get(Account, engagement.account_id)
    data = build_graph_json(
        session, tenant_id, engagement, include_sensitive=include_sensitive,
    )
    dot = build_graph_dot(
        session, tenant_id, engagement, include_sensitive=include_sensitive,
    )
    svg = dot.pipe(format="svg").decode("utf-8") if data["nodes"] else None

    label_by_id = {n["id"]: n["label"] for n in data["nodes"]}
    edges = [
        {**e, "from_label": label_by_id.get(e["from"], e["from"]),
         "to_label": label_by_id.get(e["to"], e["to"])}
        for e in data["edges"]
    ]

    return {
        "engagement": engagement,
        "account_name": account.name if account else None,
        "svg": svg,
        "nodes": data["nodes"],
        "edges": edges,
    }
