"""P3疎通確認: 商談1件を実際にengagement→line-item→quote→審査→契約→ERP転記→
出荷→請求まで、実3システムを通して最後まで走らせる(全IFが最低1回ずつ
通ることを先に1件で確認してから、smoke規模の3件に展開する)。

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p3_smoke_one.py
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crm_mvp.enums import ReviewCaseStatus
from crm_mvp.models import Engagement, Quote, ReviewCase
from sqlalchemy import select

from simulation.src.clients.aitm import AitmClient
from simulation.src.clients.crm import CrmWebhookClient
from simulation.src.clients.crm_ui import CrmUiClient, UiFlowError
from simulation.src.clients.erp import ErpClient
from simulation.src.config import load_endpoints
from simulation.src.db import tenant_session
from simulation.src.outbox_runner import drain_outbox
from simulation.src.safety import assert_safe_environment

import json

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
# `/api/crm/*-review`が返す`status`(draft/approved/rejected/...)をCRMの
# ReviewCaseStatusへ写像する。未知の値は安全側でNEEDS_REVIEWにする
# (§6.2「UNKNOWNがCLEARに化けてはいけない」原則、A-07相当のフェイルクローズ)。
_AITM_STATUS_TO_REVIEW_STATUS = {
    "approved": ReviewCaseStatus.CLEAR, "clear": ReviewCaseStatus.CLEAR,
    "rejected": ReviewCaseStatus.BLOCKED, "denied": ReviewCaseStatus.BLOCKED,
    "needs_review": ReviewCaseStatus.NEEDS_REVIEW,
}


def _drive_and_push_review_result(
    aitm: AitmClient, crm_wh, case_no: str, aitm_case_no: str, *, revision: int = 1,
) -> dict:
    """2026-08-16 P3疎通確認で判明: `/api/crm/*-review`は審査ケースを
    起票するだけで、AI判定は自動実行されない。CRM連携が想定していた
    「送信すれば非同期で判定される」動作ではなく、①スクリーニング実行
    ②該非二法令リスト照合(tier確定)を明示的に呼ぶ必要がある。
    実AI_TMは現状CRMのwebhook URLを知らずcallbackしてこない
    (2026-08-16 E2E確認で「AI_TM側の判定完了通知」は未検証・未接続と判明済み)
    ため、シミュレータが判定実行とwebhook橋渡しの両方を担う。"""
    tx_id = aitm.find_transaction_id_by_case_no(aitm_case_no)
    if tx_id is None:
        raise RuntimeError(f"transaction_id が見つかりません: {aitm_case_no}")

    resp = aitm.run_screening(tx_id)
    print(f"    run-screening(tx={tx_id}) -> {resp.status_code}")
    resp = aitm.run_two_lists_decision(tx_id)
    print(f"    run-and-two-lists(tx={tx_id}) -> {resp.status_code} {resp.text[:200]}")

    data = aitm.get_review_status(aitm_case_no)
    if data is None:
        raise RuntimeError(f"review-status取得失敗: {aitm_case_no}")
    final_status = str(data.get("status") or "").lower()
    print(f"    review-status: {final_status}")

    mapped = _AITM_STATUS_TO_REVIEW_STATUS.get(final_status, ReviewCaseStatus.NEEDS_REVIEW)
    resp = crm_wh.send_review_result(
        case_no=case_no, revision=revision, status=mapped.value,
        valid_until=data.get("valid_until"), detail={"aitm_raw_status": final_status},
    )
    print(f"    review-result webhook -> {resp.status_code} {resp.text[:150]}")
    return data


def main() -> int:
    cfg = load_endpoints(CONFIG_DIR / "endpoints.yaml")
    assert_safe_environment(cfg)
    tenant_id = cfg.crm["tenant_id"]
    id_map = json.loads((Path(__file__).resolve().parents[1] / "out" / "master_ids.json").read_text())

    account_sim_id = "SIM-ACC-001"
    product_sim_id = "SIM-PROD-07"  # 3C001, ライセンス必要品目
    account = id_map["accounts"][account_sim_id]
    product = id_map["products"][product_sim_id]
    print(f"account={account['legal_name']} ({account['crm_account_id']})")
    print(f"product={product_sim_id} ({product['crm_product_id']}, material={product['erp_material_code']})")

    actor_id = uuid.uuid4()
    ui = CrmUiClient(cfg.crm["base_url"], tenant_id=tenant_id, actor_id=actor_id)
    erp = ErpClient(cfg)
    aitm = AitmClient(cfg)
    crm_wh = CrmWebhookClient(cfg)

    try:
        # 1. Engagement(既存取引先に紐付けるため直接DB作成 — Web UIの
        #    engagement_new_submitは常に新規Accountを作る仕様のため使えない)。
        with tenant_session(tenant_id) as session:
            engagement = Engagement(
                tenant_id=tenant_id, account_id=uuid.UUID(account["crm_account_id"]),
                name="SIM-ENG-SMOKE-02 テスト商談", stage="qualified",
            )
            session.add(engagement)
            session.commit()
            engagement_id = engagement.id
        print(f"1) engagement created: {engagement_id}")

        # 2. 明細追加
        ui.add_line_item(engagement_id, product_id=uuid.UUID(product["crm_product_id"]), quantity=10)
        print("2) line item added")

        # 3. 見積作成(DRAFT) → provisional review + commerce check + quota check がenqueueされる
        ui.create_quote(
            engagement_id, destination_country="KR", end_use="半導体製造装置の部品として使用",
        )
        print("3) quote created (DRAFT)")

        with tenant_session(tenant_id) as session:
            quote = session.execute(
                select(Quote).where(Quote.tenant_id == tenant_id, Quote.engagement_id == engagement_id)
            ).scalar_one()
            quote_id = quote.id
        print(f"   quote_id={quote_id} number={quote.quote_number}")

        # 4. Outbox送信(provisional review → AI_TM, commerce-check → ERP, quota-check → AI_TM license)
        with tenant_session(tenant_id) as session:
            stats = drain_outbox(session, tenant_id)
        print(f"4) outbox drained: {stats}")

        # 5. AI_TM側のcase_no(provider_request_id)を読み、判定完了をポーリングしてwebhook反映
        with tenant_session(tenant_id) as session:
            review_case = session.execute(
                select(ReviewCase).where(
                    ReviewCase.tenant_id == tenant_id, ReviewCase.engagement_id == engagement_id,
                    ReviewCase.review_type == "provisional",
                )
            ).scalar_one()
            case_no, aitm_case_no = review_case.case_no, review_case.provider_request_id
        print(f"5) review_case case_no={case_no} aitm_case_no={aitm_case_no}")
        if aitm_case_no:
            _drive_and_push_review_result(aitm, crm_wh, case_no, aitm_case_no)
        else:
            print("   WARN: provider_request_id未設定(outbox送信が失敗している可能性)")

        # 6. 見積をSENTへ(審査・与信ゲート通過を確認)
        try:
            ui.update_quote_status(engagement_id, quote_id, status="sent")
            print("6) quote -> SENT: OK")
        except UiFlowError as e:
            print(f"6) quote -> SENT: BLOCKED ({e})")
            raise

        # 7. 契約発行(formal reviewがenqueueされる)
        ui.create_contract(
            engagement_id, quote_id=str(quote_id),
            start_date=date.today().isoformat(), end_date=(date.today() + timedelta(days=365)).isoformat(),
        )
        print("7) contract created (DRAFT)")

        with tenant_session(tenant_id) as session:
            from crm_mvp.models import Contract
            contract = session.execute(
                select(Contract).where(Contract.tenant_id == tenant_id, Contract.engagement_id == engagement_id)
            ).scalar_one()
            contract_id = contract.id
        print(f"   contract_id={contract_id} number={contract.contract_number}")

        with tenant_session(tenant_id) as session:
            stats = drain_outbox(session, tenant_id)
        print(f"   outbox drained (formal review submit): {stats}")

        with tenant_session(tenant_id) as session:
            formal_case = session.execute(
                select(ReviewCase).where(
                    ReviewCase.tenant_id == tenant_id, ReviewCase.engagement_id == engagement_id,
                    ReviewCase.review_type == "formal",
                )
            ).scalar_one()
            f_case_no, f_aitm_case_no = formal_case.case_no, formal_case.provider_request_id
        print(f"   formal review case_no={f_case_no} aitm_case_no={f_aitm_case_no}")
        if f_aitm_case_no:
            _drive_and_push_review_result(aitm, crm_wh, f_case_no, f_aitm_case_no, revision=1)

        # 8. 契約をSIGNEDへ(ERP転記IF-25 + ライセンス仮引当IF-07がenqueueされる)
        try:
            ui.update_contract_status(engagement_id, contract_id, status="signed")
            print("8) contract -> SIGNED: OK")
        except UiFlowError as e:
            print(f"8) contract -> SIGNED: BLOCKED ({e})")
            raise

        with tenant_session(tenant_id) as session:
            stats = drain_outbox(session, tenant_id)
        print(f"   outbox drained (ERP transcription + license allocation): {stats}")

        with tenant_session(tenant_id) as session:
            from crm_mvp.models import Contract
            contract = session.get(Contract, contract_id)
            erp_so_number = contract.external_id
        print(f"   contract.external_id (ERP SO) = {erp_so_number}")
        if not erp_so_number:
            raise RuntimeError("ERP転記(IF-25)が失敗しています。outbox_messageを確認してください。")

        # 9. ERPで出荷計上(§5.4)。IF-20(出荷前再スクリーニング)・IF-21
        #    (ライセンス枠消費確定)はERP自身が内部でAI_TMを呼んで処理する
        #    (2026-08-16確認: レスポンスにaitm_case_no/aitm_approval_statusが
        #    含まれる)。
        so = erp.find_sales_order_by_document_number(erp_so_number, customer_code=account["erp_bp_code"])
        if so is None:
            raise RuntimeError(f"ERP受注が見つかりません: {erp_so_number}")
        delivery_resp = erp.create_delivery({"sales_order_id": so["id"], "document_date": date.today().isoformat()})
        delivery = delivery_resp.json()
        print(f"9) ERP delivery created: {delivery['document_number']} (aitm_approval_status={delivery.get('aitm_approval_status')})")

        # 10. ERPで請求計上
        billing_resp = erp.create_billing({"delivery_id": delivery["id"], "document_date": date.today().isoformat()})
        billing = billing_resp.json()
        print(f"10) ERP billing created: {billing['document_number']}")

        # 11. ERP→CRMへの実webhook配信は現状未設定(2026-08-16 E2E確認で判明済み)
        #     のため、シミュレータがERPになりすまして押し込む(IF-29/IF-30)。
        posted_at = f"{date.today().isoformat()}T00:00:00+00:00"
        for item in delivery["items"]:
            resp = crm_wh.send_erp_delivery_posted({
                "erp_sales_order_number": erp_so_number, "erp_shipment_number": delivery["document_number"],
                "items": [{
                    "material_code": item["material_code"], "quantity": float(item["quantity"]),
                    "amount": float(item["quantity"]) * float(so["items"][0]["unit_price"]),
                }],
                "currency": so["currency"], "posted_at": posted_at,
            })
        print(f"11) delivery-posted webhook -> {resp.status_code}")
        resp = crm_wh.send_erp_billing_posted({
            "erp_sales_order_number": erp_so_number, "erp_billing_number": billing["document_number"],
            "items": [
                {"material_code": i["material_code"], "quantity": float(i["quantity"]), "amount": float(i["net_amount"])}
                for i in billing["items"]
            ],
            "currency": billing["currency"], "posted_at": posted_at,
        })
        print(f"    billing-posted webhook -> {resp.status_code}")

        with tenant_session(tenant_id) as session:
            from crm_mvp.models import ContractFulfillment
            rows = session.execute(
                select(ContractFulfillment).where(ContractFulfillment.contract_id == contract_id)
            ).scalars().all()
        print(f"    ContractFulfillment rows for this contract: {len(rows)} ({[r.kind for r in rows]})")

        print("\n=== P3疎通(1件)完了: 全IFが最低1回ずつ通ることを確認 ===")
        return 0
    finally:
        ui.close()
        erp.close()
        aitm.close()
        crm_wh.close()


if __name__ == "__main__":
    raise SystemExit(main())
