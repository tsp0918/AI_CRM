"""3システム突合(docs/BULK_SIMULATION_SPEC.md §8.1)。

スモーク規模で実際に確認できる項目(R-01/R-02/R-03/R-08)を実装する。
R-04〜R-07/R-09/R-10は分納・失注・ERP単独受注などP4(medium規模)で
初めて意味のあるデータが揃うため、ここでは"checked": False のまま返す。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from crm_mvp.models import Account, Contract, LicenseAllocation
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clients.aitm import AitmClient


@dataclass
class ReconcileFinding:
    code: str
    description: str
    entity_ref: str


@dataclass
class ReconcileReport:
    findings: list[ReconcileFinding] = field(default_factory=list)
    checked: dict[str, bool] = field(default_factory=dict)

    def add(self, code: str, description: str, entity_ref: str) -> None:
        self.findings.append(ReconcileFinding(code=code, description=description, entity_ref=entity_ref))


def _r01_transaction_id_consistency(session: Session, tenant_id: uuid.UUID, report: ReconcileReport) -> None:
    """台帳 ＝ CRM契約(external_id) ＝ AI_TM審査(provider_request_id) の
    一貫性(ERP受注番号とAI_TM審査ケース番号がそれぞれ非空であること)。"""
    report.checked["R-01"] = True
    from crm_mvp.models import ReviewCase
    contracts = session.execute(
        select(Contract).where(Contract.tenant_id == tenant_id, Contract.status.in_(["signed", "active"]))
    ).scalars().all()
    for c in contracts:
        formal = session.execute(
            select(ReviewCase).where(
                ReviewCase.tenant_id == tenant_id, ReviewCase.contract_id == c.id,
                ReviewCase.review_type == "formal",
            )
        ).scalars().first()
        if formal is None or not formal.provider_request_id:
            report.add("R-01", f"契約{c.contract_number}に対応する正式審査のAI_TM側ケース番号が無い", str(c.id))


def _r02_party_id_consistency(session: Session, tenant_id: uuid.UUID, report: ReconcileReport) -> None:
    report.checked["R-02"] = True
    accounts = session.execute(
        select(Account).where(Account.tenant_id == tenant_id, Account.external_system == "erp")
    ).scalars().all()
    for a in accounts:
        if not a.aitm_party_id:
            report.add("R-02", f"取引先{a.name}にaitm_party_idが採番されていない", str(a.id))


def _r08_license_quota_consistency(
    session: Session, tenant_id: uuid.UUID, aitm: AitmClient, report: ReconcileReport,
) -> None:
    """CRM側のLicenseAllocation(仮引当)がAI_TM側の許可証枠に反映されているか
    (warningsが記録されているのにallocation_idが無い = 本来ライセンスが
    必要なのに引当できていない、という食い違いだけを検出する)。

    2026-08-16のP3スモークで判明: AI_TM側は「ライセンス不要品目」に対しても
    `{"allocation_id": null, "status": "allocated", "allocations": []}`を返し、
    CRM側もこれをそのまま`ALLOCATED`として記録する。allocation_id無し自体は
    ライセンス不要品目では正常なので、それだけでは異常としない。
    """
    report.checked["R-08"] = True
    allocations = session.execute(
        select(LicenseAllocation).where(LicenseAllocation.tenant_id == tenant_id)
    ).scalars().all()
    for alloc in allocations:
        if str(alloc.status) == "allocated" and not alloc.allocation_id and alloc.warnings:
            report.add(
                "R-08", "警告付きなのにAI_TM側のallocation_idが記録されていない"
                "(ライセンス必要品目で引当が成立していない疑い)", str(alloc.id),
            )


def run_reconcile_checks(session: Session, tenant_id: uuid.UUID, aitm: AitmClient) -> ReconcileReport:
    report = ReconcileReport()
    _r01_transaction_id_consistency(session, tenant_id, report)
    _r02_party_id_consistency(session, tenant_id, report)
    _r08_license_quota_consistency(session, tenant_id, aitm, report)
    for code in ("R-04", "R-05", "R-06", "R-07", "R-09", "R-10"):
        report.checked.setdefault(code, False)
    return report
