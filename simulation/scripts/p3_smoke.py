"""P3: スモーク実行(docs/BULK_SIMULATION_SPEC.md §10.2)。

商談3件(既存顧客の正常系フル経路、新規顧客のフル経路、取引先制裁ヒットに
よるブロック)で、全IFが最低1回ずつ通ることを確認する。

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p3_smoke.py
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crm_mvp.enums import ComplianceCheckType

from simulation.src.clients.aitm import AitmClient
from simulation.src.clients.crm import CrmWebhookClient
from simulation.src.clients.crm_ui import CrmUiClient
from simulation.src.clients.erp import ErpClient
from simulation.src.config import load_endpoints
from simulation.src.db import tenant_session
from simulation.src.flows.contract import run_quote_to_signed_contract
from simulation.src.flows.fulfillment import ship_and_bill
from simulation.src.flows.opportunity import create_engagement_existing_account, run_opportunity_to_quote
from simulation.src.flows.party_screening import screen_and_record
from simulation.src.safety import assert_safe_environment

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
OUT_DIR = Path(__file__).resolve().parents[1] / "out"


def scenario_a_existing_customer_full(tenant_id, ui, aitm, crm_wh, erp, id_map) -> dict:
    print("=== シナリオA: 既存顧客・正常系フル経路 ===")
    acc = id_map["accounts"]["SIM-ACC-003"]  # "SIM Trading 01" — ウォッチリストと同名衝突しないクリーンな取引先
    prod = id_map["products"]["SIM-PROD-07"]
    engagement_id = create_engagement_existing_account(
        tenant_id, account_id=uuid.UUID(acc["crm_account_id"]), name="SIM-SMOKE-A 既存顧客商談",
    )
    opp = run_opportunity_to_quote(
        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id,
        line_items=[(uuid.UUID(prod["crm_product_id"]), 10)],
        destination_country="KR", end_use="半導体製造装置の部品として使用",
    )
    print(f"  quote outcome={opp.outcome} reason={opp.reason}")
    result = {"name": "A_existing_customer_full", "engagement_id": str(engagement_id), "quote": opp.outcome}
    if opp.outcome != "issuable":
        return result

    contract = run_quote_to_signed_contract(
        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id, quote_id=opp.quote_id,
    )
    print(f"  contract outcome={contract.outcome} erp_so={contract.erp_so_number}")
    result["contract"] = contract.outcome
    if contract.outcome != "signed":
        return result

    fulfillment = ship_and_bill(erp, crm_wh, erp_so_number=contract.erp_so_number, erp_bp_code=acc["erp_bp_code"])
    print(f"  fulfillment outcome={fulfillment.outcome}")
    result["fulfillment"] = fulfillment.outcome
    return result


def scenario_b_new_customer(tenant_id, ui, aitm, crm_wh, id_map) -> dict:
    print("=== シナリオB: 新規顧客・正常系(Web UI経由) ===")
    prod = id_map["products"]["SIM-PROD-03"]  # EAR99・品目マッピング済み(01/02は意図的に未マッピング)
    engagement_id = ui.create_engagement_new_account(
        account_name="SIM Smoke New Customer Corp", engagement_name="SIM-SMOKE-B 新規顧客商談",
    )
    opp = run_opportunity_to_quote(
        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id,
        line_items=[(uuid.UUID(prod["crm_product_id"]), 5)],
        destination_country="JP", end_use="国内での再販",
    )
    print(f"  quote outcome={opp.outcome} reason={opp.reason}")
    result = {"name": "B_new_customer", "engagement_id": str(engagement_id), "quote": opp.outcome}
    if opp.outcome != "issuable":
        return result

    contract = run_quote_to_signed_contract(
        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id, quote_id=opp.quote_id,
    )
    print(f"  contract outcome={contract.outcome} erp_so={contract.erp_so_number}")
    result["contract"] = contract.outcome
    return result


def scenario_c_sanctions_hit_blocked(tenant_id, ui, aitm, crm_wh, id_map) -> dict:
    print("=== シナリオC: 制裁ヒット取引先 → ブロック確認 ===")
    acc = id_map["accounts"]["SIM-ACC-014"]  # SIM Semiconductor Alpha Corp(SIM_TEST_WATCHLISTのmatch)
    with tenant_session(tenant_id) as session:
        screening_outcome = screen_and_record(
            session, tenant_id, aitm, account_id=uuid.UUID(acc["crm_account_id"]),
            legal_name=acc["legal_name"], country="JP", check_type=ComplianceCheckType.SANCTIONS,
        )
        session.commit()
    print(f"  screening outcome for {acc['legal_name']}: {screening_outcome}")

    engagement_id = create_engagement_existing_account(
        tenant_id, account_id=uuid.UUID(acc["crm_account_id"]), name="SIM-SMOKE-C 制裁ヒット商談",
    )
    prod = id_map["products"]["SIM-PROD-01"]
    opp = run_opportunity_to_quote(
        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id,
        line_items=[(uuid.UUID(prod["crm_product_id"]), 3)],
        destination_country="JP", end_use="テスト",
    )
    print(f"  quote outcome={opp.outcome} reason={opp.reason}")
    return {
        "name": "C_sanctions_hit_blocked", "engagement_id": str(engagement_id),
        "screening_outcome": str(screening_outcome), "quote": opp.outcome, "reason": opp.reason,
        "expected_blocked": True, "actually_blocked": opp.outcome == "blocked_party",
    }


def main() -> int:
    cfg = load_endpoints(CONFIG_DIR / "endpoints.yaml")
    assert_safe_environment(cfg)
    tenant_id = cfg.crm["tenant_id"]
    id_map = json.loads((OUT_DIR / "master_ids.json").read_text())

    ui = CrmUiClient(cfg.crm["base_url"], tenant_id=tenant_id, actor_id=uuid.uuid4())
    erp = ErpClient(cfg)
    aitm = AitmClient(cfg)
    crm_wh = CrmWebhookClient(cfg)

    scenarios: list[dict] = []
    try:
        scenarios.append(scenario_a_existing_customer_full(tenant_id, ui, aitm, crm_wh, erp, id_map))
        scenarios.append(scenario_b_new_customer(tenant_id, ui, aitm, crm_wh, id_map))
        scenarios.append(scenario_c_sanctions_hit_blocked(tenant_id, ui, aitm, crm_wh, id_map))
    finally:
        ui.close()
        erp.close()
        aitm.close()
        crm_wh.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "p3_smoke_result.json").write_text(json.dumps(scenarios, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== P3 smoke結果 ===")
    for s in scenarios:
        print(f"  {s}")

    c_ok = scenarios[-1].get("actually_blocked") is True
    print(f"\n制裁ヒットで正しくブロックされたか: {c_ok}")
    return 0 if c_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
