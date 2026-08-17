"""契約〜出荷〜請求(docs/BULK_SIMULATION_SPEC.md §5.4)。

IF-20(出荷前再スクリーニング)・IF-21(ライセンス枠消費確定)はERP自身が
内部でAI_TMを呼んで処理する(2026-08-16確認: `/sd/deliveries`のレスポンスに
`aitm_case_no`/`aitm_approval_status`が含まれる)。ERP→CRMへの実webhook配信は
現状未設定(2026-08-16 E2E確認で判明済み)のため、シミュレータがERPに
なりすまして`/webhooks/erp/*`へ押し込む(IF-29/IF-30)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..clients.crm import CrmWebhookClient
from ..clients.erp import ErpClient


@dataclass
class FulfillmentResult:
    delivery_document_number: str | None = None
    billing_document_number: str | None = None
    outcome: str = "unknown"  # completed | erp_delivery_failed | erp_billing_failed | webhook_failed
    reason: str | None = None


def ship_and_bill(
    erp: ErpClient, crm_wh: CrmWebhookClient, *, erp_so_number: str, erp_bp_code: str | None,
) -> FulfillmentResult:
    result = FulfillmentResult()

    so = erp.find_sales_order_by_document_number(erp_so_number, customer_code=erp_bp_code)
    if so is None:
        result.outcome, result.reason = "erp_delivery_failed", f"ERP受注が見つかりません: {erp_so_number}"
        return result

    delivery_resp = erp.create_delivery({"sales_order_id": so["id"], "document_date": date.today().isoformat()})
    if delivery_resp.status_code not in (200, 201):
        result.outcome, result.reason = "erp_delivery_failed", f"{delivery_resp.status_code} {delivery_resp.text[:200]}"
        return result
    delivery = delivery_resp.json()
    result.delivery_document_number = delivery["document_number"]

    billing_resp = erp.create_billing({"delivery_id": delivery["id"], "document_date": date.today().isoformat()})
    if billing_resp.status_code not in (200, 201):
        result.outcome, result.reason = "erp_billing_failed", f"{billing_resp.status_code} {billing_resp.text[:200]}"
        return result
    billing = billing_resp.json()
    result.billing_document_number = billing["document_number"]

    posted_at = f"{date.today().isoformat()}T00:00:00+00:00"
    unit_price_by_material = {i["material_code"]: float(i["unit_price"]) for i in so["items"]}
    delivery_resp2 = crm_wh.send_erp_delivery_posted({
        "erp_sales_order_number": erp_so_number, "erp_shipment_number": delivery["document_number"],
        "items": [
            {
                "material_code": item["material_code"], "quantity": float(item["quantity"]),
                "amount": float(item["quantity"]) * unit_price_by_material.get(item["material_code"], 0.0),
            }
            for item in delivery["items"]
        ],
        "currency": so["currency"], "posted_at": posted_at,
    })
    billing_resp2 = crm_wh.send_erp_billing_posted({
        "erp_sales_order_number": erp_so_number, "erp_billing_number": billing["document_number"],
        "items": [
            {"material_code": i["material_code"], "quantity": float(i["quantity"]), "amount": float(i["net_amount"])}
            for i in billing["items"]
        ],
        "currency": billing["currency"], "posted_at": posted_at,
    })
    if delivery_resp2.status_code != 200 or billing_resp2.status_code != 200:
        result.outcome = "webhook_failed"
        result.reason = f"delivery={delivery_resp2.status_code} billing={billing_resp2.status_code}"
        return result

    result.outcome = "completed"
    return result
