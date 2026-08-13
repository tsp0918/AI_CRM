"""デモ用パイプラインデータの投入コマンド。

  python scripts/seed_demo_pipeline.py --tenant-id <uuid>

指定テナントの既存データを全削除したうえで、Lead〜既存契約まで6段階の
案件を実際のアプリのロジックを通して作り直す。直接 INSERT ではなく、
IngestionSource → ExtractionProposal(承認/却下/自動適用) →
apply_proposal、apply_stage_transition という本物の経路を通す。これにより:
  - 活動ログ(StageTransition/ExtractionProposal)が実運用と同じ形で残る
  - クオリフィケーション値に evidence_quote(根拠)が紐づく
  - Lead には Contact 登録(register_contact_and_link)で人物が伴う

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
    AccessLevel, BuyingCenterRole, Confidence, Criterion, EdgeType,
    ProposalStatus, SourceKind, Stage, Stance, VerificationMethod,
)
from crm_mvp.models import (
    Account, Engagement, ExtractionProposal, GraphEdge, GraphNode,
    IngestionSource, QualificationSlot, Waiver,
)
from crm_mvp.schemas.extraction import ExtractedClaim
from crm_mvp.services.apply_proposal import apply_proposal
from crm_mvp.services.contacts import (
    link_contact_to_engagement, register_contact_and_link,
)
from crm_mvp.services.decay_policy import compute_decays_at
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

    def create_lead(self, account_name: str, engagement_name: str, created: datetime):
        account = Account(tenant_id=TENANT_ID, name=account_name)
        self.session.add(account)
        self.session.flush()
        engagement = Engagement(
            tenant_id=TENANT_ID, account_id=account.id, name=engagement_name,
            stage=Stage.LEAD, created_at=created,
        )
        self.session.add(engagement)
        self.session.flush()
        return account, engagement

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


def wipe_tenant_data(session: Session) -> None:
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
    """Lead: 引き合いが来たばかり。提案はまだ未レビュー(承認待ち)。"""
    _, eng = b.create_lead(
        "山田電子工業株式会社", "生産設備更新に関する引き合い", days_ago(4),
    )
    b.add_contact(
        eng, name="山田 太郎", title="購買担当", org_unit="購買部",
        role=None, access_level=AccessLevel.CONTACTED, when=days_ago(4),
    )
    source = b.ingest(
        eng, kind=SourceKind.EMAIL,
        raw_text=(
            "お世話になっております。現行の検査工程で歩留まりが伸び悩んでおり、"
            "設備更新のご相談ができればと思いご連絡しました。"
        ),
        when=days_ago(4),
    )
    b.propose(
        eng, source, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "検査工程の歩留まりが伸び悩んでいる"}, model_score=0.72,
        rationale="問い合わせ文面から課題感を抽出",
        evidence_quote="現行の検査工程で歩留まりが伸び悩んでおり",
        when=days_ago(4), decision="pending",
    )


def build_deal_2_prospect(b: DealBuilder):
    """Prospect(案件化手前): 初回ヒアリング済み、ペイン・タイミングを確認中。"""
    _, eng = b.create_lead(
        "北陸精密機械株式会社", "検査装置導入検討", days_ago(20),
    )
    b.add_contact(
        eng, name="鈴木 一郎", title="生産技術課長", org_unit="生産技術部",
        role=BuyingCenterRole.USER, access_level=AccessLevel.ENGAGED,
        when=days_ago(18),
    )
    source = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "鈴木様: 検査装置が古く、来年度上期までには入れ替えたいと考えています。"
            "現状は目視検査に頼っている部分が多く、負荷が大きいです。"
        ),
        when=days_ago(15),
    )
    b.propose(
        eng, source, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "目視検査への依存による負荷増"}, model_score=0.81,
        rationale="発言から課題を抽出", evidence_quote="目視検査に頼っている部分が多く、負荷が大きい",
        when=days_ago(15), decision="accept",
    )
    b.propose(
        eng, source, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=200)),
               "driver": "来年度上期の設備更新予定"}, model_score=0.78,
        rationale="発言中の希望時期を抽出", evidence_quote="来年度上期までには入れ替えたい",
        when=days_ago(15), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(15))


def build_deal_3_qualified(b: DealBuilder):
    """案件化(Qualified): 2名と関係構築済み。ペイン・タイミング・定量効果まで確認。"""
    _, eng = b.create_lead(
        "東海鋳造株式会社", "生産ライン増設案件", days_ago(50),
    )
    eng.amount = Decimal("15000000")
    eng.currency = "JPY"
    eng.expected_close_date = date.today() + timedelta(days=120)

    b.add_contact(
        eng, name="田中 修", title="製造部長", org_unit="製造部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(48),
    )
    b.add_contact(
        eng, name="佐々木 玲", title="生産技術主任", org_unit="生産技術部",
        role=BuyingCenterRole.USER, access_level=AccessLevel.ENGAGED,
        when=days_ago(40),
    )

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "田中様: 増産計画に伴いラインを増設したいが、現行工程の歩留まりが"
            "目標より3ポイント低い状態が続いている。年内には方向性を固めたい。"
        ),
        when=days_ago(45),
    )
    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "歩留まりが目標より3ポイント低い"}, model_score=0.84,
        rationale="発言から課題を抽出", evidence_quote="現行工程の歩留まりが目標より3ポイント低い状態",
        when=days_ago(45), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=140)),
               "driver": "増産計画"}, model_score=0.75,
        rationale="発言中の希望時期を抽出", evidence_quote="年内には方向性を固めたい",
        when=days_ago(45), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(45))
    b.advance(eng, Stage.QUALIFIED, when=days_ago(44))

    src2 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "田中様: 歩留まり改善で年間換算 900万円ほどの効果を見込んでいます。"
        ),
        when=days_ago(30),
    )
    b.propose(
        eng, src2, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "歩留まり", "baseline": 92.0, "target": 95.0, "unit": "%",
               "annual_value": 9000000}, model_score=0.79,
        rationale="効果額の言及から抽出", evidence_quote="年間換算 900万円ほどの効果を見込んでいます",
        when=days_ago(30), decision="accept",
    )


def build_deal_4_proposal(b: DealBuilder):
    """提案(Proposal): 評価基準・予算まで確認済み、見積提示中。"""
    _, eng = b.create_lead(
        "中部電子部品株式会社", "半導体検査装置更新案件", days_ago(70),
    )
    eng.amount = Decimal("28000000")
    eng.currency = "JPY"
    eng.expected_close_date = date.today() + timedelta(days=90)

    b.add_contact(
        eng, name="小林 誠", title="製造技術部長", org_unit="製造技術部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(65),
    )
    b.add_contact(
        eng, name="渡辺 美咲", title="品質保証課長", org_unit="品質保証部",
        role=BuyingCenterRole.TECHNICAL_GATE, access_level=AccessLevel.ENGAGED,
        when=days_ago(50),
    )

    src1 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "小林様: 検査タクトタイムを12秒から8秒に短縮したい。年間効果は900万円程度。"
            "評価基準は検査精度・既存ラインとの互換性・保守体制の3点。"
            "予算は来期分で確保済みです。"
        ),
        when=days_ago(55),
    )
    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:identified_pain",
        value={"summary": "検査タクトタイムが長く生産のボトルネックになっている"},
        model_score=0.8, rationale="発言から課題を抽出",
        evidence_quote="検査タクトタイムを12秒から8秒に短縮したい",
        when=days_ago(55), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:timing",
        value={"target_date": str(date.today() + timedelta(days=100)),
               "driver": "生産計画"}, model_score=0.7,
        rationale="納期要望から推定", evidence_quote="検査タクトタイムを12秒から8秒に短縮したい",
        when=days_ago(55), decision="accept",
    )
    b.advance(eng, Stage.PROSPECT, when=days_ago(55))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "検査タクトタイム", "baseline": 12.0, "target": 8.0,
               "unit": "秒", "annual_value": 9000000}, model_score=0.93,
        rationale="効果額の言及から抽出", evidence_quote="年間効果は900万円程度",
        when=days_ago(54), decision="auto",
    )
    b.advance(eng, Stage.QUALIFIED, when=days_ago(54))

    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:decision_criteria",
        value={"criteria": ["検査精度", "既存ラインとの互換性", "保守体制"]},
        model_score=0.82, rationale="発言から評価基準を抽出",
        evidence_quote="評価基準は検査精度・既存ラインとの互換性・保守体制の3点",
        when=days_ago(20), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:budget",
        value={"amount": 28000000, "fiscal_period": "2026年度", "secured": True},
        model_score=0.88, rationale="予算確保状況の言及から抽出",
        evidence_quote="予算は来期分で確保済みです",
        when=days_ago(20), decision="accept",
    )
    b.advance(eng, Stage.PROPOSAL, when=days_ago(19))


def build_deal_5_negotiation(b: DealBuilder):
    """最終交渉(Negotiation): 決裁者まで到達、稟議・競合状況を把握。却下→再提案も含む。"""
    _, eng = b.create_lead(
        "関西セミコンダクタ株式会社", "半導体前工程装置導入案件", days_ago(90),
    )
    eng.amount = Decimal("85000000")
    eng.currency = "JPY"
    eng.expected_close_date = date.today() + timedelta(days=45)

    _, champion_role = b.add_contact(
        eng, name="佐藤 健", title="製造技術部長", org_unit="製造技術部",
        role=BuyingCenterRole.CHAMPION, access_level=AccessLevel.ENGAGED,
        when=days_ago(85),
    )
    _, finance_role = b.add_contact(
        eng, name="伊藤 直子", title="経理部 課長", org_unit="経理部",
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
            "佐藤様: 前工程の歩留まりが低下しており、来期予算で装置更新を検討中。"
            "評価基準は精度と保守体制。予算は確保見込み。"
        ),
        when=days_ago(80),
    )
    for field, value, score, quote in [
        ("criterion:identified_pain", {"summary": "前工程の歩留まりが低下"}, 0.85,
         "前工程の歩留まりが低下しており"),
        ("criterion:timing", {"target_date": str(date.today() + timedelta(days=60)),
                               "driver": "来期予算での更新計画"}, 0.77,
         "来期予算で装置更新を検討中"),
    ]:
        b.propose(eng, src1, target_type="qualification_slot", field_path=field,
                   value=value, model_score=score, rationale="発言から抽出",
                   evidence_quote=quote, when=days_ago(80), decision="accept")
    b.advance(eng, Stage.PROSPECT, when=days_ago(79))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "歩留まり", "baseline": 88.0, "target": 93.0, "unit": "%",
               "annual_value": 15000000}, model_score=0.8,
        rationale="効果額の推定", evidence_quote="前工程の歩留まりが低下しており",
        when=days_ago(78), decision="accept",
    )
    b.advance(eng, Stage.QUALIFIED, when=days_ago(77))

    b.propose(
        eng, src1, target_type="qualification_slot",
        field_path="criterion:decision_criteria",
        value={"criteria": ["検査精度", "保守体制"]}, model_score=0.75,
        rationale="発言から評価基準を抽出", evidence_quote="評価基準は精度と保守体制",
        when=days_ago(60), decision="accept",
    )
    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:budget",
        value={"amount": 85000000, "fiscal_period": "2026年度", "secured": False},
        model_score=0.68, rationale="予算状況の言及から抽出",
        evidence_quote="予算は確保見込み",
        when=days_ago(60), decision="accept",
    )
    b.advance(eng, Stage.PROPOSAL, when=days_ago(59))

    src2 = b.ingest(
        eng, kind=SourceKind.TRANSCRIPT,
        raw_text=(
            "佐藤様: 決裁は工場長が行うが、経理部長の予算承認が前提。"
            "稟議は3階層、法務レビューも必要とのこと。"
            "競合はB社を比較検討中と聞いている。"
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
    # 最初の競合抽出は誤り(B社のみと誤認)→ 却下、翌週の確認で訂正
    b.propose(
        eng, src2, target_type="qualification_slot", field_path="criterion:competition",
        value={"vendors": ["B社"]}, model_score=0.6,
        rationale="発言から競合を抽出", evidence_quote="競合はB社を比較検討中と聞いている",
        when=days_ago(20), decision="reject",
        corrected_value={"vendors": ["A社", "B社"], "incumbent": "A社"},
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
        value={"vendors": ["A社", "B社"], "incumbent": "A社"}, model_score=0.86,
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
    """既存契約(Closed Won): Waiver を使って前倒しし、後日正式に条件を満たして受注。"""
    _, eng = b.create_lead(
        "東北製作所株式会社", "検査ライン一式導入契約", days_ago(150),
    )
    eng.amount = Decimal("42000000")
    eng.currency = "JPY"

    _, decider_role = b.add_contact(
        eng, name="高橋 誠一", title="取締役製造本部長", org_unit="製造本部",
        role=BuyingCenterRole.DECIDER, access_level=AccessLevel.ENGAGED,
        when=days_ago(145),
    )
    b.add_contact(
        eng, name="村上 恵", title="経理部長", org_unit="経理部",
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
            "高橋様: 検査ラインを一式刷新したい。歩留まり改善効果は年間1200万円想定。"
            "評価基準は納期と価格。予算は確保済み。"
        ),
        when=days_ago(140),
    )
    for field, value, score, quote in [
        ("criterion:identified_pain", {"summary": "検査ラインの老朽化"}, 0.8,
         "検査ラインを一式刷新したい"),
        ("criterion:timing", {"target_date": str(date.today() - timedelta(days=60)),
                               "driver": "既存契約更新時期"}, 0.75, "検査ラインを一式刷新したい"),
    ]:
        b.propose(eng, src1, target_type="qualification_slot", field_path=field,
                   value=value, model_score=score, rationale="発言から抽出",
                   evidence_quote=quote, when=days_ago(140), decision="accept")
    b.advance(eng, Stage.PROSPECT, when=days_ago(139))

    b.propose(
        eng, src1, target_type="qualification_slot", field_path="criterion:metrics",
        value={"kpi": "歩留まり", "baseline": 90.0, "target": 96.0, "unit": "%",
               "annual_value": 12000000}, model_score=0.85, rationale="効果額の言及から抽出",
        evidence_quote="歩留まり改善効果は年間1200万円想定",
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
        value={"amount": 42000000, "fiscal_period": "2025年度", "secured": True},
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
            "高橋様: 決裁は私の権限で完結します。稟議は2階層、法務レビュー不要。"
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
        evidence_uri="s3://demo-bucket/tohoku-order-confirmation.pdf",
        when=days_ago(25),
    )
    b.verify_slot(
        eng, Criterion.BUDGET, method=VerificationMethod.CUSTOMER_DOCUMENT,
        evidence_uri="s3://demo-bucket/tohoku-po.pdf", when=days_ago(25),
    )
    b.advance(eng, Stage.CLOSED_WON, when=days_ago(20))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存データはこのテナント分のみ全削除される)",
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
