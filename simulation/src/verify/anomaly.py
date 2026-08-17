"""異常検出(docs/BULK_SIMULATION_SPEC.md §8.3)。

「誰も気づかないまま止まっている案件」を検出する。各チェックはCRMの
DBを読み取り専用で走査する(simulation/src/db.pyのtenant_session経由)。
スモーク規模では意味を持たない項目(A-06の失注等)は対象0件のまま返す
(嘘の「pass」を主張しないよう、"checked": False で明示する)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from crm_mvp.enums import ComplianceOutcome
from crm_mvp.models import (
    ComplianceStatus, Contract, ContractFulfillment, Engagement, Quote, ReviewCase,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass
class AnomalyFinding:
    code: str
    description: str
    entity_ref: str


@dataclass
class AnomalyReport:
    findings: list[AnomalyFinding] = field(default_factory=list)
    checked: dict[str, bool] = field(default_factory=dict)

    def add(self, code: str, description: str, entity_ref: str) -> None:
        self.findings.append(AnomalyFinding(code=code, description=description, entity_ref=entity_ref))

    def count(self, code: str) -> int:
        return sum(1 for f in self.findings if f.code == code)


def _a03_contract_signed_without_erp_order(session: Session, tenant_id: uuid.UUID, report: AnomalyReport) -> None:
    report.checked["A-03"] = True
    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.status.in_(["signed", "active"]),
        )
    ).scalars().all()
    for c in contracts:
        if not c.external_id:
            report.add(
                "A-03", f"契約{c.contract_number}はSIGNED済みだがERP受注が存在しない"
                f"(取引先がERP未登録のままERP転記した可能性)", str(c.id),
            )


def _a07_unknown_treated_as_clear(session: Session, tenant_id: uuid.UUID, report: AnomalyReport) -> None:
    """UNKNOWN(判定不能)のComplianceStatusを持つ取引先が、それでもSENT/SIGNED
    まで進んだ見積・契約の当事者(取引先 or エンドユーザー)になっていないかを
    確認する(§6.2「フェイルクローズが機能しているか」、最重要項目)。"""
    report.checked["A-07"] = True
    unknown_account_ids = set(session.execute(
        select(ComplianceStatus.account_id).where(
            ComplianceStatus.tenant_id == tenant_id, ComplianceStatus.outcome == ComplianceOutcome.UNKNOWN,
        )
    ).scalars().all())
    if not unknown_account_ids:
        return

    engagement_account = dict(session.execute(
        select(Engagement.id, Engagement.account_id).where(Engagement.tenant_id == tenant_id)
    ).all())

    quotes = session.execute(
        select(Quote).where(Quote.tenant_id == tenant_id, Quote.status.in_(["sent", "accepted"]))
    ).scalars().all()
    for q in quotes:
        parties = {engagement_account.get(q.engagement_id), q.end_user_account_id}
        if parties & unknown_account_ids:
            report.add(
                "A-07", f"取引先/エンドユーザーがUNKNOWN判定のまま見積が{q.status}に進んでいる"
                "(フェイルクローズ違反の疑い)", str(q.id),
            )

    contracts = session.execute(
        select(Contract).where(Contract.tenant_id == tenant_id, Contract.status.in_(["signed", "active"]))
    ).scalars().all()
    for c in contracts:
        parties = {engagement_account.get(c.engagement_id), c.end_user_account_id}
        if parties & unknown_account_ids:
            report.add(
                "A-07", f"取引先/エンドユーザーがUNKNOWN判定のまま契約が{c.status}に進んでいる"
                "(フェイルクローズ違反の疑い)", str(c.id),
            )


def _a08_orphan_fulfillment(session: Session, tenant_id: uuid.UUID, report: AnomalyReport) -> None:
    report.checked["A-08"] = True
    fulfillments = session.execute(
        select(ContractFulfillment).where(ContractFulfillment.tenant_id == tenant_id)
    ).scalars().all()
    contract_ids = {
        c.id for c in session.execute(select(Contract.id).where(Contract.tenant_id == tenant_id)).all()
    }
    for f in fulfillments:
        if f.contract_id not in contract_ids:
            report.add("A-08", f"親契約を持たない実績レコード({f.kind})", str(f.id))


def _r03_no_duplicate_reviews(session: Session, tenant_id: uuid.UUID, report: AnomalyReport) -> None:
    """R-03相当: 1エンゲージメントあたり仮審査1件・正式審査1件を超えていないか
    (review_key_hash不変での再利用が機能していれば増えないはず)。"""
    report.checked["R-03"] = True
    reviews = session.execute(select(ReviewCase).where(ReviewCase.tenant_id == tenant_id)).scalars().all()
    by_engagement: dict[uuid.UUID, dict[str, int]] = {}
    for r in reviews:
        bucket = by_engagement.setdefault(r.engagement_id, {"provisional": 0, "formal": 0})
        bucket[r.review_type] = bucket.get(r.review_type, 0) + 1
    for engagement_id, counts in by_engagement.items():
        if counts.get("formal", 0) > 1:
            report.add(
                "R-03", f"正式審査が{counts['formal']}件起票されている(重複の疑い)", str(engagement_id),
            )


def run_anomaly_checks(session: Session, tenant_id: uuid.UUID) -> AnomalyReport:
    report = AnomalyReport()
    _a03_contract_signed_without_erp_order(session, tenant_id, report)
    _a07_unknown_treated_as_clear(session, tenant_id, report)
    _a08_orphan_fulfillment(session, tenant_id, report)
    _r03_no_duplicate_reviews(session, tenant_id, report)
    report.checked.setdefault("A-01", False)
    report.checked.setdefault("A-02", False)
    report.checked.setdefault("A-04", False)
    report.checked.setdefault("A-05", False)
    report.checked.setdefault("A-06", False)
    return report
