"""P2: マスタデータ投入(docs/BULK_SIMULATION_SPEC.md §4)。

3システムすべてに実際にPOSTする(ERP business-partner/material、
AI_TM counterparty/watchlist/license quota)。CRMのAccount/Product/
ErpMaterialは公開JSON APIのスコープ外のため、`simulation/src/db.py`経由で
直接投入する(`scripts/seed_demo_*.py`と同じ確立済みパターン)。
`aitm_party_id`は本物のWebhook経路(`/webhooks/aitm/party-event`、
`party.linked`)で反映させる — DBに直接書き込まない(§4完了条件の
「aitm_party_idが採番されている」を、実際の連携経路で満たすため)。

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p2_seed_masters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crm_mvp.enums import FeftaJudgment
from crm_mvp.models import Account, Product
from crm_mvp.services.erp_business_partners import upsert_erp_business_partner
from crm_mvp.services.erp_materials import upsert_erp_material

from simulation.src.clients.aitm import AitmClient
from simulation.src.clients.crm import CrmWebhookClient
from simulation.src.clients.erp import ErpClient
from simulation.src.config import load_endpoints
from simulation.src.db import tenant_session
from simulation.src.generators.masters import generate_masters
from simulation.src.rng import Rng
from simulation.src.safety import assert_safe_environment

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _load_scenario() -> dict:
    import yaml
    with open(CONFIG_DIR / "scenario.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    scenario = _load_scenario()
    cfg = load_endpoints(CONFIG_DIR / "endpoints.yaml")
    assert_safe_environment(cfg)

    rng = Rng(scenario["seed"])
    masters = generate_masters(rng)
    tenant_id = cfg.crm["tenant_id"]

    erp = ErpClient(cfg)
    aitm = AitmClient(cfg)
    crm_wh = CrmWebhookClient(cfg)

    stats = {"erp_bp": 0, "erp_material": 0, "accounts": 0, "aitm_counterparty": 0,
              "party_linked": 0, "watchlist": 0, "products": 0, "licenses": 0, "errors": []}
    id_map: dict = {"accounts": {}, "products": {}, "licenses": {}}

    try:
        with tenant_session(tenant_id) as session:
            # --- §4.1 取引先: ERP既存(13社)は先にERPへ登録してからCRMへミラー ---
            for account in masters.accounts:
                if account.category in ("erp_domestic", "erp_overseas"):
                    resp = erp.create_business_partner({
                        "name": account.legal_name, "country": account.country,
                        "roles": "CUSTOMER", "bp_code": account.external_id,
                        "credit_limit": 50000000 if account.category == "erp_domestic" else None,
                        "currency": "JPY" if account.category == "erp_domestic" else None,
                    })
                    if resp.status_code == 409:
                        bp = erp.find_business_partner_by_code(account.external_id)
                        if bp is None:
                            stats["errors"].append(f"ERP BP {account.sim_id}: 409だが既存レコードが見つからない")
                            continue
                    elif resp.status_code not in (200, 201):
                        stats["errors"].append(f"ERP BP {account.sim_id}: {resp.status_code} {resp.text[:200]}")
                        continue
                    else:
                        bp = resp.json()
                    stats["erp_bp"] += 1
                    upsert_erp_business_partner(
                        session, tenant_id, bp_code=bp["bp_code"], name=bp["name"],
                        bp_type=bp.get("bp_type", "ORG"), country=bp.get("country"),
                        roles=bp.get("roles", "CUSTOMER"), email=bp.get("email"), phone=bp.get("phone"),
                        address_line1=bp.get("address_line1"), address_line2=bp.get("address_line2"),
                        city=bp.get("city"), postal_code=bp.get("postal_code"),
                        credit_limit=bp.get("credit_limit"), payment_terms=bp.get("payment_terms"),
                        currency=bp.get("currency"), is_denied_party=bp.get("is_denied_party", False),
                        is_active=bp.get("is_active", True),
                    )
                    session.commit()

                crm_account = Account(
                    tenant_id=tenant_id, name=account.legal_name, country=account.country,
                    external_system="erp" if account.external_id else None,
                    external_id=account.external_id,
                )
                session.add(crm_account)
                session.flush()
                # ★ webhookはCRMサーバの別プロセス/別コネクションが処理する
                # (READ COMMITTED)。ここでcommitする前にwebhookを呼ぶと、
                # 向こう側からこの行がまだ見えず`_resolve_party_account`が
                # Noneを返して黙って何もしないのに、レスポンスは200
                # "processed"を返す(該当なし正常系と同じ扱いのため) —
                # 気づかずに成功扱いしてしまう典型例。必ず先にcommitする。
                session.commit()
                stats["accounts"] += 1
                id_map["accounts"][account.sim_id] = {
                    "crm_account_id": str(crm_account.id), "erp_bp_code": account.external_id,
                    "aitm_party_id": None, "legal_name": account.legal_name,
                }

                # --- AI_TMへcounterparty登録(再実行時の重複防止に名前で存在確認) ---
                existing_cp = aitm.find_counterparty_by_name(account.legal_name)
                if existing_cp is not None:
                    aitm_party_id = existing_cp["id"]
                else:
                    cp_resp = aitm.create_counterparty({
                        "name": account.legal_name, "country_code": account.country,
                        "roles": ["CUSTOMER"], "is_end_user": account.is_end_user_only,
                    })
                    if cp_resp.status_code not in (200, 201):
                        stats["errors"].append(f"AI_TM counterparty {account.sim_id}: {cp_resp.status_code} {cp_resp.text[:200]}")
                        continue
                    aitm_party_id = cp_resp.json()["id"]
                stats["aitm_counterparty"] += 1
                id_map["accounts"][account.sim_id]["aitm_party_id"] = aitm_party_id

                wh_resp = crm_wh.send_party_linked(
                    crm_account_id=str(crm_account.id), aitm_party_id=aitm_party_id,
                    erp_bp_code=account.external_id,
                )
                # 200固定のため、応答コードだけでは「実際にaitm_party_idが
                # 反映されたか」を保証できない(該当なし正常系も200を返す設計
                # のため) — 必ず実際の行を読み直して検証する(fail loud)。
                session.expire(crm_account)
                actually_linked = session.get(Account, crm_account.id).aitm_party_id == aitm_party_id
                if wh_resp.status_code == 200 and actually_linked:
                    stats["party_linked"] += 1
                else:
                    stats["errors"].append(
                        f"party.linked {account.sim_id}: status={wh_resp.status_code} "
                        f"body={wh_resp.text[:200]} verified={actually_linked}"
                    )

            # --- §4.4 ウォッチリスト ---
            # `GET /api/watchlist`はlist_sourceでの絞り込みに対応しておらず、
            # 全件(54000件超)の中からクライアント側で照合するのは非現実的
            # なため、再実行時の重複チェックはしない — 同名エントリが重複
            # 登録されても`/api/screen`のHIT/CLEAR判定自体は変わらない
            # (スコアが同じ重複候補が複数返るだけ)ため実害は無いと判断。
            for entry in masters.watchlist:
                resp = aitm.add_watchlist_entry({
                    "list_source": "SIM_TEST_WATCHLIST", "entity_name": entry.company_name,
                    "risk_level": "high",
                })
                if resp.status_code in (200, 201):
                    stats["watchlist"] += 1
                else:
                    stats["errors"].append(f"watchlist {entry.company_name}: {resp.status_code} {resp.text[:200]}")
            # 追加直後は検索インデックスに反映されない(2026-08-16 P3疎通確認で
            # 判明) — 登録後に必ずインデックスを再構築する。
            rebuild_resp = aitm.rebuild_watchlist_index()
            print(f"  watchlist index rebuild -> {rebuild_resp.status_code}")

            # --- §4.2 品目 ---
            for product in masters.products:
                erp_material_id = None
                if product.erp_material_code is not None:
                    if erp.find_material_by_code(product.erp_material_code) is None:
                        resp = erp.create_material({
                            "description": product.name, "material_type": "FERT", "base_unit": "PC",
                            "standard_price": 10000, "currency": "JPY", "eccn": product.eccn,
                            "material_code": product.erp_material_code, "auto_classify": False,
                        })
                        if resp.status_code not in (200, 201):
                            stats["errors"].append(f"ERP material {product.sim_id}: {resp.status_code} {resp.text[:200]}")
                            continue
                    stats["erp_material"] += 1
                    mat = upsert_erp_material(
                        session, tenant_id, material_code=product.erp_material_code,
                        description=product.name, material_type="FERT", base_unit="PC",
                        standard_price=10000, currency="JPY", eccn=product.eccn,
                        fefta_judgment=FeftaJudgment(product.classification),
                    )
                    erp_material_id = mat.id
                    session.commit()

                crm_product = Product(
                    tenant_id=tenant_id, name=product.name, sku=product.sim_id,
                    list_price=15000, currency="JPY", erp_material_id=erp_material_id,
                )
                session.add(crm_product)
                session.flush()
                stats["products"] += 1
                id_map["products"][product.sim_id] = {
                    "crm_product_id": str(crm_product.id), "erp_material_code": product.erp_material_code,
                }
            session.commit()

        # --- §4.3 輸出許可証(destination_countryは1件1レコードのためdestごとに登録) ---
        product_code_by_sim_id = {p.sim_id: p.erp_material_code for p in masters.products}
        for lic in masters.licenses:
            product_code = product_code_by_sim_id.get(lic.product_sim_id)
            license_nos = []
            for dest in lic.destinations:
                license_no = lic.sim_id if len(lic.destinations) == 1 else f"{lic.sim_id}-{dest}"
                resp = aitm.register_license_quota({
                    "license_no": license_no, "product_code": product_code,
                    "destination_country": dest, "total_unit": lic.quantity,
                    "valid_until": lic.expires_on,
                })
                # 409(license_no重複)は「既に登録済み」を意味する既存の
                # outbox分類方針(classify_http_response)と同じ考え方で
                # 成功として扱う(再実行時の冪等化)。
                if resp.status_code in (200, 201, 409):
                    stats["licenses"] += 1
                    license_nos.append(license_no)
                else:
                    stats["errors"].append(f"license {license_no}: {resp.status_code} {resp.text[:200]}")
            id_map["licenses"][lic.sim_id] = {"license_nos": license_nos, "product_code": product_code}
    finally:
        erp.close()
        aitm.close()
        crm_wh.close()
        out_dir = Path(__file__).resolve().parents[1] / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        import json
        (out_dir / "master_ids.json").write_text(
            json.dumps(id_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print("=== P2 マスタ投入結果 ===")
    print(f"  ERP business partner 登録: {stats['erp_bp']} / 13(既存)")
    print(f"  ERP material 登録:         {stats['erp_material']} / 13(未マッピング2件を除く)")
    print(f"  CRM Account 作成:          {stats['accounts']} / {len(masters.accounts)}")
    print(f"  AI_TM counterparty 登録:   {stats['aitm_counterparty']} / {len(masters.accounts)}")
    print(f"  party.linked webhook 成功: {stats['party_linked']} / {stats['aitm_counterparty']}")
    print(f"  AI_TM watchlist 登録:      {stats['watchlist']} / {len(masters.watchlist)}")
    print(f"  CRM Product 作成:          {stats['products']} / {len(masters.products)}")
    print(f"  AI_TM license quota 登録:  {stats['licenses']}")
    if stats["errors"]:
        print(f"\nエラー {len(stats['errors'])}件:")
        for e in stats["errors"][:20]:
            print(f"  - {e}")
        return 1
    print("\nOK: P2完了条件を満たしました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
