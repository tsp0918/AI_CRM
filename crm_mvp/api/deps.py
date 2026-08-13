"""FastAPI 共通依存。

認証は MVP のスコープ外（README.md 参照）。テナント識別は暫定的に
X-Tenant-Id ヘッダで受け取る — 認証基盤が入り次第、トークンからの
導出に置き換える前提の最小実装。

§7.4: テナント分離は Row-Level Security に確定(2026-08-13)。
get_tenant_scoped_session が RLS ポリシー用の session 変数を
リクエストごとに設定する。
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_session as get_session  # re-export for router imports
from ..ports.extractor import ExtractorPort, NullExtractor
from ..ports.screening import MockScreeningAdapter, ScreeningPort


def get_tenant_id(x_tenant_id: str = Header(alias="X-Tenant-Id")) -> uuid.UUID:
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="X-Tenant-Id header must be a UUID"
        )


def get_tenant_scoped_session(
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> Session:
    """RLS ポリシーが参照する app.current_tenant_id をリクエスト単位で設定する。

    SET LOCAL は現在のトランザクション終了時に自動的に解除されるため、
    コネクションプーリング環境でも次のリクエストへテナント文脈が
    漏れる心配がない。アプリ層の WHERE tenant_id = ... によるフィルタは
    引き続き行う(RLS は多層防御の最後の砦であって、それだけに頼らない)。
    """
    # SET / SET LOCAL は値にバインドパラメータを取れない(リテラルのみ)ため
    # set_config() を使う。第3引数 true が SET LOCAL 相当(トランザクション終了で解除)。
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    return session


def get_extractor() -> ExtractorPort:
    """既定は NullExtractor（LLM 未接続）。実運用では差し替える。"""
    return NullExtractor()


def get_screening_port() -> ScreeningPort:
    """既定は MockScreeningAdapter。AITM_SCREENING_URL があれば実アダプタに
    差し替えるのは呼び出し側(main.py 等)の組み立て責務とする。"""
    return MockScreeningAdapter()
