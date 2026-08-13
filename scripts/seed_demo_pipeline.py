"""デモ用パイプラインデータの投入コマンド(半導体製造材料の商談シナリオ)。

  python scripts/seed_demo_pipeline.py --tenant-id <uuid>

指定テナントの既存パイプラインデータ(Lead〜契約)を全削除したうえで、
6段階の商談を実際のアプリのロジックを通して作り直す。直接 INSERT では
なく、IngestionSource → ExtractionProposal(承認/却下/自動適用) →
apply_proposal、apply_stage_transition という本物の経路を通す。これにより:
  - 活動ログ(StageTransition/ExtractionProposal)が実運用と同じ形で残る
  - クオリフィケーション値に evidence_quote(根拠)が紐づく
  - 案件化(Qualified)以降は商品構成(EngagementLineItem)を実際の
    Product Priceリストから組み、案件金額はそこから自動計算される

取引先(Account)は scripts/import_erp_business_partners_csv.py で取り込み
済みの ErpBusinessPartner(CUSTOMER)から実名を使い、Account.external_system
/external_id で紐付ける。商品は scripts/apply_fert_pricing.py で価格設定
済みの Product(半導体製造材料)を使う。どちらも先に実行しておくこと
(2026-08-13 ユーザー指示: 商品表を使って商談ダミーデータを作り直す)。

Product / ProductGroup / ErpMaterial / ErpBusinessPartner はここでは
削除しない — 別スクリプトで管理するマスタデータのため。

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
    AccessLevel, BuyingCenterRole, Confidence, ContractStatus, Criterion,
    EdgeType, ProposalStatus, QuoteStatus, SourceKind, Stage, Stance,
    VerificationMethod,
)
from crm_mvp.models import (
    Account, Engagement, ErpBusinessPartner, ExtractionProposal, GraphEdge,
    GraphNode, IngestionSource, Product, QualificationSlot, Waiver,
)
from crm_mvp.schemas.extraction import ExtractedClaim
from crm_mvp.services.apply_proposal import apply_proposal
from crm_mvp.services.contacts import (
    link_contact_to_engagement, register_contact_and_link,
)
from crm_mvp.services.decay_policy import compute_decays_at
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.quoting import (
    create_contract, create_quote_from_engagement, update_contract_status,
    update_quote_status,
)
from crm_mvp.services.seed_policies import (
    upsert_default_autonomy, upsert_gate_policies,
)
from crm_mvp.services.stage_transitions import apply_stage_transition

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


class DealBuilder:
    def __init__(self, session: Session):
        self.session = session

    def create_lead(
        self, bp_code: str, engagement_name: str, created: datetime,
    ):
        """取引先は ErpBusinessPartner から実名を引いて Account を作る。
        external_system/external_id で ERP側の bp_code に紐付ける。同じ
        bp_code で複数回呼んでも既存の Account を再利用する(同一取引先が
        複数商談を持つのは普通のことで、Account を毎回複製すると
        uq_account_external に違反する)。"""
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

    def add_products(self, engagement, items: list[tuple[str, int, str]]):
        """商品名(Product.name = ERP品目のdescriptionをそのまま踏襲)・数量・
        値引率のタプルから商品構成を組む。案件金額はここから自動計算される。"""
        for name, quantity, discount_rate in items:
            product = self.session.execute(
                select(Product).where(
                    Product.tenant_id == TENANT_ID, Product.name == name,
                )
            ).scalar_one()
            add_line_item(
                self.session, TENANT_ID, engagement, product=product,
                quantity=quantity, discount_rate=Decimal(discount_rate),
            )

    def add_contact(
        self, engagement, *, name, title=None, org_unit=None, email=None,
        role: BuyingCenterRole | None = None,
        access_level: AccessLevel = AccessLevel.CONTACTED, when: datetime,
    ):
        contact, engrole = register_contact_and_link(
            self.session, TENANT_ID, engagement, full_name=name, title=title,
            org_unit=org_unit, email=email,
            roles=[role.value] if role else [], stance=Stance.UNKNOWN,
            access_level=access_level, written_by=f"human:{ACTOR_ID}",
        )
        engrole.last_touched_at = when
        engrole.created_at = when
        self.session.flush()
        return contact, engrole

    def add_placeholder_node(self, engagement, *, label, org_unit=None,
                              seniority_layer=0, when: datetime):
        node = GraphNode(
            tenant_id=TENANT_ID, account_id=engagement.account_id,
            placeholder_label=label, org_unit=org_unit,
            seniority_layer=seniority_layer, created_at=when,
        )
        self.session.add(node)
        self.session.flush()
        return node

    def link_node(self, engagement, node, *, role: BuyingCenterRole | None,
                  access_level: AccessLevel, when: datetime):
        """既存の GraphNode(プレースホルダー含む)をこの案件の関係者として
        紐付ける。ゲートの path_to_decider は EngagementRole 経由でしか
        'decider' ロールを見つけられないため、プレースホルダーの決裁者
        ノードにも必ず必要。"""
        role_obj = link_contact_to_engagement(
            self.session, TENANT_ID, engagement, node,
            roles=[role.value] if role else [], stance=Stance.UNKNOWN,
            access_level=access_level, written_by=f"human:{ACTOR_ID}",
        )
        role_obj.last_touched_at = when
        role_obj.created_at = when
        self.session.flush()
        return role_obj

    def add_edge(self, engagement, from_node, to_node, *, confidence, sequence=None,
                 when: datetime):
        edge = GraphEdge(
            tenant_id=TENANT_ID, account_id=engagement.account_id,
            from_node_id=from_node.id, to_node_id=to_node.id,
            edge_type=EdgeType.APPROVES, sequence=sequence, confidence=confidence,
            created_at=when,
        )
        self.session.add(edge)
        self.session.flush()
        return edge

    def ingest(self, engagement, *, kind: SourceKind, raw_text: str, when: datetime):
        source = IngestionSource(
            tenant_id=TENANT_ID, engagement_id=engagement.id, kind=kind,
            raw_text=raw_text, occurred_at=when, created_at=when,
            processed_at=when, extractor_version="demo-scripted-v1",
        )
        self.session.add(source)
        self.session.flush()
        return source

    def propose(
        self, engagement, source, *, target_type, field_path, value, model_score,
        rationale, evidence_quote, when: datetime, decision="accept",
        corrected_value=None,
    ):
        claim = ExtractedClaim(
            target_type=target_type, field_path=field_path, value=value,
            model_score=model_score, rationale=rationale,
            evidence_quote=evidence_quote,
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

        if decision in ("accept", "auto"):
            apply_proposal(self.session, proposal)
            proposal.status = (
                ProposalStatus.ACCEPTED if decision == "accept"
                else ProposalStatus.AUTO_APPLIED
            )
            if decision == "accept":
                proposal.decided_by = ACTOR_ID
            proposal.decided_at = when
            if target_type == "qualification_slot":
                criterion = Criterion(field_path.split(":", 1)[1])
                slot = self.session.execute(
                    select(QualificationSlot).where(
                        QualificationSlot.tenant_id == TENANT_ID,
                        QualificationSlot.engagement_id == engagement.id,
                        QualificationSlot.criterion == criterion,
                    )
                ).scalar_one()
                slot.asserted_at = when
                slot.decays_at = compute_decays_at(criterion, when)
        elif decision == "reject":
            proposal.status = ProposalStatus.REJECTED
            proposal.decided_by = ACTOR_ID
            proposal.decided_at = when
            proposal.corrected_value = corrected_value

        self.session.flush()
        return proposal

    def advance(self, engagement, to_stage: Stage, when: datetime, waiver_id=None):
        outcome = apply_stage_transition(
            self.session, TENANT_ID, engagement, to_stage,
            waiver_id=waiver_id, actor=f"human:{ACTOR_ID}",
        )
        if not outcome.allowed:
            missing = [m.reason for m in outcome.result.missing]
            raise RuntimeError(
                f"{engagement.name}: blocked {engagement.stage} -> {to_stage}: {missing}"
            )
        outcome.transition.occurred_at = when
        self.session.flush()
        return outcome

    def issue_waiver(self, engagement, policy_id, reason: str, when: datetime):
        waiver = Waiver(
            tenant_id=TENANT_ID, engagement_id=engagement.id, policy_id=policy_id,
            approved_by=ACTOR_ID, reason=reason, approved_at=when,
            written_by=f"human:{ACTOR_ID}",
        )
        self.session.add(waiver)
        self.session.flush()
        return waiver

    def verify_slot(self, engagement, criterion: Criterion, *, method, when,
                     evidence_uri=None, note=None):
        slot = self.session.execute(
            select(QualificationSlot).where(
                QualificationSlot.tenant_id == TENANT_ID,
                QualificationSlot.engagement_id == engagement.id,
                QualificationSlot.criterion == criterion,
            )
        ).scalar_one()
        slot.confidence = Confidence.VERIFIED
        slot.verification_method = method
        slot.evidence_uri = evidence_uri
        slot.verification_note = note
        slot.verified_by = ACTOR_ID
        slot.verified_at = when
        slot.decays_at = compute_decays_at(criterion, when)
        self.session.flush()
        return slot

    def find_policy_id(self, code: str):
        from crm_mvp.models import GatePolicy
        return self.session.execute(
            select(GatePolicy.id).where(
                GatePolicy.tenant_id == TENANT_ID, GatePolicy.code == code,
                GatePolicy.industry_template == "manufacturing",
            )
        ).scalar_one()

    def wire_quote_and_contract(
        self, engagement, *, when: datetime,
        start_date: date | None = None, end_date: date | None = None,
    ):
        """商品構成が確定した受注案件に、見積もり(SENT→ACCEPTED)と
        契約(SIGNED→ACTIVE)まで実サービス経由で発行する。"""
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
        self.session.flush()
        return quote, contract


def wipe_tenant_data(session: Session) -> None:
    """商談パイプラインのデータのみを削除する。Product/ProductGroup/
    ErpMaterial/ErpBusinessPartner は別スクリプトが管理するマスタデータ
    のためここでは削除しない(engagement_line_item/quote/contract は
    engagement 削除時に ON DELETE CASCADE で連鎖削除される)。"""
    tables = [
        "waiver", "gate_evaluation", "extraction_proposal", "ingestion_source",
        "engagement_role", "graph_edge", "graph_node", "qualification_slot",
        "stage_transition", "pipeline_snapshot", "engagement",
        "contact", "compliance_status", "account",
        "field_autonomy_policy", "gate_policy",
    ]
    for t in tables:
        session.execute(
            text(f"DELETE FROM {t} WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
        )


def build_deal_1_lead(b: DealBuilder):
    """Lead: CMPスラリーの品質不具合を機に代替サプライヤーを探し始めた引き合い。"""
    _, eng = b.create_lead(
        "BP-3000005", "銅CMPスラリー代替サプライヤー引き合い", days_ago(4),
    )
    b.add_contact(
        eng, name="林 建華", title="購買主任", org_unit="購買部",
        role=None, access_level=AccessLevel.CONTACTED, when=days_ago(4),
    )
    source = b.ingest(
        eng, kind=SourceKind.EMAIL,
        raw_text=(
            "お世話になっております。現行の銅配線CMP工程でディッシング不良が"
            "散発しており、スラリーの切り替えを検討したくご連絡しました。"
        ),
        when=days_ago(4),
    )
    b.propose(
        eng, source, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "銅配線CMP工程でディッシング不良が散発している"}, model_score=0.71,
        rationale="問い合わせ文面から課題感を抽出",
        evidence_quote="銅配線CMP工程でディッシング不良が散発しており",
        when=days_ago(4), decision="pending",
    )


def build_deal_2_prospect(b: DealBuilder):
    """Prospect(案件化手前): 洗浄薬液の代替評価を開始、タイミングを確認中。"""
    _, eng = b.create_lead(
        "BP-2000003", "洗浄薬液サプライヤー切替評価", days_ago(20),
    )
    b.add_contact(
        eng, name="Daniel Cohen", title="技術部長", org_unit="プロセス開発部",
        role=BuyingCenterRole.USER, access_level=AccessLevel.ENGAGED,
        when=days_ago(18),
    )
    source = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "Cohen様: 現行の洗浄薬液サプライヤーの供給が不安定で、来四半期までには"
            "セカンドソースを確立したいと考えています。評価にはウェハテストが必要です。"
        ),
        when=days_ago(15),
    )
    b.propose(
        eng, source, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "洗浄薬液の供給が不安定でセカンドソースが必要"}, model_score=0.8,
        rationale="発言から課題を抽出", evidence_quote="サプライヤーの供給が不安定で",
        when=days_ago(15), decision="accept",
    )
    b.propose(
        eng, source, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=90)),
               "driver": "セカンドソース確立の社内目標"}, model_score=0.76,
        rationale="発言中の希望時期を抽出", evidence_quote="来四半期までにはセカンドソースを確立したい",
        when=days_ago(15), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(15))


def build_deal_3_qualified(b: DealBuilder):
    """案件化(Qualified): 洗浄薬液の年間供給契約。2名と関係構築済み。"""
    _, eng = b.create_lead(
        "BP-2000001", "洗浄薬液 年間供給契約", days_ago(50),
    )
    b.add_products(eng, [
        ("SC-1 Cleaning Solution (Standard)", 500, "5"),
        ("Photoresist Stripping Solution NMP-Free NSC-STR02", 200, "0"),
        ("Cleaning Chemical Kit SC-2 Formulation", 300, "0"),
    ])
    eng.expected_close_date = date.today() + timedelta(days=120)

    b.add_contact(
        eng, name="陳 志明", title="購買部長", org_unit="購買部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(48),
    )
    b.add_contact(
        eng, name="林 佳穎", title="品質保証課長", org_unit="品質保証部",
        role=BuyingCenterRole.USER, access_level=AccessLevel.ENGAGED,
        when=days_ago(40),
    )

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "陳様: 年間の薬液使用量が増えており、供給の安定性とコストの両面で"
            "見直したい。現行品よりパーティクル残留が目標値より高い状態が続いている。"
            "年内には契約先を固めたい。"
        ),
        when=days_ago(45),
    )
    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "洗浄後のパーティクル残留が目標値より高い"}, model_score=0.84,
        rationale="発言から課題を抽出", evidence_quote="パーティクル残留が目標値より高い状態",
        when=days_ago(45), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=140)),
               "driver": "年間供給契約の切替タイミング"}, model_score=0.75,
        rationale="発言中の希望時期を抽出", evidence_quote="年内には契約先を固めたい",
        when=days_ago(45), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(45))
    b.advance(eng, Stage.QUALIFIED, when=days_ago(44))

    src2 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "陳様: パーティクル低減で歩留まり改善、年間換算800万円ほどの効果を"
            "見込んでいます。"
        ),
        when=days_ago(30),
    )
    b.propose(
        eng, src2, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "洗浄後パーティクル数", "baseline": 45.0, "target": 20.0,
               "unit": "個/枚", "annual_value": 8000000}, model_score=0.78,
        rationale="効果額の言及から抽出", evidence_quote="年間換算800万円ほどの効果を見込んでいます",
        when=days_ago(30), decision="accept",
    )


def build_deal_4_proposal(b: DealBuilder):
    """提案(Proposal): 銅配線CMPスラリーの切替提案。評価基準・予算まで確認済み。"""
    _, eng = b.create_lead(
        "BP-3000004", "銅配線CMPスラリー切替提案", days_ago(70),
    )
    b.add_products(eng, [
        ("CMP Slurry for Copper NSC-CuS18", 400, "0"),
        ("Copper Interconnect CMP Slurry NSC-CuP22", 250, "0"),
        ("W-CMP Slurry Advanced H2O2-free NSC-WSL55", 150, "10"),
    ])
    eng.expected_close_date = date.today() + timedelta(days=90)

    b.add_contact(
        eng, name="Robert Chen", title="VP Manufacturing", org_unit="製造本部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(65),
    )
    b.add_contact(
        eng, name="Sarah Kim", title="Quality Engineering Manager", org_unit="品質保証部",
        role=BuyingCenterRole.TECHNICAL_GATE, access_level=AccessLevel.ENGAGED,
        when=days_ago(50),
    )

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "Chen様: 銅配線工程のディッシングが歩留まりのボトルネックになっている。"
            "評価基準は平坦性実績・供給安定性・価格の3点。予算は来期分で確保済みです。"
        ),
        when=days_ago(55),
    )
    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "銅配線工程のディッシングが歩留まりのボトルネック"},
        model_score=0.81, rationale="発言から課題を抽出",
        evidence_quote="ディッシングが歩留まりのボトルネックになっている",
        when=days_ago(55), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=100)),
               "driver": "次期量産計画"}, model_score=0.72,
        rationale="納期要望から推定", evidence_quote="評価基準は平坦性実績・供給安定性・価格の3点",
        when=days_ago(55), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(55))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "ディッシング量", "baseline": 25.0, "target": 12.0,
               "unit": "nm", "annual_value": 11000000}, model_score=0.9,
        rationale="効果額の言及から抽出", evidence_quote="ディッシングが歩留まりのボトルネックになっている",
        when=days_ago(54), decision="auto",
    )
    b.advance(eng, Stage.QUALIFIED, when=days_ago(54))

    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:decision_criteria",
        value={"criteria": ["平坦性実績", "供給安定性", "価格"]},
        model_score=0.82, rationale="発言から評価基準を抽出",
        evidence_quote="評価基準は平坦性実績・供給安定性・価格の3点",
        when=days_ago(20), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:budget",
        value={"amount": 29693750, "fiscal_period": "2026年度", "secured": True},
        model_score=0.87, rationale="予算確保状況の言及から抽出",
        evidence_quote="予算は来期分で確保済みです",
        when=days_ago(20), decision="accept",
    )
    b.advance(eng, Stage.PROPOSAL, when=days_ago(19))


def build_deal_5_negotiation(b: DealBuilder):
    """最終交渉(Negotiation): EUVフォトレジストの量産採用案件。決裁者まで到達、
    稟議・競合状況を把握。却下→再提案も含む。"""
    _, eng = b.create_lead(
        "BP-3000002", "EUVフォトレジスト量産採用案件", days_ago(90),
    )
    b.add_products(eng, [
        ("EUV Photoresist NSP-EUV13 (Pilot Lot)", 20, "0"),
        ("EUV Photoresist EUV-RS100 13.5nm (High-End)", 10, "0"),
        ("ArF Immersion Photoresist NSP-AR450", 100, "5"),
    ])
    eng.expected_close_date = date.today() + timedelta(days=45)

    _, champion_role = b.add_contact(
        eng, name="金 敏俊", title="製造技術部長", org_unit="製造技術部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(85),
    )
    _, finance_role = b.add_contact(
        eng, name="朴 惠珍", title="経理部長", org_unit="経理部",
        role=BuyingCenterRole.FINANCE, access_level=AccessLevel.ENGAGED,
        when=days_ago(60),
    )
    decider_node = b.add_placeholder_node(
        eng, label="工場長(氏名未確認)", org_unit="製造本部", seniority_layer=3,
        when=days_ago(30),
    )
    b.link_node(
        eng, decider_node, role=BuyingCenterRole.DECIDER,
        access_level=AccessLevel.NONE, when=days_ago(30),
    )

    champion_node = b.session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == TENANT_ID,
            GraphNode.id == champion_role.node_id,
        )
    ).scalar_one()
    finance_node = b.session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == TENANT_ID,
            GraphNode.id == finance_role.node_id,
        )
    ).scalar_one()
    b.add_edge(eng, champion_node, finance_node, confidence=Confidence.CORROBORATED,
               sequence=1, when=days_ago(28))
    b.add_edge(eng, finance_node, decider_node, confidence=Confidence.CORROBORATED,
               sequence=2, when=days_ago(28))

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "金様: EUV露光の量産採用に向けてフォトレジストを評価中。"
            "来期予算でパイロットロットから本採用に切り替えたい。"
            "評価基準は解像性能と保守体制。予算は確保見込み。"
        ),
        when=days_ago(80),
    )
    for field, value, score, quote in [
        ("criterion:identified_pain", {"summary": "EUV露光の解像性能が量産目標に未達"}, 0.85,
         "EUV露光の量産採用に向けてフォトレジストを評価中"),
        ("criterion:timing", {"target_date": str(date.today() + timedelta(days=60)),
                               "driver": "来期予算での量産切替計画"}, 0.77,
         "来期予算でパイロットロットから本採用に切り替えたい"),
    ]:
        b.propose(eng, src1, target_type="qualification_slot", field_path=field,
                   value=value, model_score=score, rationale="発言から抽出",
                   evidence_quote=quote, when=days_ago(80), decision="accept")
    b.advance(eng, Stage.PROSPECT, when=days_ago(79))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "パターン解像限界", "baseline": 15.0, "target": 13.0, "unit": "nm",
               "annual_value": 40000000}, model_score=0.82,
        rationale="効果額の推定", evidence_quote="EUV露光の量産採用に向けてフォトレジストを評価中",
        when=days_ago(78), decision="accept",
    )
    b.advance(eng, Stage.QUALIFIED, when=days_ago(77))

    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:decision_criteria",
        value={"criteria": ["解像性能", "保守体制"]}, model_score=0.75,
        rationale="発言から評価基準を抽出", evidence_quote="評価基準は解像性能と保守体制",
        when=days_ago(60), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:budget",
        value={"amount": 102187500, "fiscal_period": "2026年度", "secured": False},
        model_score=0.68, rationale="予算状況の言及から抽出",
        evidence_quote="予算は確保見込み",
        when=days_ago(60), decision="accept",
    )
    b.advance(eng, Stage.PROPOSAL, when=days_ago(59))

    src2 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "金様: 決裁は工場長が行うが、経理部長の予算承認が前提。"
            "稟議は3階層、法務レビューも必要とのこと。"
            "競合はC社を比較検討中と聞いている。"
        ),
        when=days_ago(20),
    )
    b.propose(
        eng, src2, target_type="qualification_slot",
        field_path="criterion:economic_buyer",
        value={"node_id": str(decider_node.id), "title": "工場長"}, model_score=0.83,
        rationale="決裁者の言及から抽出",
        evidence_quote="決裁は工場長が行うが、経理部長の予算承認が前提",
        when=days_ago(20), decision="accept",
    )
    b.propose(
        eng, src2, target_type="qualification_slot",
        field_path="criterion:paper_process",
        value={"approval_layers": 3, "legal_review_required": True}, model_score=0.79,
        rationale="稟議階層と法務レビュー要否を抽出",
        evidence_quote="稟議は3階層、法務レビューも必要とのこと",
        when=days_ago(20), decision="accept",
    )
    # 最初の競合抽出は誤り(C社のみと誤認)→ 却下、翌週の確認で訂正
    b.propose(
        eng, src2, target_type="qualification_slot", field_path="criterion:competition",
        value={"vendors": ["C社"]}, model_score=0.6,
        rationale="発言から競合を抽出", evidence_quote="競合はC社を比較検討中と聞いている",
        when=days_ago(20), decision="reject",
        corrected_value={"vendors": ["A社", "C社"], "incumbent": "A社"},
    )
    src3 = b.ingest(
        eng, kind=SourceKind.EMAIL,
        raw_text=(
            "追記: 確認したところ、現行ベンダーのA社も含めて比較検討しているとのことでした。"
        ),
        when=days_ago(13),
    )
    b.propose(
        eng, src3, target_type="qualification_slot", field_path="criterion:competition",
        value={"vendors": ["A社", "C社"], "incumbent": "A社"}, model_score=0.86,
        rationale="訂正情報を反映して再抽出",
        evidence_quote="現行ベンダーのA社も含めて比較検討している",
        when=days_ago(13), decision="accept",
    )
    b.advance(eng, Stage.NEGOTIATION, when=days_ago(12))

    b.verify_slot(
        eng, Criterion.ECONOMIC_BUYER, method=VerificationMethod.MANAGER_CONFIRMATION,
        note="営業部長が工場長への電話確認で決裁権限を確認", when=days_ago(3),
    )


def build_deal_6_closed_won(b: DealBuilder):
    """既存契約(Closed Won): CMP装置一式導入契約。Waiverで前倒しし、後日
    正式に条件を満たして受注。見積もり・契約まで発行する。"""
    _, eng = b.create_lead(
        "BP-3000001", "CMP装置一式導入契約", days_ago(150),
    )
    b.add_products(eng, [
        ("CMP Tool NSC-CMP-Pro100 (300mm wafer process)", 1, "0"),
        ("Chemical Delivery System CDS-500 (Point-of-Use)", 1, "0"),
        ("Chemical Blending Unit CBU-200 (PFA wetted)", 1, "0"),
    ])

    _, decider_role = b.add_contact(
        eng, name="呉 俊傑", title="取締役製造本部長", org_unit="製造本部",
        role=BuyingCenterRole.DECIDER, access_level=AccessLevel.ENGAGED,
        when=days_ago(145),
    )
    b.add_contact(
        eng, name="謝 美玲", title="経理部長", org_unit="経理部",
        role=BuyingCenterRole.FINANCE, access_level=AccessLevel.ENGAGED,
        when=days_ago(120),
    )
    decider_node = b.session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == TENANT_ID, GraphNode.id == decider_role.node_id,
        )
    ).scalar_one()

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "呉様: CMP工程を300mmライン向けに一式刷新したい。歩留まり改善効果は"
            "年間2000万円想定。評価基準は納期と価格。予算は確保済み。"
        ),
        when=days_ago(140),
    )
    for field, value, score, quote in [
        ("criterion:identified_pain", {"summary": "既存CMPラインの老朽化"}, 0.8,
         "CMP工程を300mmライン向けに一式刷新したい"),
        ("criterion:timing", {"target_date": str(date.today() - timedelta(days=60)),
                               "driver": "既存契約更新時期"}, 0.75,
         "CMP工程を300mmライン向けに一式刷新したい"),
    ]:
        b.propose(eng, src1, target_type="qualification_slot", field_path=field,
                   value=value, model_score=score, rationale="発言から抽出",
                   evidence_quote=quote, when=days_ago(140), decision="accept")
    b.advance(eng, Stage.PROSPECT, when=days_ago(139))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "歩留まり", "baseline": 91.0, "target": 96.5, "unit": "%",
               "annual_value": 20000000}, model_score=0.85, rationale="効果額の言及から抽出",
        evidence_quote="歩留まり改善効果は年間2000万円想定",
        when=days_ago(138), decision="accept",
    )
    b.advance(eng, Stage.QUALIFIED, when=days_ago(137))

    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:decision_criteria",
        value={"criteria": ["納期", "価格"]}, model_score=0.7,
        rationale="発言から評価基準を抽出", evidence_quote="評価基準は納期と価格",
        when=days_ago(120), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:budget",
        value={"amount": 2267500000, "fiscal_period": "2025年度", "secured": True},
        model_score=0.9, rationale="予算確保状況の言及から抽出", evidence_quote="予算は確保済み",
        when=days_ago(120), decision="accept",
    )
    b.advance(eng, Stage.PROPOSAL, when=days_ago(119))

    # まだ paper_process / competition が無い状態で最終交渉入りを急いだため一度ブロックされ、
    # Waiver で例外承認 → 後日正式に情報を埋める、という現実的な流れを再現する。
    policy_id = b.find_policy_id("stage.negotiation")
    b.issue_waiver(
        eng, policy_id,
        "四半期末の受注確定を優先するため、稟議・競合情報の確認前に先行して交渉入りを承認",
        when=days_ago(100),
    )
    b.advance(eng, Stage.NEGOTIATION, when=days_ago(100),
              waiver_id=b.session.execute(
                  select(Waiver.id).where(
                      Waiver.tenant_id == TENANT_ID, Waiver.engagement_id == eng.id,
                  )
              ).scalar_one())

    src2 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "呉様: 決裁は私の権限で完結します。稟議は2階層、法務レビュー不要。"
            "他社との比較は行わず、貴社に一本化する方針です。"
        ),
        when=days_ago(90),
    )
    b.propose(
        eng, src2, target_type="qualification_slot",
        field_path="criterion:economic_buyer",
        value={"node_id": str(decider_node.id), "title": "取締役製造本部長"},
        model_score=0.9, rationale="決裁者の言及から抽出",
        evidence_quote="決裁は私の権限で完結します",
        when=days_ago(90), decision="accept",
    )
    b.propose(
        eng, src2, target_type="qualification_slot",
        field_path="criterion:paper_process",
        value={"approval_layers": 2, "legal_review_required": False}, model_score=0.87,
        rationale="稟議階層の言及から抽出", evidence_quote="稟議は2階層、法務レビュー不要",
        when=days_ago(90), decision="accept",
    )
    b.propose(
        eng, src2, target_type="qualification_slot", field_path="criterion:competition",
        value={"vendors": [], "incumbent": None}, model_score=0.72,
        rationale="単独見積である旨を抽出", evidence_quote="貴社に一本化する方針です",
        when=days_ago(90), decision="accept",
    )

    b.verify_slot(
        eng, Criterion.ECONOMIC_BUYER, method=VerificationMethod.CUSTOMER_DOCUMENT,
        evidence_uri="s3://demo-bucket/apex-foundry-order-confirmation.pdf",
        when=days_ago(25),
    )
    b.verify_slot(
        eng, Criterion.BUDGET, method=VerificationMethod.CUSTOMER_DOCUMENT,
        evidence_uri="s3://demo-bucket/apex-foundry-po.pdf", when=days_ago(25),
    )
    b.advance(eng, Stage.CLOSED_WON, when=days_ago(20))

    quote, contract = b.wire_quote_and_contract(
        eng, when=days_ago(19),
        start_date=(NOW - timedelta(days=19)).date(),
        end_date=(NOW - timedelta(days=19) + timedelta(days=365)).date(),
    )
    print(
        f"  {eng.name}: 見積もり {quote.quote_number}、契約 {contract.contract_number} "
        f"を発行しました(合計 {contract.total_amount:,.0f} {contract.currency})。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存の商談データはこのテナント分のみ全削除される)",
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
        wipe_tenant_data(session)
        session.commit()

        upsert_gate_policies(session, tenant_id=TENANT_ID, industry_template="manufacturing")
        upsert_default_autonomy(session, tenant_id=TENANT_ID)
        session.commit()

        b = DealBuilder(session)
        build_deal_1_lead(b)
        build_deal_2_prospect(b)
        build_deal_3_qualified(b)
        build_deal_4_proposal(b)
        build_deal_5_negotiation(b)
        build_deal_6_closed_won(b)
        session.commit()

    print(f"Rebuilt demo data via real application logic for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
