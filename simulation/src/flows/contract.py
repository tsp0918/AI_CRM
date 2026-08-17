"""見積〜契約〜ERP転記(docs/BULK_SIMULATION_SPEC.md §5.3)。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from crm_mvp.models import Contract, ReviewCase
from sqlalchemy import select

from ..clients.aitm import AitmClient
from ..clients.crm import CrmWebhookClient
from ..clients.crm_ui import CrmUiClient, UiFlowError
from ..db import tenant_session
from ..outbox_runner import drain_outbox
from .review import drive_and_push_review_result


@dataclass
class ContractResult:
    engagement_id: uuid.UUID
    contract_id: uuid.UUID | None = None
    contract_number: str | None = None
    erp_so_number: str | None = None
    outcome: str = "unknown"  # signed | blocked_party | blocked_review | error
    reason: str | None = None
    outbox_stats: dict = field(default_factory=dict)


def run_quote_to_signed_contract(
    tenant_id: uuid.UUID, ui: CrmUiClient, aitm: AitmClient, crm_wh: CrmWebhookClient, *,
    engagement_id: uuid.UUID, quote_id: uuid.UUID,
    destination_country: str = "", end_use: str = "", end_user_account_id: str = "",
    contract_years: int = 1,
) -> ContractResult:
    result = ContractResult(engagement_id=engagement_id)

    try:
        ui.create_contract(
            engagement_id, quote_id=str(quote_id),
            start_date=date.today().isoformat(),
            end_date=(date.today() + timedelta(days=365 * contract_years)).isoformat(),
            destination_country=destination_country, end_use=end_use,
            end_user_account_id=end_user_account_id,
        )
    except UiFlowError as e:
        result.outcome, result.reason = "blocked_party", str(e)
        return result

    with tenant_session(tenant_id) as session:
        contract = session.execute(
            select(Contract).where(Contract.tenant_id == tenant_id, Contract.engagement_id == engagement_id)
        ).scalars().first()
        result.contract_id, result.contract_number = contract.id, contract.contract_number

    with tenant_session(tenant_id) as session:
        result.outbox_stats = drain_outbox(session, tenant_id)

    with tenant_session(tenant_id) as session:
        formal_case = session.execute(
            select(ReviewCase).where(
                ReviewCase.tenant_id == tenant_id, ReviewCase.engagement_id == engagement_id,
                ReviewCase.review_type == "formal",
            )
        ).scalars().first()
        case_no = formal_case.case_no if formal_case else None
        aitm_case_no = formal_case.provider_request_id if formal_case else None

    if aitm_case_no:
        drive_and_push_review_result(aitm, crm_wh, case_no, aitm_case_no)

    try:
        ui.update_contract_status(engagement_id, result.contract_id, status="signed")
    except UiFlowError as e:
        result.outcome, result.reason = "blocked_review", str(e)
        return result

    with tenant_session(tenant_id) as session:
        stats2 = drain_outbox(session, tenant_id)
        for k, v in stats2.items():
            result.outbox_stats[k] = result.outbox_stats.get(k, 0) + v
        contract = session.get(Contract, result.contract_id)
        result.erp_so_number = contract.external_id

    result.outcome = "signed" if result.erp_so_number else "erp_transcription_failed"
    return result
