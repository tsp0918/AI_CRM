"""UI ページ共通のヘルパー(ナビゲーション用コンテキスト・フラッシュ表示)。"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import ProposalStatus
from ...models import ExtractionProposal
from .session import UiSession

STAGE_LABELS = {
    "lead": "リード", "prospect": "見込み", "qualified": "案件化",
    "proposal": "提案", "negotiation": "最終交渉",
    "closed_won": "受注", "closed_lost": "失注",
}

CRITERION_LABELS = {
    "metrics": "定量効果", "economic_buyer": "決裁者", "decision_criteria": "評価基準",
    "decision_process": "決裁プロセス", "paper_process": "稟議手続き",
    "identified_pain": "顕在ペイン", "champion": "チャンピオン",
    "competition": "競合", "budget": "予算", "timing": "タイミング",
}

CONFIDENCE_LABELS = {
    "asserted": "担当者の主張", "corroborated": "裏付けあり", "verified": "検証済み",
}

SOURCE_KIND_LABELS = {
    "transcript": "トランスクリプト", "recording": "録画・録音(未対応)",
    "email": "メール", "free_note": "自由メモ", "crm_sync": "既存CRM取り込み",
    "calendar_sync": "会議同期(Outlook/Teams)",
}


def base_context(
    session: Session, ui_session: UiSession, active_nav: str,
    flash: str | None = None, flash_type: str = "info",
) -> dict:
    pending_count = session.execute(
        select(func.count()).select_from(ExtractionProposal).where(
            ExtractionProposal.tenant_id == ui_session.tenant_id,
            ExtractionProposal.status == ProposalStatus.PENDING,
        )
    ).scalar_one()
    return {
        "ui_session": ui_session,
        "active_nav": active_nav,
        "pending_count": pending_count,
        "flash_message": flash,
        "flash_type": flash_type,
        "stage_labels": STAGE_LABELS,
        "criterion_labels": CRITERION_LABELS,
        "confidence_labels": CONFIDENCE_LABELS,
        "source_kind_labels": SOURCE_KIND_LABELS,
    }


def redirect_with_flash(
    url: str, message: str, flash_type: str = "success", status_code: int = 303,
) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    qs = urlencode({"flash": message, "flash_type": flash_type})
    return RedirectResponse(url=f"{url}{separator}{qs}", status_code=status_code)


def format_slot_value(value: dict) -> str:
    if not value:
        return "—"
    return ", ".join(f"{k}: {v}" for k, v in value.items())
