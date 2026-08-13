"""ROH/HALB/HAWA(原材料・中間体・消耗品)を使った商流の多角化シミュレーション。

  python scripts/seed_demo_material_deals.py --tenant-id <uuid>

これまでのシミュレーション(半導体ファブ向けの完成品材料販売)に加えて、
別の商流を積み増す(2026-08-13 ユーザー要望: 装置部材・原材料の製造
メーカー向け販売など、取引先・セールスグループを多岐に広げて同期間分の
実績を集積する):

  - 原材料(ROH)を製造メーカーへ販売する商流(化学薬品コンパウンド会社、
    特殊ガス会社)
  - 中間体(HALB)の一部(化学系ベース材料)をフォトレジスト配合会社へ
    販売する商流
  - 装置サブアセンブリ部材(HALB)をOEM装置メーカーへ販売する商流
  - 消耗品・交換部品(HAWA)をMRO(保守)会社へ販売する商流
  - 半導体部材・基板(ROH)をOSAT(組立・実装受託)会社へ販売する商流

取引先はERPの取引先マスタに存在しない(架空の)製造業アカウントを新設
する — 実在企業とは無関係の合成名のみを使う。セールスグループは新たに
「素材事業本部」「装置事業本部」を新設し、既存の「海外営業本部」とは
別軸で管理する。

先に scripts/apply_material_pricing.py を実行しておくこと(商品価格表が
必要)。既存データは削除しない(積み増しのみ)。

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
    AccessLevel, BuyingCenterRole, ContractStatus, Criterion,
    EngagementRelationshipType, ProposalStatus, QuoteStatus, SourceKind, Stage,
    Stance,
)
from crm_mvp.models import (
    Account, Engagement, ExtractionProposal, GraphNode,
    IngestionSource, Product, QualificationSlot,
)
from crm_mvp.schemas.extraction import ExtractedClaim
from crm_mvp.services.account_hierarchy import create_grouping_account
from crm_mvp.services.apply_proposal import apply_proposal
from crm_mvp.services.contacts import link_contact_to_engagement
from crm_mvp.services.engagement_relationships import create_child_engagement
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.quoting import (
    create_contract, create_quote_from_engagement, update_contract_status,
    update_quote_status,
)
from crm_mvp.services.sales_groups import create_sales_group
from crm_mvp.services.stage_transitions import apply_stage_transition

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)

# 実在企業とは無関係の合成アカウント名(2026-08-13 商流多角化用)。
NEW_ACCOUNTS = {
    "meridian": ("Meridian Chemical Compounding Co., Ltd.", "JP"),
    "crescent": ("Crescent Specialty Gases Ltd.", "SG"),
    "vantage": ("Vantage Process Systems Corporation", "US"),
    "precision": ("Precision MRO Solutions K.K.", "JP"),
    "orion": ("Orion Semiconductor Assembly Pte. Ltd.", "MY"),
    "nordic": ("Nordic Photoresist Formulation AB", "SE"),
    "zenith": ("Zenith Wafer Solutions Ltd.", "TW"),
}


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def date_days_ago(n: float) -> date:
    return (NOW - timedelta(days=n)).date()


def date_days_from_now(n: float) -> date:
    return (NOW + timedelta(days=n)).date()


class FastDealBuilder:
    """scripts/seed_demo_2yr_backfill.py の FastDealBuilder と同じ設計。
    違いは create_lead が ErpBusinessPartner ではなく、架空の新規アカウント
    (NEW_ACCOUNTS)を直接作る点のみ。"""

    def __init__(self, session: Session):
        self.session = session
        self._accounts: dict[str, Account] = {}

    def get_or_create_account(self, key: str) -> Account:
        if key in self._accounts:
            return self._accounts[key]
        name, country = NEW_ACCOUNTS[key]
        account = self.session.execute(
            select(Account).where(Account.tenant_id == TENANT_ID, Account.name == name)
        ).scalar_one_or_none()
        if account is None:
            account = Account(tenant_id=TENANT_ID, name=name, country=country)
            self.session.add(account)
            self.session.flush()
        self._accounts[key] = account
        return account

    def create_lead(
        self, account_key: str, engagement_name: str, created: datetime,
        sales_group_id: uuid.UUID | None = None,
    ):
        account = self.get_or_create_account(account_key)
        engagement = Engagement(
            tenant_id=TENANT_ID, account_id=account.id, name=engagement_name,
            stage=Stage.LEAD, created_at=created, sales_group_id=sales_group_id,
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
        child.sales_group_id = parent.sales_group_id
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
        source = IngestionSource(
            tenant_id=TENANT_ID, engagement_id=engagement.id, kind=SourceKind.TRANSCRIPT,
            raw_text=raw_text, occurred_at=when, created_at=when,
            processed_at=when, extractor_version="demo-scripted-v1-materials",
        )
        self.session.add(source)
        self.session.flush()
        return source

    def _propose(self, engagement, source, *, field_path, value, when, model_score=0.8):
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
        # decays_at は設定しない(過去実績の再現。理由は
        # scripts/seed_demo_2yr_backfill.py の同名メソッド参照)。
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
        self, engagement, *, pain, timing_days, timing_driver, metrics_kpi,
        metrics_value, decision_criteria, budget_amount, fiscal_period,
        decider_title, paper_layers, legal_review, competitors, incumbent,
        when_prospect, when_qualified, when_proposal, when_negotiation, when_won,
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

        decider_node = GraphNode(
            tenant_id=TENANT_ID, account_id=engagement.account_id,
            placeholder_label=f"{decider_title}(氏名未確認)", seniority_layer=3,
            created_at=when_negotiation,
        )
        self.session.add(decider_node)
        self.session.flush()
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

    def lose_from_proposal(
        self, engagement, *, pain, timing_days, timing_driver, metrics_kpi,
        metrics_value, decision_criteria, budget_amount, fiscal_period,
        lost_reason, when_prospect, when_qualified, when_proposal, when_lost,
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


def seed_sales_groups(session: Session) -> dict[str, uuid.UUID]:
    materials_hq = create_sales_group(session, TENANT_ID, name="素材事業本部")
    raw_materials = create_sales_group(
        session, TENANT_ID, name="原材料営業課", parent_group_id=materials_hq.id,
    )
    intermediates = create_sales_group(
        session, TENANT_ID, name="中間体営業課", parent_group_id=materials_hq.id,
    )
    equipment_hq = create_sales_group(session, TENANT_ID, name="装置事業本部")
    equipment_parts = create_sales_group(
        session, TENANT_ID, name="装置部材営業課", parent_group_id=equipment_hq.id,
    )
    mro_parts = create_sales_group(
        session, TENANT_ID, name="保守部品営業課", parent_group_id=equipment_hq.id,
    )
    session.flush()
    return {
        "raw_materials": raw_materials.id, "intermediates": intermediates.id,
        "equipment_parts": equipment_parts.id, "mro_parts": mro_parts.id,
    }


def build_meridian_initial(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Meridian Chemical Compounding: 原材料(溶剤・酸)の初回契約。既に期限切れ、
    後続の独立した再商談(build_meridian_repeat)へ自然につながる。"""
    _, eng = b.create_lead(
        "meridian", "高純度溶剤・酸供給契約", days_ago(490), sales_group_id=sg["raw_materials"],
    )
    b.add_products(eng, [
        ("PGMEA Solvent (Electronic Grade)", 3000, "0"),
        ("Hydrogen Fluoride HF 49% Electronic Grade", 800, "0"),
        ("Sulfuric Acid H2SO4 98% Ultra-Pure", 5000, "5"),
    ])
    b.close_deal_fast(
        eng, pain="洗浄工程用薬液の調達コストを削減したい", timing_days=-450,
        timing_driver="年間調達計画", metrics_kpi="薬液調達コスト", metrics_value=4000000,
        decision_criteria=["価格", "純度規格"], budget_amount=9500000,
        fiscal_period="2024年度", decider_title="調達部長", paper_layers=2,
        legal_review=False, competitors=["国内サプライヤー"], incumbent=None,
        when_prospect=days_ago(485), when_qualified=days_ago(480),
        when_proposal=days_ago(475), when_negotiation=days_ago(472), when_won=days_ago(470),
    )
    b.wire_contract(
        eng, when=days_ago(470),
        start_date=date_days_ago(470), end_date=date_days_ago(105),
        final_status=ContractStatus.TERMINATED,
    )


def build_meridian_repeat(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Meridian: 独立した新規RFP(現行パイプライン、Proposal段階で止める)。"""
    _, eng = b.create_lead(
        "meridian", "洗浄薬液 追加調達提案", days_ago(70), sales_group_id=sg["raw_materials"],
    )
    b.add_products(eng, [
        ("pH Modifier (Aqueous KOH 5%)", 3000, "0"),
        ("IPA Electronic Grade 99.9% (Isopropyl Alcohol)", 5000, "0"),
    ])
    src = b._ingest(eng, "追加ラインの洗浄薬液調達先を再検討している", days_ago(65))
    b._propose(eng, src, field_path="criterion:identified_pain",
               value={"summary": "追加ラインの洗浄薬液調達先を再検討している"}, when=days_ago(65))
    b._propose(eng, src, field_path="criterion:timing",
               value={"target_date": str(date.today() + timedelta(days=60)), "driver": "増設ライン稼働計画"},
               when=days_ago(65))
    b.advance(eng, Stage.PROSPECT, when=days_ago(65))
    b._propose(eng, src, field_path="criterion:metrics",
               value={"kpi": "薬液調達コスト", "annual_value": 3200000}, when=days_ago(60))
    b.advance(eng, Stage.QUALIFIED, when=days_ago(60))
    b._propose(eng, src, field_path="criterion:decision_criteria",
               value={"criteria": ["価格", "純度規格"]}, when=days_ago(55))
    b._propose(eng, src, field_path="criterion:budget",
               value={"amount": 7000000, "fiscal_period": "2026年度", "secured": True},
               when=days_ago(55))
    b.advance(eng, Stage.PROPOSAL, when=days_ago(55))


def build_crescent(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Crescent Specialty Gases: 前駆体・ドーパントガスの初回契約→更新
    (更新後の契約がまもなく2回目の更新対象になる)。"""
    _, gen1 = b.create_lead(
        "crescent", "前駆体・ドーパントガス供給契約", days_ago(690), sales_group_id=sg["raw_materials"],
    )
    b.add_products(gen1, [
        ("Ammonia NH3 Semiconductor Grade 6N", 200, "0"),
        ("Titanium Tetrachloride TiCl4 Semiconductor Grade", 50, "0"),
        ("Diborane B2H6 Dopant Gas 1% in H2", 10, "0"),
    ])
    b.close_deal_fast(
        gen1, pain="ドーパントガスの供給元を国際分散させたい", timing_days=-600,
        timing_driver="サプライチェーン多様化計画", metrics_kpi="供給リスク", metrics_value=5000000,
        decision_criteria=["純度", "供給安定性"], budget_amount=12000000,
        fiscal_period="2024年度", decider_title="Supply Chain Director", paper_layers=2,
        legal_review=True, competitors=["欧州系サプライヤー"], incumbent=None,
        when_prospect=days_ago(685), when_qualified=days_ago(680),
        when_proposal=days_ago(675), when_negotiation=days_ago(672), when_won=days_ago(670),
    )
    b.wire_contract(
        gen1, when=days_ago(670),
        start_date=date_days_ago(670), end_date=date_days_ago(305),
        final_status=ContractStatus.TERMINATED,
    )

    gen2 = b.spawn_child(
        gen1, relationship_type=EngagementRelationshipType.RENEWAL,
        name="前駆体・ドーパントガス供給契約(更新)", created=days_ago(325),
    )
    b.add_products(gen2, [
        ("Ammonia NH3 Semiconductor Grade 6N", 250, "5"),
        ("Titanium Tetrachloride TiCl4 Semiconductor Grade", 60, "5"),
        ("Diborane B2H6 Dopant Gas 1% in H2", 15, "0"),
    ])
    b.close_deal_fast(
        gen2, pain="増産に伴いドーパントガスの調達量を見直したい", timing_days=-300,
        timing_driver="契約更新", metrics_kpi="供給リスク", metrics_value=6000000,
        decision_criteria=["純度", "供給安定性"], budget_amount=16000000,
        fiscal_period="2025年度", decider_title="Supply Chain Director", paper_layers=2,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(320), when_qualified=days_ago(315),
        when_proposal=days_ago(310), when_negotiation=days_ago(307), when_won=days_ago(305),
    )
    b.wire_contract(
        gen2, when=days_ago(305),
        start_date=date_days_ago(305), end_date=date_days_from_now(60),
        final_status=ContractStatus.ACTIVE,
    )


def build_vantage_won(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Vantage Process Systems: 装置サブアセンブリ部材のOEM供給契約(長期)。"""
    _, eng = b.create_lead(
        "vantage", "装置サブアセンブリ部材 OEM供給契約", days_ago(440), sales_group_id=sg["equipment_parts"],
    )
    b.add_products(eng, [
        ("Chemical Pump Assembly (DE origin, Linde subsystem)", 2, "0"),
        ("Chemical Distribution Manifold PFA Welded", 3, "0"),
        ("Automated Chemical Dosing Module (PLC-controlled)", 1, "0"),
    ])
    b.close_deal_fast(
        eng, pain="自社装置向けの主要部材を内製から外部調達に切り替えたい", timing_days=-400,
        timing_driver="製造BOM見直し", metrics_kpi="部材調達コスト", metrics_value=15000000,
        decision_criteria=["品質認証", "供給リードタイム"], budget_amount=10500000,
        fiscal_period="2025年度", decider_title="VP Engineering", paper_layers=2,
        legal_review=True, competitors=["自社内製"], incumbent="自社内製",
        when_prospect=days_ago(435), when_qualified=days_ago(430),
        when_proposal=days_ago(425), when_negotiation=days_ago(422), when_won=days_ago(420),
    )
    b.wire_contract(
        eng, when=days_ago(420),
        start_date=date_days_ago(420), end_date=date_days_from_now(310),
        final_status=ContractStatus.ACTIVE,
    )


def build_vantage_lost(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Vantage: 別件(消耗性の高い部材ライン)は価格で競合負け。"""
    _, eng = b.create_lead(
        "vantage", "配管・継手部材 追加供給提案", days_ago(160), sales_group_id=sg["equipment_parts"],
    )
    b.lose_from_proposal(
        eng, pain="配管継手部材のコスト削減を検討している", timing_days=-130,
        timing_driver="コスト削減計画", metrics_kpi="部材コスト", metrics_value=1200000,
        decision_criteria=["価格"], budget_amount=3000000, fiscal_period="2026年度",
        lost_reason="価格で競合他社に決定",
        when_prospect=days_ago(155), when_qualified=days_ago(150),
        when_proposal=days_ago(145), when_lost=days_ago(140),
    )


def build_precision_mro(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Precision MRO Solutions: 消耗品・交換部品の初回契約→Upsell。"""
    _, base = b.create_lead(
        "precision", "消耗品・交換部品 供給契約", days_ago(280), sales_group_id=sg["mro_parts"],
    )
    b.add_products(base, [
        ("Fluoropolymer O-Ring Set (FKM/FFKM, cleanroom)", 20, "0"),
        ("High-Purity Filter Cartridge 0.05μm PTFE", 30, "0"),
        ("PTFE / PFA Fittings Set (1/4, 3/8, 1/2 inch)", 15, "0"),
    ])
    b.close_deal_fast(
        base, pain="複数拠点の保守部材調達を一本化したい", timing_days=-250,
        timing_driver="保守体制の集約計画", metrics_kpi="保守部材調達コスト", metrics_value=2500000,
        decision_criteria=["対応スピード", "在庫保有力"], budget_amount=6000000,
        fiscal_period="2025年度", decider_title="保守統括部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(275), when_qualified=days_ago(270),
        when_proposal=days_ago(265), when_negotiation=days_ago(262), when_won=days_ago(260),
    )
    b.wire_contract(
        base, when=days_ago(260),
        start_date=date_days_ago(260), end_date=date_days_from_now(105),
        final_status=ContractStatus.ACTIVE,
    )

    upsell = b.spawn_child(
        base, relationship_type=EngagementRelationshipType.UPSELL,
        name="消耗品・交換部品 供給拡張(MFC追加)", created=days_ago(70),
    )
    b.add_products(upsell, [
        ("Mass Flow Controller MFC for Chemical Gas", 5, "0"),
        ("Chemical Drum Liner HDPE 200L Cleanroom Grade", 100, "0"),
    ])
    b.close_deal_fast(
        upsell, pain="ガス系保守部材も同一サプライヤーに集約したい", timing_days=-40,
        timing_driver="保守体制の集約計画(第2弾)", metrics_kpi="保守部材調達コスト", metrics_value=1800000,
        decision_criteria=["対応スピード"], budget_amount=2200000,
        fiscal_period="2026年度", decider_title="保守統括部長", paper_layers=1,
        legal_review=False, competitors=[], incumbent=None,
        when_prospect=days_ago(65), when_qualified=days_ago(60),
        when_proposal=days_ago(55), when_negotiation=days_ago(52), when_won=days_ago(50),
    )
    # 既存契約への追加発注のため、単独の見積・契約は発行しない(PO拡張ベース)。


def build_orion(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Orion Semiconductor Assembly(OSAT): 半導体部材・基板の供給契約。"""
    _, eng = b.create_lead(
        "orion", "パッケージ基板・ボンディングワイヤ供給契約", days_ago(350),
        sales_group_id=sg["raw_materials"],
    )
    b.add_products(eng, [
        ("BGA Package Substrate 7x7mm 0.5mm pitch (FC-BGA, semiconductor)", 50000, "0"),
        ("Gold Bonding Wire 25um 99.99% (thermo-compression, IC assembly)", 20000, "0"),
        ("Epoxy Mold Compound Silica-filled (semiconductor encapsulant)", 500000, "5"),
        ("LPDRAM Die 4MB 16nm SDRAM (KR origin, JEDEC LPDDR4)", 10000, "0"),
    ])
    b.close_deal_fast(
        eng, pain="組立実装向け部材の複数購買化を進めたい", timing_days=-310,
        timing_driver="購買先分散方針", metrics_kpi="部材調達コスト", metrics_value=18000000,
        decision_criteria=["価格", "納期", "品質実績"], budget_amount=95000000,
        fiscal_period="2025年度", decider_title="Procurement Director", paper_layers=3,
        legal_review=True, competitors=["現行サプライヤー"], incumbent="現行サプライヤー",
        when_prospect=days_ago(345), when_qualified=days_ago(340),
        when_proposal=days_ago(335), when_negotiation=days_ago(332), when_won=days_ago(330),
    )
    b.wire_contract(
        eng, when=days_ago(330),
        start_date=date_days_ago(330), end_date=date_days_from_now(35),
        final_status=ContractStatus.ACTIVE,
    )


def build_nordic(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Nordic Photoresist Formulation: フォトレジスト用ベース材料(中間体)供給契約。"""
    _, eng = b.create_lead(
        "nordic", "フォトレジストベース材料供給契約", days_ago(300), sales_group_id=sg["intermediates"],
    )
    b.add_products(eng, [
        ("ArF Photoresist Base Polymer (US origin, Dow)", 50, "0"),
        ("Developer TMAH 2.38% Electronic Grade", 2000, "0"),
    ])
    b.close_deal_fast(
        eng, pain="自社フォトレジスト配合用のベースポリマー調達元を確保したい", timing_days=-260,
        timing_driver="製品ライン拡張計画", metrics_kpi="原料調達コスト", metrics_value=7000000,
        decision_criteria=["純度", "価格"], budget_amount=40000000,
        fiscal_period="2025年度", decider_title="R&D Director", paper_layers=2,
        legal_review=False, competitors=["米国系サプライヤー"], incumbent=None,
        when_prospect=days_ago(295), when_qualified=days_ago(290),
        when_proposal=days_ago(285), when_negotiation=days_ago(282), when_won=days_ago(280),
    )
    b.wire_contract(
        eng, when=days_ago(280),
        start_date=date_days_ago(280), end_date=date_days_from_now(85),
        final_status=ContractStatus.ACTIVE,
    )


def build_zenith_lead(b: FastDealBuilder, sg: dict[str, uuid.UUID]):
    """Zenith Wafer Solutions: シリコンウェーハに関する新規引き合い(現行パイプライン)。"""
    _, eng = b.create_lead(
        "zenith", "高純度シリコンウェーハ引き合い", days_ago(15), sales_group_id=sg["raw_materials"],
    )
    src = b._ingest(
        eng, "高純度シリコンウェーハの新規調達先を探しており、まずは価格感を知りたい", days_ago(15),
    )
    b._propose(
        eng, src, field_path="criterion:identified_pain",
        value={"summary": "既存ウェーハサプライヤーの供給枠が逼迫している"}, when=days_ago(15),
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

        sg = seed_sales_groups(session)
        session.commit()

        b = FastDealBuilder(session)
        build_meridian_initial(b, sg)
        build_meridian_repeat(b, sg)
        build_crescent(b, sg)
        build_vantage_won(b, sg)
        build_vantage_lost(b, sg)
        build_precision_mro(b, sg)
        build_orion(b, sg)
        build_nordic(b, sg)
        build_zenith_lead(b, sg)
        session.commit()

    print(f"Seeded diversified material/equipment deals for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
