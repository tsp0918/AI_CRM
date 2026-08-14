"""経営レポートのスナップショット(2026-08-14)。

売上・キャンペーン効果・シーケンス効果・パイプラインの状態を、任意の
時点で1行(ReportSnapshot)に丸めて保存する。集計結果そのものを
JSONBに固定化するため、後から集計ロジックが変わっても過去の
スナップショットの見え方は保存時点のまま変わらない。

各集計関数の as_of パラメータ(revenue_report.py / campaigns.py /
sequences.py)を使って、snapshot_date時点までの実績だけに絞って
再構成する。パイプラインの内訳だけは「ステージは時間とともに変わる」
性質上、日次のPipelineSnapshotテーブルに実際にその日のデータが
無ければ再構成できない — 無ければ has_baseline と同じ思想で
available=Falseを返す(このアプリの既存のフォールバック方針と同じ)。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Engagement, PipelineSnapshot, ReportSnapshot
from .campaigns import list_campaign_effectiveness
from .engagement_relationships import list_renewal_candidates
from .revenue_report import (
    RELATIONSHIP_TYPE_REPORT_LABELS, aggregate_by, closed_won_revenue_rows,
    product_group_revenue, totals_by_currency,
)
from .sequences import list_sequence_summaries
from .snapshot import OPEN_STAGES


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


def _serialize_agg_rows(rows: list[dict]) -> list[dict]:
    """aggregate_by()の出力からid(ドリルダウン用)を落とし、Decimalを
    文字列化してJSONB保存できる形にする(履歴は読み取り専用のため
    idは不要)。"""
    return [
        {"label": r["label"], "amount": str(r["amount"]), "count": r["count"]}
        for r in rows
    ]


def _build_revenue_payload(
    session: Session, tenant_id: uuid.UUID, as_of: date,
) -> dict:
    rows = closed_won_revenue_rows(session, tenant_id, as_of=as_of)
    return {
        "totals_by_currency": [
            [currency, str(amount)] for currency, amount in totals_by_currency(rows)
        ],
        "deal_count": len(rows),
        "by_product_group": _serialize_agg_rows(
            product_group_revenue(session, tenant_id, as_of=as_of)
        ),
        "by_account": _serialize_agg_rows(aggregate_by(
            rows, lambda r: r["root_account"].name if r["root_account"] else "—",
        )),
        "by_sales_group": _serialize_agg_rows(aggregate_by(
            rows, lambda r: r["sales_group"].name if r["sales_group"] else "未設定",
        )),
        "by_relationship": _serialize_agg_rows(aggregate_by(
            rows,
            lambda r: RELATIONSHIP_TYPE_REPORT_LABELS.get(
                r["relationship_type"], r["relationship_type"],
            ),
        )),
    }


def _build_campaigns_payload(
    session: Session, tenant_id: uuid.UUID, as_of: datetime,
) -> list[dict]:
    rows = list_campaign_effectiveness(session, tenant_id, as_of=as_of)
    return [
        {
            "name": r["campaign"].name, "channel_type": str(r["campaign"].channel_type),
            "lead_count": r["lead_count"], "converted_count": r["converted_count"],
            "conversion_rate": r["conversion_rate"],
        }
        for r in rows
    ]


def _build_sequences_payload(
    session: Session, tenant_id: uuid.UUID, as_of: datetime,
) -> list[dict]:
    rows = list_sequence_summaries(session, tenant_id, as_of=as_of)
    return [
        {
            "name": r["sequence"].name, "step_count": r["step_count"],
            "active_count": r["active_count"], "total_enrolled": r["total_enrolled"],
            "final_reach_pct": r["final_reach_pct"],
        }
        for r in rows
    ]


def _build_pipeline_payload(
    session: Session, tenant_id: uuid.UUID, snapshot_date: date,
) -> dict:
    rows = session.execute(
        select(PipelineSnapshot.stage, func.count()).where(
            PipelineSnapshot.tenant_id == tenant_id,
            PipelineSnapshot.snapshot_date == snapshot_date,
        ).group_by(PipelineSnapshot.stage)
    ).all()
    if rows:
        stage_counts = {str(stage): count for stage, count in rows}
        source = "pipeline_snapshot"
    elif snapshot_date == date.today():
        rows = session.execute(
            select(Engagement.stage, func.count()).where(
                Engagement.tenant_id == tenant_id, Engagement.stage.in_(OPEN_STAGES),
            ).group_by(Engagement.stage)
        ).all()
        stage_counts = {str(stage): count for stage, count in rows}
        source = "live"
    else:
        return {"available": False}

    candidates = list_renewal_candidates(
        session, tenant_id, within_days=90, today=snapshot_date,
    )
    overdue = [c for c in candidates if c.end_date <= snapshot_date]
    return {
        "available": True, "source": source, "stage_counts": stage_counts,
        "renewal_candidates": len(candidates), "renewal_overdue": len(overdue),
    }


def capture_snapshot(
    session: Session, tenant_id: uuid.UUID, *,
    snapshot_date: date | None = None, label: str | None = None, actor: str,
) -> ReportSnapshot:
    """指定日時点の売上/キャンペーン/シーケンス/パイプラインを1行に
    丸めて保存する。同じ日に既に保存済みなら上書きする(冪等)。"""
    snapshot_date = snapshot_date or date.today()
    as_of_dt = _end_of_day(snapshot_date)

    payload = {
        "revenue": _build_revenue_payload(session, tenant_id, snapshot_date),
        "campaigns": _build_campaigns_payload(session, tenant_id, as_of_dt),
        "sequences": _build_sequences_payload(session, tenant_id, as_of_dt),
        "pipeline": _build_pipeline_payload(session, tenant_id, snapshot_date),
    }

    existing = get_snapshot_for_date(session, tenant_id, snapshot_date)
    if existing is not None:
        existing.label = label
        existing.payload = payload
        existing.written_by = actor
        session.flush()
        return existing

    snapshot = ReportSnapshot(
        tenant_id=tenant_id, snapshot_date=snapshot_date, label=label,
        payload=payload, written_by=actor,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def get_snapshot_for_date(
    session: Session, tenant_id: uuid.UUID, snapshot_date: date,
) -> ReportSnapshot | None:
    return session.execute(
        select(ReportSnapshot).where(
            ReportSnapshot.tenant_id == tenant_id,
            ReportSnapshot.snapshot_date == snapshot_date,
        )
    ).scalar_one_or_none()


def list_snapshots(session: Session, tenant_id: uuid.UUID) -> list[ReportSnapshot]:
    return session.execute(
        select(ReportSnapshot).where(ReportSnapshot.tenant_id == tenant_id)
        .order_by(ReportSnapshot.snapshot_date.desc())
    ).scalars().all()


def get_snapshot(
    session: Session, tenant_id: uuid.UUID, snapshot_id: uuid.UUID,
) -> ReportSnapshot | None:
    return session.execute(
        select(ReportSnapshot).where(
            ReportSnapshot.tenant_id == tenant_id, ReportSnapshot.id == snapshot_id,
        )
    ).scalar_one_or_none()


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def diff_snapshots(a: ReportSnapshot, b: ReportSnapshot) -> dict:
    """aからbへの差分(呼び出し側が古い順/新しい順どちらで渡すかは自由)。"""
    a_currency = dict(a.payload.get("revenue", {}).get("totals_by_currency", []))
    b_currency = dict(b.payload.get("revenue", {}).get("totals_by_currency", []))
    revenue_diff = []
    for currency in sorted(set(a_currency) | set(b_currency)):
        from_amount = Decimal(a_currency.get(currency, "0"))
        to_amount = Decimal(b_currency.get(currency, "0"))
        revenue_diff.append({
            "currency": currency, "from": from_amount, "to": to_amount,
            "delta": to_amount - from_amount,
        })

    a_campaigns = _by_name(a.payload.get("campaigns", []))
    b_campaigns = _by_name(b.payload.get("campaigns", []))
    campaign_diff = [
        {
            "name": name,
            "lead_count_from": a_campaigns.get(name, {}).get("lead_count", 0),
            "lead_count_to": b_campaigns.get(name, {}).get("lead_count", 0),
            "conversion_rate_from": a_campaigns.get(name, {}).get("conversion_rate", 0),
            "conversion_rate_to": b_campaigns.get(name, {}).get("conversion_rate", 0),
        }
        for name in sorted(set(a_campaigns) | set(b_campaigns))
    ]

    a_sequences = _by_name(a.payload.get("sequences", []))
    b_sequences = _by_name(b.payload.get("sequences", []))
    sequence_diff = [
        {
            "name": name,
            "total_enrolled_from": a_sequences.get(name, {}).get("total_enrolled", 0),
            "total_enrolled_to": b_sequences.get(name, {}).get("total_enrolled", 0),
            "final_reach_pct_from": a_sequences.get(name, {}).get("final_reach_pct", 0),
            "final_reach_pct_to": b_sequences.get(name, {}).get("final_reach_pct", 0),
        }
        for name in sorted(set(a_sequences) | set(b_sequences))
    ]

    return {
        "revenue": revenue_diff,
        "deal_count_from": a.payload.get("revenue", {}).get("deal_count", 0),
        "deal_count_to": b.payload.get("revenue", {}).get("deal_count", 0),
        "campaigns": campaign_diff,
        "sequences": sequence_diff,
    }
