"""2年間の運用シミュレーション: 過去実績の商談を積み増す。

  python scripts/seed_demo_2yr_backfill.py --tenant-id <uuid>

scripts/seed_demo_pipeline.py が作る「直近の商談6件」はそのまま残し、
そこに過去24ヶ月分の商談を追加する(2026-08-13 ユーザー要望: 仮想セール
スチームによる2年間の運用シミュレーション)。中規模(追加15件、既存6件と
合わせて21件)。

構成:
  - 完全な更新チェーン(親→更新→さらにUpsell)を1本: Sunrise Logic Devices
    は初回契約→1回更新済みで、更新後の契約が「もうすぐ更新対象」になる
  - NSC Taiwan Materials は初回契約→更新→その更新にUpsell、という3世代の
    親子構造で「普遍的な構造」が世代を跨いで機能することを示す
  - 更新が一度も起きずに契約期限が過ぎている「放置された契約」を1件
    (Eurasia Specialty Materials)— 更新管理が本当に必要な理由を残す
  - 親子関係を意図的に張らない「独立した再商談」も複数(Pacific Electronic
    Chemicals, Northern Silicon Technologies, Helios Memory Systems, Apex
    Foundry の一部)— 過去の小口取引とは無関係に新規RFPで戻ってきたケース
  - 失注2件(価格負け・予算凍結)

本スクリプトは商談データを追加するのみで、既存データは削除しない。
先に scripts/seed_demo_pipeline.py と scripts/apply_fert_pricing.py を
実行しておくこと(取引先マスタ・商品価格表・ゲートポリシーが必要)。

§7.4: crm_app ロール(非 superuser)で接続し、RLS のテナント文脈を
自分で SET してから実行する。
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.enums import (
    AccessLevel, BuyingCenterRole, ContractStatus, EngagementRelationshipType,
    ProposalStatus, QuoteStatus, SourceKind, Stage, Stance,
)
from crm_mvp.models import (
    Account, Engagement, ErpBusinessPartner, ExtractionProposal, GraphNode,
    Product, QualificationSlot,
)
from crm_mvp.schemas.extraction import ExtractedClaim
from crm_mvp.services.apply_proposal import apply_proposal
from crm_mvp.services.contacts import link_contact_to_engagement
from crm_mvp.services.engagement_relationships import create_child_engagement
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.quoting import (
    create_contract, create_quote_from_engagement, update_contract_status,
    update_quote_status,
)
from crm_mvp.services.stage_transitions import apply_stage_transition

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def date_days_ago(n: float) -> date:
    return (NOW - timedelta(days=n)).date()


def date_days_from_now(n: float) -> date:
    return (NOW + timedelta(days=n)).date()


class FastDealBuilder:
    """seed_demo_pipeline.DealBuilder の簡潔版。過去実績の大量投入向けに、
    フル・ナラティブ(エビデンス引用の作り込み)は省き、ゲートを通すのに
    必要な最小限のクオリフィケーションだけを1商談あたり数行で済ませる。"""

    def __init__(self, session: Session):
        self.session = session

    def create_lead(self, bp_code: str, engagement_name: str, created: datetime):
        bp = self.session.execute(
            select(ErpBusinessPartner).where(
                ErpBusinessPartner.tenant_id == TENANT_ID,
                ErpBusinessPartner.bp_code == bp_code,
            )
        ).scalar_one()
        account = self.session.execute(
            select(Account).where(
                Account.tenant_id == TENANT_ID, Account.external_system == "erp",
                Account.external_id == bp.bp_code,
            )
        ).scalar_one_or_none()
        if account is None:
            account = Account(
                tenant_id=TENANT_ID, name=bp.name, country=bp.country,
                external_system="erp", external_id=bp.bp_code,
            )
            self.session.add(account)
            self.session.flush()
        engagement = Engagement(
            tenant_id=TENANT_ID, account_id=account.id, name=engagement_name,
            stage=Stage.LEAD, created_at=created,
        )
        self.session.add(engagement)
        self.session.flush()
        return account, engagement

    def spawn_child(
        self, parent: Engagement, *, relationship_type: EngagementRelationshipType,
        name: str, created: datetime,
    ) -> Engagement:
        child = create_child_engagement(
            self.session, TENANT_ID, parent, relationship_type=relationship_type, name=name,
        )
        child.created_at = created
        self.session.flush()
        return child

    def add_products(self, engagement, items: list[tuple[str, int, str]]):
        for name, quantity, discount_rate in items:
            product = self.session.execute(
                select(Product).where(Product.tenant_id == TENANT_ID, Product.name == name)
            ).scalar_one()
            add_line_item(
                self.session, TENANT_ID, engagement, product=product,
                quantity=quantity, discount_rate=Decimal(discount_rate),
            )

    def _ingest(self, engagement, raw_text: str, when: datetime):
        from crm_mvp.models import IngestionSource
        source = IngestionSource(
            tenant_id=TENANT_ID, engagement_id=engagement.id, kind=SourceKind.TRANSCRIPT,
            raw_text=raw_text, occurred_at=when, created_at=when,
            processed_at=when, extractor_version="demo-scripted-v1-backfill",
        )
        self.session.add(source)
        self.session.flush()
        return source

    def _propose(self, engagement, source, *, field_path, value, when, model_score=0.8):
        from crm_mvp.enums import Criterion
        claim = ExtractedClaim(
            target_type="qualification_slot", field_path=field_path, value=value,
            model_score=model_score, rationale="過去実績の簡易再現",
            evidence_quote=source.raw_text[:80],
        )
        proposal = ExtractionProposal(
            tenant_id=TENANT_ID, source_id=source.id, engagement_id=engagement.id,
            target_type=claim.target_type, field_path=claim.field_path,
            proposed_value=claim.value, model_score=claim.model_score,
            rationale=claim.rationale, evidence_quote=claim.evidence_quote,
            status=ProposalStatus.PENDING, created_at=when,
        )
        self.session.add(proposal)
        self.session.flush()

        apply_proposal(self.session, proposal)
        proposal.status = ProposalStatus.ACCEPTED
        proposal.decided_by = ACTOR_ID
        proposal.decided_at = when
        criterion = Criterion(field_path.split(":", 1)[1])
        slot = self.session.execute(
            select(QualificationSlot).where(
                QualificationSlot.tenant_id == TENANT_ID,
                QualificationSlot.engagement_id == engagement.id,
                QualificationSlot.criterion == criterion,
            )
        ).scalar_one()
        slot.asserted_at = when
        # decays_at はあえて設定しない。compute_decays_at は "when からNN日"を
        # 返すため、when が数百日前だと実時刻基準のゲート判定(meets())が
        # 即座に「失効済み」と見なしてしまい、ステージ遷移がブロックされる
        # (QualificationSlot.meets: decays_at <= now なら問答無用で False)。
        # 過去実績の再現が目的でリアルタイムの鮮度管理は対象外のため、
        # decays_at=None(失効なし)のままにする。
        self.session.flush()

    def advance(self, engagement, to_stage: Stage, when: datetime):
        outcome = apply_stage_transition(
            self.session, TENANT_ID, engagement, to_stage, actor=f"human:{ACTOR_ID}",
        )
        if not outcome.allowed:
            missing = [m.reason for m in outcome.result.missing]
            raise RuntimeError(
                f"{engagement.name}: blocked {engagement.stage} -> {to_stage}: {missing}"
            )
        outcome.transition.occurred_at = when
        self.session.flush()

    def close_deal_fast(
        self, engagement, *, pain: str, timing_days: int, timing_driver: str,
        metrics_kpi: str, metrics_value: int, decision_criteria: list[str],
        budget_amount: int, fiscal_period: str, decider_title: str,
        paper_layers: int, legal_review: bool, competitors: list[str],
        incumbent: str | None,
        when_prospect: datetime, when_qualified: datetime, when_proposal: datetime,
        when_negotiation: datetime, when_won: datetime,
    ):
        """Prospect〜Closed Wonまでを最小限のエビデンスで一気に進める。"""
        src = self._ingest(engagement, pain, when_prospect)
        self._propose(engagement, src, field_path="criterion:identified_pain",
                       value={"summary": pain}, when=when_prospect)
        self._propose(engagement, src, field_path="criterion:timing",
                       value={"target_date": str(date.today() + timedelta(days=timing_days)),
                              "driver": timing_driver}, when=when_prospect)
        self.advance(engagement, Stage.PROSPECT, when=when_prospect)

        self._propose(engagement, src, field_path="criterion:metrics",
                       value={"kpi": metrics_kpi, "annual_value": metrics_value},
                       when=when_qualified)
        self.advance(engagement, Stage.QUALIFIED, when=when_qualified)

        self._propose(engagement, src, field_path="criterion:decision_criteria",
                       value={"criteria": decision_criteria}, when=when_proposal)
        self._propose(engagement, src, field_path="criterion:budget",
                       value={"amount": budget_amount, "fiscal_period": fiscal_period,
                              "secured": True}, when=when_proposal)
        self.advance(engagement, Stage.PROPOSAL, when=when_proposal)

        decider_node = GraphNode(
            tenant_id=TENANT_ID, account_id=engagement.account_id,
            placeholder_label=f"{decider_title}(氏名未確認)", seniority_layer=3,
            created_at=when_negotiation,
        )
        self.session.add(decider_node)
        self.session.flush()
        # path_to_decider ゲートは「接触済(CONTACTED/ENGAGED)のノードから
        # decider ロールのノードへの経路」を探す。中間者を作らず決裁者を
        # 直接 ENGAGED にすることで、決裁者自身が起点かつ終点になる
        # 0ホップの経路として自己充足させる(元スクリプトの deal 6 と同じ
        # パターン — 「決裁は私の権限で完結します」を再現)。
        role = link_contact_to_engagement(
            self.session, TENANT_ID, engagement, decider_node,
            roles=[BuyingCenterRole.DECIDER.value], stance=Stance.UNKNOWN,
            access_level=AccessLevel.ENGAGED, written_by=f"human:{ACTOR_ID}",
        )
        role.last_touched_at = when_negotiation
        role.created_at = when_negotiation
        self.session.flush()

        self._propose(engagement, src, field_path="criterion:economic_buyer",
                       value={"node_id": str(decider_node.id), "title": decider_title},
                       when=when_negotiation)
        self._propose(engagement, src, field_path="criterion:paper_process",
                       value={"approval_layers": paper_layers,
                              "legal_review_required": legal_review}, when=when_negotiation)
        self._propose(engagement, src, field_path="criterion:competition",
                       value={"vendors": competitors, "incumbent": incumbent},
                       when=when_negotiation)
        self.advance(engagement, Stage.NEGOTIATION, when=when_negotiation)

        self.advance(engagement, Stage.CLOSED_WON, when=when_won)

    def lose_from_prospect(
        self, engagement, *, pain: str, timing_days: int, timing_driver: str,
        lost_reason: str, when_prospect: datetime, when_lost: datetime,
    ):
        src = self._ingest(engagement, pain, when_prospect)
        self._propose(engagement, src, field_path="criterion:identified_pain",
                       value={"summary": pain}, when=when_prospect)
        self._propose(engagement, src, field_path="criterion:timing",
                       value={"target_date": str(date.today() + timedelta(days=timing_days)),
                              "driver": timing_driver}, when=when_prospect)
        self.advance(engagement, Stage.PROSPECT, when=when_prospect)
        engagement.lost_reason = lost_reason
        self.advance(engagement, Stage.CLOSED_LOST, when=when_lost)

    def lose_from_proposal(
        self, engagement, *, pain: str, timing_days: int, timing_driver: str,
        metrics_kpi: str, metrics_value: int, decision_criteria: list[str],
        budget_amount: int, fiscal_period: str, lost_reason: str,
        when_prospect: datetime, when_qualified: datetime, when_proposal: datetime,
        when_lost: datetime,
    ):
        src = self._ingest(engagement, pain, when_prospect)
        self._propose(engagement, src, field_path="criterion:identified_pain",
                       value={"summary": pain}, when=when_prospect)
        self._propose(engagement, src, field_path="criterion:timing",
                       value={"target_date": str(date.today() + timedelta(days=timing_days)),
                              "driver": timing_driver}, when=when_prospect)
        self.advance(engagement, Stage.PROSPECT, when=when_prospect)

        self._propose(engagement, src, field_path="criterion:metrics",
                       value={"kpi": metrics_kpi, "annual_value": metrics_value},
                       when=when_qualified)
        self.advance(engagement, Stage.QUALIFIED, when=when_qualified)

        self._propose(engagement, src, field_path="criterion:decision_criteria",
                       value={"criteria": decision_criteria}, when=when_proposal)
        self._propose(engagement, src, field_path="criterion:budget",
                       value={"amount": budget_amount, "fiscal_period": fiscal_period,
                              "secured": True}, when=when_proposal)
        self.advance(engagement, Stage.PROPOSAL, when=when_proposal)

        engagement.lost_reason = lost_reason
        self.advance(engagement, Stage.CLOSED_LOST, when=when_lost)

    def wire_contract(
        self, engagement, *, when: datetime, start_date: date, end_date: date,
        final_status: ContractStatus = ContractStatus.ACTIVE,
    ):
        """商品構成が確定した受注案件に見積もり・契約を発行し、期間・最終
        ステータスを過去実績用に上書きする。"""
        actor = f"human:{ACTOR_ID}"
        quote = create_quote_from_engagement(
            self.session, TENANT_ID, engagement, valid_until=None, actor=actor,
        )
        quote.created_at = when
        update_quote_status(quote, QuoteStatus.SENT)
        update_quote_status(quote, QuoteStatus.ACCEPTED)

        contract = create_contract(
            self.session, TENANT_ID, engagement, quote=quote,
            start_date=start_date, end_date=end_date, actor=actor,
        )
        contract.created_at = when
        update_contract_status(contract, ContractStatus.SIGNED)
        update_contract_status(contract, ContractStatus.ACTIVE)
        contract.status = final_status
        self.session.flush()
        return quote, contract


# --- A1: Sunrise Logic Devices — 初回契約→1回更新済み(更新後の契約が
#         まもなく2回目の更新対象になる) -------------------------------------

def build_a1_sunrise(b: FastDealBuilder):
    _, parent = b.create_lead("BP-3000003", "エッチング薬液 初回採用契約", days_ago(705))
    b.add_products(parent, [
        ("SC-1 Cleaning Solution (Standard)", 300, "0"),
        ("Photoresist Stripping Solution NMP-Free NSC-STR02", 150, "0"),
    ])
    b.close_deal_fast(
        parent, pain="洗浄工程のパーティクル残留を改善したい", timing_days=-600,
        timing_driver="工程改善計画", metrics_kpi="パーティクル数", metrics_value=6000000,
        decision_criteria=["価格", "供給安定性"], budget_amount=6000000,
        fiscal_period="2025年度", decider_title="製造部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(700), when_qualified=days_ago(695),
        when_proposal=days_ago(690), when_negotiation=days_ago(687), when_won=days_ago(685),
    )
    b.wire_contract(
        parent, when=days_ago(685),
        start_date=date_days_ago(685), end_date=date_days_ago(320),
        final_status=ContractStatus.TERMINATED,
    )

    child = b.spawn_child(
        parent, relationship_type=EngagementRelationshipType.RENEWAL,
        name="エッチング薬液 初回採用契約(更新)", created=days_ago(340),
    )
    b.add_products(child, [
        ("SC-1 Cleaning Solution (Standard)", 350, "5"),
        ("Photoresist Stripping Solution NMP-Free NSC-STR02", 180, "5"),
        ("Cleaning Chemical Kit SC-2 Formulation", 100, "0"),
    ])
    b.close_deal_fast(
        child, pain="供給契約の更新時期のため条件を見直したい", timing_days=-330,
        timing_driver="契約更新", metrics_kpi="パーティクル数", metrics_value=7000000,
        decision_criteria=["価格", "供給安定性"], budget_amount=8500000,
        fiscal_period="2025年度", decider_title="製造部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(335), when_qualified=days_ago(330),
        when_proposal=days_ago(325), when_negotiation=days_ago(322), when_won=days_ago(320),
    )
    b.wire_contract(
        child, when=days_ago(320),
        start_date=date_days_ago(320), end_date=date_days_from_now(45),
        final_status=ContractStatus.ACTIVE,
    )


# --- A2: NSC Taiwan Materials — 初回契約→更新→さらにUpsell(3世代) --------

def build_a2_nsc_taiwan(b: FastDealBuilder):
    _, gen1 = b.create_lead("BP-1000002", "CMPスラリー年間供給契約", days_ago(550))
    b.add_products(gen1, [
        ("CMP Slurry for Tungsten NSC-WSL30", 300, "0"),
        ("CMP Slurry for Copper NSC-CuS18", 200, "0"),
    ])
    b.close_deal_fast(
        gen1, pain="タングステンCMP工程の消耗品コストを最適化したい", timing_days=-450,
        timing_driver="年間調達計画", metrics_kpi="消耗品コスト", metrics_value=3000000,
        decision_criteria=["価格", "納期"], budget_amount=15000000,
        fiscal_period="2025年度", decider_title="採購部長", paper_layers=1,
        legal_review=False, competitors=["現行サプライヤー"], incumbent="現行サプライヤー",
        when_prospect=days_ago(545), when_qualified=days_ago(540),
        when_proposal=days_ago(535), when_negotiation=days_ago(532), when_won=days_ago(530),
    )
    b.wire_contract(
        gen1, when=days_ago(530),
        start_date=date_days_ago(530), end_date=date_days_ago(165),
        final_status=ContractStatus.TERMINATED,
    )

    gen2 = b.spawn_child(
        gen1, relationship_type=EngagementRelationshipType.RENEWAL,
        name="CMPスラリー年間供給契約(更新)", created=days_ago(185),
    )
    b.add_products(gen2, [
        ("CMP Slurry for Tungsten NSC-WSL30", 350, "5"),
        ("CMP Slurry for Copper NSC-CuS18", 250, "5"),
    ])
    b.close_deal_fast(
        gen2, pain="増産に伴いCMPスラリーの調達量を見直したい", timing_days=-160,
        timing_driver="契約更新", metrics_kpi="消耗品コスト", metrics_value=4000000,
        decision_criteria=["価格", "納期"], budget_amount=20000000,
        fiscal_period="2025年度", decider_title="採購部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(180), when_qualified=days_ago(175),
        when_proposal=days_ago(170), when_negotiation=days_ago(167), when_won=days_ago(165),
    )
    b.wire_contract(
        gen2, when=days_ago(165),
        start_date=date_days_ago(165), end_date=date_days_from_now(200),
        final_status=ContractStatus.ACTIVE,
    )

    gen3 = b.spawn_child(
        gen2, relationship_type=EngagementRelationshipType.UPSELL,
        name="CMPスラリー 増量・高純度グレード切替", created=days_ago(60),
    )
    b.add_products(gen3, [
        ("W-CMP Slurry Advanced H2O2-free NSC-WSL55", 100, "0"),
    ])
    b.close_deal_fast(
        gen3, pain="先端ノードで高純度グレードへの切替が必要", timing_days=-30,
        timing_driver="次世代プロセス立ち上げ", metrics_kpi="歩留まり", metrics_value=5000000,
        decision_criteria=["性能"], budget_amount=3600000,
        fiscal_period="2026年度", decider_title="採購部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(55), when_qualified=days_ago(50),
        when_proposal=days_ago(45), when_negotiation=days_ago(42), when_won=days_ago(40),
    )


# --- A3: NSC Korea Advanced Materials — 単発(まもなく初回更新対象) --------

def build_a3_nsc_korea(b: FastDealBuilder):
    _, eng = b.create_lead("BP-1000003", "洗浄薬液 追加ライン契約", days_ago(300))
    b.add_products(eng, [
        ("Buffered HF Etch Solution 50:1 BOE-50", 150, "0"),
        ("SC-1 Cleaning Solution (Standard)", 200, "0"),
    ])
    b.close_deal_fast(
        eng, pain="新設ラインの洗浄薬液サプライヤーを確保したい", timing_days=-260,
        timing_driver="新ライン立ち上げ", metrics_kpi="立ち上げ遅延リスク", metrics_value=2000000,
        decision_criteria=["納期", "品質実績"], budget_amount=7500000,
        fiscal_period="2025年度", decider_title="生産技術部長", paper_layers=2,
        legal_review=False, competitors=["現地サプライヤー"], incumbent=None,
        when_prospect=days_ago(295), when_qualified=days_ago(290),
        when_proposal=days_ago(285), when_negotiation=days_ago(282), when_won=days_ago(280),
    )
    b.wire_contract(
        eng, when=days_ago(280),
        start_date=date_days_ago(280), end_date=date_days_from_now(85),
        final_status=ContractStatus.ACTIVE,
    )


# --- A4: Eurasia Specialty Materials — 更新されずに放置された契約 --------

def build_a4_eurasia(b: FastDealBuilder):
    _, eng = b.create_lead("BP-2000002", "プロセスガス供給契約", days_ago(430))
    b.add_products(eng, [
        ("High Purity Silane Gas SiH4 6N", 50, "0"),
        ("TEOS PECVD Precursor Solution (high-purity blend)", 80, "0"),
    ])
    b.close_deal_fast(
        eng, pain="成膜プロセス用の高純度ガス供給元を確保したい", timing_days=-390,
        timing_driver="プロセス移管計画", metrics_kpi="供給リードタイム", metrics_value=1500000,
        decision_criteria=["純度", "価格"], budget_amount=14000000,
        fiscal_period="2024年度", decider_title="調達部長", paper_layers=2,
        legal_review=True, competitors=["欧州系サプライヤー"], incumbent=None,
        when_prospect=days_ago(425), when_qualified=days_ago(420),
        when_proposal=days_ago(415), when_negotiation=days_ago(412), when_won=days_ago(410),
    )
    b.wire_contract(
        eng, when=days_ago(410),
        start_date=date_days_ago(410), end_date=date_days_ago(45),
        final_status=ContractStatus.ACTIVE,  # 意図的に放置: 期限超過だが誰も更新していない
    )


# --- B1: Apex Foundry Corporation — 過去の小口取引 + Cross-sell ------------

def build_b1_apex(b: FastDealBuilder):
    _, spare = b.create_lead("BP-3000001", "制御ICサンプル発注", days_ago(610))
    b.add_products(spare, [("高精度ハイブリッドコントローラーIC", 30, "0")])
    b.close_deal_fast(
        spare, pain="既存制御ICの代替供給元を評価したい", timing_days=-580,
        timing_driver="部材調達計画", metrics_kpi="調達リスク", metrics_value=500000,
        decision_criteria=["納期"], budget_amount=1000000,
        fiscal_period="2024年度", decider_title="購買課長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(605), when_qualified=days_ago(600),
        when_proposal=days_ago(595), when_negotiation=days_ago(592), when_won=days_ago(590),
    )
    # 単発のPOベース取引のため見積・契約は発行しない(すべての受注が
    # フォーマルな契約書を伴うわけではない、という現実的なばらつき)。

    cross = b.spawn_child(
        spare, relationship_type=EngagementRelationshipType.CROSS_SELL,
        name="エッチング・洗浄薬液 供給契約", created=days_ago(460),
    )
    b.add_products(cross, [
        ("Buffered HF Etch Solution 50:1 BOE-50", 100, "0"),
        ("Cleaning Chemical Kit SC-2 Formulation", 80, "0"),
    ])
    b.close_deal_fast(
        cross, pain="制御基板の洗浄工程用に薬液サプライヤーを拡張したい", timing_days=-430,
        timing_driver="工程拡張計画", metrics_kpi="洗浄コスト", metrics_value=2000000,
        decision_criteria=["対応スピード", "価格"], budget_amount=6300000,
        fiscal_period="2024年度", decider_title="製造本部長", paper_layers=2,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(455), when_qualified=days_ago(450),
        when_proposal=days_ago(445), when_negotiation=days_ago(442), when_won=days_ago(440),
    )
    b.wire_contract(
        cross, when=days_ago(440),
        start_date=date_days_ago(440), end_date=date_days_from_now(290),
        final_status=ContractStatus.ACTIVE,
    )


# --- B2: Pacific Electronic Chemicals — 独立した過去のトライアル契約 ------

def build_b2_pacific(b: FastDealBuilder):
    _, eng = b.create_lead("BP-2000001", "洗浄薬液サンプル評価契約", days_ago(390))
    b.add_products(eng, [("Cleaning Chemical Kit SC-2 Formulation", 50, "0")])
    b.close_deal_fast(
        eng, pain="新薬液のパイロット評価を行いたい", timing_days=-360,
        timing_driver="評価計画", metrics_kpi="評価コスト", metrics_value=800000,
        decision_criteria=["性能"], budget_amount=1200000,
        fiscal_period="2024年度", decider_title="品質保証課長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(385), when_qualified=days_ago(380),
        when_proposal=days_ago(375), when_negotiation=days_ago(372), when_won=days_ago(370),
    )
    b.wire_contract(
        eng, when=days_ago(370),
        start_date=date_days_ago(370), end_date=date_days_ago(5),
        final_status=ContractStatus.ACTIVE,  # 小口トライアルは更新せず終了、別のRFPで再商談中(現行パイプライン)
    )


# --- C1: NSC Electronic Materials USA — 長期契約(まだ余裕あり) -----------

def build_c1_nsc_usa(b: FastDealBuilder):
    _, eng = b.create_lead("BP-1000001", "半導体材料 長期供給契約", days_ago(580))
    b.add_products(eng, [
        ("High Purity Silane Gas SiH4 6N", 100, "0"),
        ("Hafnium Precursor for ALD HfCl4", 30, "0"),
    ])
    b.close_deal_fast(
        eng, pain="複数拠点向けに長期安定供給契約を結びたい", timing_days=-540,
        timing_driver="グローバル調達方針", metrics_kpi="調達リスク", metrics_value=10000000,
        decision_criteria=["供給継続性", "価格"], budget_amount=19000000,
        fiscal_period="2025年度", decider_title="Global Procurement VP", paper_layers=3,
        legal_review=True, competitors=["競合X社"], incumbent=None,
        when_prospect=days_ago(575), when_qualified=days_ago(570),
        when_proposal=days_ago(565), when_negotiation=days_ago(562), when_won=days_ago(560),
    )
    b.wire_contract(
        eng, when=days_ago(560),
        start_date=date_days_ago(560), end_date=date_days_from_now(535),
        final_status=ContractStatus.ACTIVE,
    )


# --- C3: Northern Silicon Technologies — 独立した過去のトライアル契約 -----

def build_c3_northern_silicon(b: FastDealBuilder):
    _, eng = b.create_lead("BP-3000004", "銅配線CMPスラリー トライアル契約", days_ago(520))
    b.add_products(eng, [("CMP Slurry for Copper NSC-CuS18", 100, "0")])
    b.close_deal_fast(
        eng, pain="銅CMPスラリーの小ロット評価を行いたい", timing_days=-490,
        timing_driver="評価計画", metrics_kpi="評価コスト", metrics_value=600000,
        decision_criteria=["性能"], budget_amount=3700000,
        fiscal_period="2024年度", decider_title="Quality Engineering Manager", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(515), when_qualified=days_ago(510),
        when_proposal=days_ago(505), when_negotiation=days_ago(502), when_won=days_ago(500),
    )
    b.wire_contract(
        eng, when=days_ago(500),
        start_date=date_days_ago(500), end_date=date_days_ago(135),
        final_status=ContractStatus.TERMINATED,  # トライアル終了、大型商談として再来(現行パイプライン)
    )


# --- C5: Helios Memory Systems — 独立した過去の小口契約 -------------------

def build_c5_helios(b: FastDealBuilder):
    _, eng = b.create_lead("BP-3000002", "フォトレジスト 評価契約(小口)", days_ago(650))
    b.add_products(eng, [("KrF Photoresist NSP-KR220", 15, "0")])
    b.close_deal_fast(
        eng, pain="既存ノード向けフォトレジストの供給元を追加したい", timing_days=-620,
        timing_driver="供給元多様化", metrics_kpi="供給リスク", metrics_value=1000000,
        decision_criteria=["品質"], budget_amount=2400000,
        fiscal_period="2024年度", decider_title="製造技術部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(645), when_qualified=days_ago(640),
        when_proposal=days_ago(635), when_negotiation=days_ago(632), when_won=days_ago(630),
    )
    b.wire_contract(
        eng, when=days_ago(630),
        start_date=date_days_ago(630), end_date=date_days_ago(265),
        final_status=ContractStatus.TERMINATED,  # 小口評価は完了、EUV量産採用で再来(現行パイプライン)
    )


# --- C2: NSC Singapore — 失注(提案後、価格で競合負け) ---------------------

def build_c2_nsc_singapore_lost(b: FastDealBuilder):
    _, eng = b.create_lead("BP-1000004", "CMPスラリー評価案件", days_ago(250))
    b.lose_from_proposal(
        eng, pain="CMPスラリーのコスト削減を検討している", timing_days=-220,
        timing_driver="コスト削減計画", metrics_kpi="スラリーコスト", metrics_value=1800000,
        decision_criteria=["価格"], budget_amount=9000000, fiscal_period="2025年度",
        lost_reason="価格で競合他社に決定",
        when_prospect=days_ago(245), when_qualified=days_ago(240),
        when_proposal=days_ago(235), when_lost=days_ago(230),
    )


# --- C4: Yangtze Photonics Manufacturing — 失注(予算凍結、後日再訪) ------

def build_c4_yangtze_lost(b: FastDealBuilder):
    _, eng = b.create_lead("BP-3000005", "銅CMPスラリー導入検討(過去)", days_ago(340))
    b.lose_from_prospect(
        eng, pain="銅CMPスラリーの切替を検討していたが予算が凍結された",
        timing_days=-310, timing_driver="設備投資計画",
        lost_reason="予算凍結のため見送り",
        when_prospect=days_ago(335), when_lost=days_ago(330),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存データは削除しない — 積み増しのみ)",
    )
    return parser.parse_args()


def main() -> None:
    global TENANT_ID
    args = parse_args()
    TENANT_ID = args.tenant_id

    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(TENANT_ID)},
        )

        b = FastDealBuilder(session)
        build_a1_sunrise(b)
        build_a2_nsc_taiwan(b)
        build_a3_nsc_korea(b)
        build_a4_eurasia(b)
        build_b1_apex(b)
        build_b2_pacific(b)
        build_c1_nsc_usa(b)
        build_c3_northern_silicon(b)
        build_c5_helios(b)
        build_c2_nsc_singapore_lost(b)
        build_c4_yangtze_lost(b)
        session.commit()

    print(f"Backfilled 2 years of historical deals for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
