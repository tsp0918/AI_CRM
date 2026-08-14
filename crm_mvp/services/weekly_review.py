"""1on1週次レビュー(2026-08-14)。

商談ごとに「週の月曜日」をキーにした1行のレビュー(担当者コメント・
マネージャーコメント・マネージャーステータス)を管理する。あわせて、
PipelineSnapshot(既存の日次スナップショット)から直近7日以上前の
最新行を基準にした「先週との差分」を計算する。

PipelineSnapshotは外部cron(scripts/daily_snapshot.py)前提で書き込まれる
テーブルのため、基準となるスナップショットが無いことは普通に起こる —
その場合は has_baseline=False を返し、呼び出し側は「データなし」を
素直に表示する(このアプリの他のフォールバック方針と同じ)。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ReviewStatus
from ..models import Engagement, PipelineSnapshot, WeeklyReview
from .confidence_score import ConfidenceScore


def _week_start(d: date) -> date:
    """その日を含む週の月曜日を返す。"""
    return d - timedelta(days=d.weekday())


def get_review_for_week(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID, week_start_date: date,
) -> WeeklyReview | None:
    return session.execute(
        select(WeeklyReview).where(
            WeeklyReview.tenant_id == tenant_id,
            WeeklyReview.engagement_id == engagement_id,
            WeeklyReview.week_start_date == week_start_date,
        )
    ).scalar_one_or_none()


def get_or_create_current_review(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID, *,
    actor: str = "human:unknown", today: date | None = None,
) -> WeeklyReview:
    week_start_date = _week_start(today or date.today())
    review = get_review_for_week(session, tenant_id, engagement_id, week_start_date)
    if review is not None:
        return review

    review = WeeklyReview(
        tenant_id=tenant_id, engagement_id=engagement_id,
        week_start_date=week_start_date, written_by=actor,
    )
    session.add(review)
    session.flush()
    return review


def update_review(
    review: WeeklyReview, *, rep_comment: str | None = None,
    manager_comment: str | None = None, manager_status: ReviewStatus | str | None = None,
) -> WeeklyReview:
    if rep_comment is not None:
        review.rep_comment = rep_comment or None
    if manager_comment is not None:
        review.manager_comment = manager_comment or None
    if manager_status is not None:
        review.manager_status = manager_status or None
    return review


def compute_week_over_week_diff(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement, score: ConfidenceScore,
    today: date | None = None,
) -> dict:
    """今週(=ライブの現在値)と、直近7日以上前の最新PipelineSnapshotとを
    比較する。基準スナップショットが無ければ has_baseline=False のみ返す。"""
    today = today or date.today()
    threshold = today - timedelta(days=7)
    previous = session.execute(
        select(PipelineSnapshot).where(
            PipelineSnapshot.tenant_id == tenant_id,
            PipelineSnapshot.engagement_id == engagement.id,
            PipelineSnapshot.snapshot_date <= threshold,
        ).order_by(PipelineSnapshot.snapshot_date.desc()).limit(1)
    ).scalar_one_or_none()
    if previous is None:
        return {"has_baseline": False}

    amount_delta = None
    if engagement.amount is not None and previous.amount is not None:
        amount_delta = engagement.amount - previous.amount

    return {
        "has_baseline": True,
        "snapshot_date": previous.snapshot_date,
        "score_delta": score.total - round(previous.evidence_score * 100),
        "amount_delta": amount_delta,
        "previous_stage": previous.stage,
        "stage_changed": previous.stage != engagement.stage,
    }
