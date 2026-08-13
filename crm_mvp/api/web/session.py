"""UI 用の擬似セッション(Cookie ベース)。

認証基盤が無い MVP の制約上(README.md「MVP に含めていないもの」参照)、
tenant_id と actor_id(操作者を識別する UUID)を Cookie に保存するだけの
最小実装。署名や暗号化はしていない — 本番投入前に必ず実認証へ
置き換えること。actor_id は accept/reject・stage 遷移・verify・waiver
発行など「誰が行ったか」の記録(Provenance / decided_by 等)に使う。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import quote, unquote

from fastapi import Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import get_session

TENANT_COOKIE = "crm_tenant_id"
ACTOR_COOKIE = "crm_actor_id"
ACTOR_NAME_COOKIE = "crm_actor_name"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30日


@dataclass(slots=True)
class UiSession:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    actor_name: str | None


class WorkspaceRequired(Exception):
    """セッション未確立時に投げ、app.py の例外ハンドラでリダイレクトさせる。"""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def read_ui_session(request: Request) -> UiSession | None:
    tenant_raw = request.cookies.get(TENANT_COOKIE)
    actor_raw = request.cookies.get(ACTOR_COOKIE)
    if not tenant_raw or not actor_raw:
        return None
    try:
        tenant_id = uuid.UUID(tenant_raw)
        actor_id = uuid.UUID(actor_raw)
    except ValueError:
        return None
    actor_name_raw = request.cookies.get(ACTOR_NAME_COOKIE)
    return UiSession(
        tenant_id=tenant_id, actor_id=actor_id,
        # Cookie 値は latin-1 のみ許容されるため保存時に percent-encode している。
        actor_name=unquote(actor_name_raw) if actor_name_raw else None,
    )


def set_ui_session_cookies(
    response: Response, tenant_id: uuid.UUID, actor_id: uuid.UUID,
    actor_name: str | None,
) -> None:
    response.set_cookie(
        TENANT_COOKIE, str(tenant_id), max_age=COOKIE_MAX_AGE, httponly=True,
    )
    response.set_cookie(
        ACTOR_COOKIE, str(actor_id), max_age=COOKIE_MAX_AGE, httponly=True,
    )
    if actor_name:
        # Cookie 値は latin-1 のみ許容されるため、日本語等は percent-encode して保存する。
        response.set_cookie(
            ACTOR_NAME_COOKIE, quote(actor_name), max_age=COOKIE_MAX_AGE, httponly=True,
        )


def clear_ui_session_cookies(response: Response) -> None:
    response.delete_cookie(TENANT_COOKIE)
    response.delete_cookie(ACTOR_COOKIE)
    response.delete_cookie(ACTOR_NAME_COOKIE)


def require_ui_session(request: Request) -> UiSession:
    session = read_ui_session(request)
    if session is None:
        raise WorkspaceRequired(next_url=str(request.url))
    return session


def get_ui_db_session(
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_session),
) -> Session:
    """§7.4: RLS が参照する session 変数を Cookie の tenant_id で設定する。"""
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ui_session.tenant_id)},
    )
    return session
