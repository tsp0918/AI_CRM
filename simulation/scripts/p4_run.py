"""P4: 本実行 コア経路(docs/BULK_SIMULATION_SPEC.md §10.3、商談60件)。

ユーザー選択によるスコープ: 商談60件すべてを「商談→見積→(発行できれば)
契約→(締結できれば)ERP転記・出荷・請求」というコア経路で実行する。
以下は今回のパスでは対象外(P1のイベント生成では件数が確定済みだが、
実システムへの実行はまだ行っていない) — 詳細は findings_p4.md 参照:
  - 見積の複数回改訂(review_key_hash不変での再審査省略の実証)
  - 審査鮮度切れ(§11.3)の再現
  - 継続監視ヒット・みなし輸出・返品・ERP単独受注・R&D起点(IF-14/26)

例外系のうち、今回のコア経路に組み込んだもの:
  - 品目マッピング未設定(2件): 明細に未マッピング品目を強制混入
  - ライセンス枠不足(3件): SIM-LIC-02(CN, 300枠)を超える数量で仕向地CN
  - 取引先制裁ヒット/possible_match: 既存のSIM_TEST_WATCHLIST該当取引先を使用

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p4_run.py
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
from simulation.src.generators.events import generate_all_events
from simulation.src.generators.masters import generate_masters
from simulation.src.generators.product_assignment import pick_line_items
from simulation.src.rng import Rng
from simulation.src.safety import assert_safe_environment

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
OUT_DIR = Path(__file__).resolve().parents[1] / "out"

# SIM_TEST_WATCHLIST該当取引先(§4.4)。P4コア経路ではこの2社を該当象限の
# 代表として使い、実スクリーニングでHIT/フラグが付くことを確認する。
FORCED_SCREENING_ACCOUNTS = {
    "SIM-ACC-014": ComplianceCheckType.SANCTIONS,  # SIM Semiconductor Alpha Corp(match)
}

# `destination_country`はVARCHAR(2)。scenario.yamlのdestination_mixにある
# "OTHER"はあくまで生成側の分布ラベルであり、実在の2文字国コードではない。
_OTHER_DESTINATION_MAP = {"OTHER": "DE"}


def _load_scenario() -> dict:
    import yaml
    with open(CONFIG_DIR / "scenario.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    scenario = _load_scenario()
    cfg = load_endpoints(CONFIG_DIR / "endpoints.yaml")
    assert_safe_environment(cfg)
    tenant_id = cfg.crm["tenant_id"]
    id_map = json.loads((OUT_DIR / "master_ids.json").read_text())

    rng = Rng(scenario["seed"])
    masters = generate_masters(rng)  # P2と同一seed → 同一マスタ構成を再現
    account_by_sim_id = {a.sim_id: a for a in masters.accounts}

    rng2 = Rng(scenario["seed"])  # イベント生成はP1と全く同じ手順で再現する
    events, summary = generate_all_events(scenario, generate_masters(rng2), rng2)
    engagement_events = [e for e in events if e.kind == "engagement_created"]
    print(f"engagements to run: {len(engagement_events)}")

    exec_rng = Rng(scenario["seed"] + 1)  # 品目割当・win判定は実行専用の別系列にする

    unmapped_targets = set(exec_rng.sample(range(len(engagement_events)), 2))
    remaining = [i for i in range(len(engagement_events)) if i not in unmapped_targets]
    shortage_targets = set(exec_rng.sample(remaining, 3))

    ui = CrmUiClient(cfg.crm["base_url"], tenant_id=tenant_id, actor_id=uuid.uuid4())
    erp = ErpClient(cfg)
    aitm = AitmClient(cfg)
    crm_wh = CrmWebhookClient(cfg)

    # 事前に制裁ヒット取引先の実スクリーニング結果をComplianceStatusへ反映
    with tenant_session(tenant_id) as session:
        for sim_id, check_type in FORCED_SCREENING_ACCOUNTS.items():
            acc = id_map["accounts"][sim_id]
            outcome = screen_and_record(
                session, tenant_id, aitm, account_id=uuid.UUID(acc["crm_account_id"]),
                legal_name=acc["legal_name"], country="JP", check_type=check_type,
            )
            print(f"pre-screened {acc['legal_name']}: {outcome}")
        session.commit()

    results: list[dict] = []
    try:
        for i, ev in enumerate(engagement_events):
            sim_account = account_by_sim_id[ev.payload["account"]]
            acc = id_map["accounts"].get(sim_account.sim_id)
            if acc is None:
                results.append({"index": i, "sim_id": ev.sim_id, "outcome": "skipped_no_account_mapping"})
                continue

            force_unmapped = i in unmapped_targets
            force_shortage = i in shortage_targets
            raw_destination = ev.payload.get("destination", "JP")
            # `destination_country`はVARCHAR(2)(2026-08-16 P4実行で発覚:
            # generators/events.pyの分布に含まれる"OTHER"というプレースホルダ
            # ラベルをそのまま送るとDataErrorになる)ため実在の国コードに写像する。
            destination = "CN" if force_shortage else _OTHER_DESTINATION_MAP.get(raw_destination, raw_destination)
            # P1のイベントには明細件数までは含まれない(dry-runは件数検証のみで
            # 十分なため)。P4では固定2件(強制品目がある場合は+1)とする簡略化。
            n_items = 1 if (force_unmapped or force_shortage) else 2
            line_item_specs = pick_line_items(
                exec_rng, ev.payload["quadrant"], n_items,
                force_unmapped=force_unmapped, force_license_shortage=force_shortage,
            )
            line_items = []
            for prod_sim_id, qty in line_item_specs:
                prod = id_map["products"].get(prod_sim_id)
                if prod:
                    line_items.append((uuid.UUID(prod["crm_product_id"]), qty))

            print(f"\n[{i + 1}/{len(engagement_events)}] {ev.sim_id} account={sim_account.legal_name} "
                  f"quadrant={ev.payload['quadrant']} dest={destination} "
                  f"{'UNMAPPED' if force_unmapped else ''}{'SHORTAGE' if force_shortage else ''}")

            row = {
                "index": i, "sim_id": ev.sim_id, "account": sim_account.sim_id,
                "quadrant": ev.payload["quadrant"], "forced_unmapped": force_unmapped,
                "forced_shortage": force_shortage,
            }
            try:
                engagement_id = create_engagement_existing_account(
                    tenant_id, account_id=uuid.UUID(acc["crm_account_id"]), name=f"{ev.sim_id} 自動生成商談",
                )
                row["engagement_id"] = str(engagement_id)

                opp = run_opportunity_to_quote(
                    tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id, line_items=line_items,
                    destination_country=destination, end_use="シミュレーション自動生成",
                )
                row["quote_outcome"] = opp.outcome
                row["quote_reason"] = opp.reason
                print(f"  quote: {opp.outcome} {opp.reason or ''}")

                if opp.outcome == "issuable" and exec_rng.random() < scenario["funnel"]["win_rate"]:
                    contract = run_quote_to_signed_contract(
                        tenant_id, ui, aitm, crm_wh, engagement_id=engagement_id, quote_id=opp.quote_id,
                    )
                    row["contract_outcome"] = contract.outcome
                    print(f"  contract: {contract.outcome} erp_so={contract.erp_so_number}")
                    if contract.outcome == "signed":
                        fulfillment = ship_and_bill(
                            erp, crm_wh, erp_so_number=contract.erp_so_number, erp_bp_code=acc["erp_bp_code"],
                        )
                        row["fulfillment_outcome"] = fulfillment.outcome
                        print(f"  fulfillment: {fulfillment.outcome}")
            except Exception as exc:  # noqa: BLE001 — 1件の失敗で全体を止めない
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"  ERROR: {row['error']}")

            results.append(row)
            (OUT_DIR / "p4_run_result.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
            )
    finally:
        ui.close()
        erp.close()
        aitm.close()
        crm_wh.close()

    print(f"\n=== P4完了: {len(results)}件処理 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
