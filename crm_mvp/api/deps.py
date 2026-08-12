"""FastAPI 共通依存。

認証は MVP のスコープ外（README.md 参照）。テナント識別は暫定的に
X-Tenant-Id ヘッダで受け取る — 認証基盤が入り次第、トークンからの
導出に置き換える前提の最小実装（HANDOVER.md §7.4 は分離方式そのものが未決）。
"""

from __future__ import annotations

import uuid

from fastapi import Header, HTTPException

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


def get_extractor() -> ExtractorPort:
    """既定は NullExtractor（LLM 未接続）。実運用では差し替える。"""
    return NullExtractor()


def get_screening_port() -> ScreeningPort:
    """既定は MockScreeningAdapter。AITM_SCREENING_URL があれば実アダプタに
    差し替えるのは呼び出し側(main.py 等)の組み立て責務とする。"""
    return MockScreeningAdapter()
