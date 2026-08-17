"""AI_TM/ERP側の対応完了を受けた確認dry-run。

report_to_AITM_team.md / report_to_ERP_team.md で報告した各事項について、
実際に直り具合を検証する。特に重要な確認ポイント:
  1. AI_TMの審査判定が(明示トリガー無しで)自動的に完了するか
  2. AI_TMがCRMのwebhookへ自分から結果をpushしてくるようになったか
     (=シミュレータ側の橋渡し(review-result送信)が不要になったか)

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p6_verify_partner_fixes.py
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crm_mvp.models import Engagement, Quote, ReviewCase
from sqlalchemy import select

from simulation.src.clients.aitm import AitmClient
from simulation.src.clients.crm_ui import CrmUiClient
from simulation.src.config import load_endpoints
from simulation.src.db import tenant_session
from simulation.src.outbox_runner import drain_outbox
from simulation.src.safety import assert_safe_environment

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def main() -> int:
    cfg = load_endpoints(CONFIG_DIR / "endpoints.yaml")
    assert_safe_environment(cfg)
    tenant_id = cfg.crm["tenant_id"]
    id_map = json.loads((Path(__file__).resolve().parents[1] / "out" / "master_ids.json").read_text())

    acc = id_map["accounts"]["SIM-ACC-003"]  # "SIM Trading 01" — ウォッチリストと無関係のクリーンな取引先
    prod = id_map["products"]["SIM-PROD-03"]  # EAR99・マッピング済み

    ui = CrmUiClient(cfg.crm["base_url"], tenant_id=tenant_id, actor_id=uuid.uuid4())
    aitm = AitmClient(cfg)

    try:
        with tenant_session(tenant_id) as session:
            engagement = Engagement(
                tenant_id=tenant_id, account_id=uuid.UUID(acc["crm_account_id"]),
                name="SIM-DRYRUN-VERIFY 対応確認商談", stage="qualified",
            )
            session.add(engagement)
            session.commit()
            engagement_id = engagement.id
        print(f"1) engagement created: {engagement_id}")

        ui.add_line_item(engagement_id, product_id=uuid.UUID(prod["crm_product_id"]), quantity=5)
        ui.create_quote(engagement_id, destination_country="KR", end_use="dry-run verification")
        print("2) quote created (DRAFT)")

        with tenant_session(tenant_id) as session:
            quote = session.execute(
                select(Quote).where(Quote.tenant_id == tenant_id, Quote.engagement_id == engagement_id)
            ).scalars().first()
            quote_id = quote.id

        with tenant_session(tenant_id) as session:
            stats = drain_outbox(session, tenant_id)
        print(f"3) outbox drained (provisional review submitted to AI_TM): {stats}")

        with tenant_session(tenant_id) as session:
            review_case = session.execute(
                select(ReviewCase).where(
                    ReviewCase.tenant_id == tenant_id, ReviewCase.engagement_id == engagement_id,
                    ReviewCase.review_type == "provisional",
                )
            ).scalars().first()
            case_no, aitm_case_no = review_case.case_no, review_case.provider_request_id
        print(f"4) review_case case_no={case_no} aitm_case_no={aitm_case_no}")

        # --- 確認1: AI_TM側は何もトリガーせずに判定が完了するか ---
        print("\n=== 確認1: AI_TM側の自動判定(スクリーニング/該非トリガーを一切呼ばない) ===")
        for i in range(12):
            data = aitm.get_review_status(aitm_case_no)
            status = data.get("status")
            print(f"  t+{i * 5}s: AI_TM側 status={status}")
            if status not in ("draft", None):
                break
            time.sleep(5)

        # --- 確認2: CRM側のReviewCaseは、シミュレータが何もwebhookを送らなくても更新されるか ---
        print("\n=== 確認2: CRM側ReviewCaseの自動更新(webhook橋渡しを一切行わない) ===")
        for i in range(6):
            with tenant_session(tenant_id) as session:
                rc = session.get(ReviewCase, review_case.id)
                session.refresh(rc)
                print(f"  t+{i * 5}s: CRM側 ReviewCase.status={rc.status}")
            time.sleep(5)

        print("\n=== 結果 ===")
        final_aitm = aitm.get_review_status(aitm_case_no)
        with tenant_session(tenant_id) as session:
            rc = session.get(ReviewCase, review_case.id)
            print(f"AI_TM側最終status: {final_aitm.get('status')}")
            print(f"CRM側最終status:   {rc.status}")
            if final_aitm.get("status") not in ("draft", None) and rc.status == "pending":
                print("\n判定: AI_TM側は自動判定に対応済み。ただしCRMへのwebhook push は依然未接続"
                      "(CRM側は今も判定完了を知る手段がなく、シミュレータの橋渡しが引き続き必要)。")
            elif rc.status != "pending":
                print("\n判定: CRM側ReviewCaseが自動的に更新された = AI_TMからのwebhook pushが機能している。")
            else:
                print("\n判定: AI_TM側の自動判定も未確認(状態が変化しなかった)。")
    finally:
        ui.close()
        aitm.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
