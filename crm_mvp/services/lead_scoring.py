"""Lead/Companyスコアリング(ロードマップ§7)。

確度スコア(confidence_score.py)と同じ思想: ルールベース・透明性重視。
ブラックボックスな学習モデルは使わず、各要因の根拠を常に説明できることを
優先する。Company Score(この会社は狙うべき対象か)と Person Score(この
人物が今どれだけ反応しているか)を1つの数値に潰さず分けて見せる —
「良い会社だが担当者の反応が薄い」と「担当者は前のめりだが会社が対象外」
では打つべき手が違うため。

現時点で使える実データ(名寄せ済みAccountの有無、Touchの種別と頻度、
Leadの役職テキスト)だけを根拠にする。業種・企業規模などの外部
firmographicデータは未接続のため、将来の拡張点として残す。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import TouchChannel
from ..models import Account, Lead, Touch

TOUCH_RECENCY_WINDOW_DAYS = 90

HIGH_INTENT_CHANNELS: frozenset[TouchChannel] = frozenset({
    TouchChannel.FORM_SUBMIT, TouchChannel.CONTENT_DOWNLOAD,
    TouchChannel.CALL_CONNECTED, TouchChannel.EMAIL_CLICK,
})

SENIOR_TITLE_KEYWORDS: tuple[str, ...] = (
    # 日本語の役職語彙
    "社長", "取締役", "本部長", "工場長", "部長", "課長", "室長",
    "マネージャー", "リーダー", "CEO", "COO", "CTO", "CFO",
    # 英語の役職語彙(2026-08-14: 製造業の役職語彙に偏っており、海外拠点・
    # 研究機関・大学等の役職者が拾えていなかったため追加)。
    "Chief", "President", "Vice President", "VP", "Director",
    "Head of", "Officer", "Dean", "Provost", "Executive",
    "Principal", "Manager", "Lead",
)


@dataclass(slots=True)
class LeadScore:
    company_score: int  # 0-100
    person_score: int  # 0-100
    company_reasons: list[str] = field(default_factory=list)
    person_reasons: list[str] = field(default_factory=list)

    @property
    def quadrant(self) -> str:
        """Fit(Company) x Engagement(Person) の4象限。"""
        fit = self.company_score >= 50
        engaged = self.person_score >= 50
        if fit and engaged:
            return "hot"
        if fit and not engaged:
            return "watch"
        if not fit and engaged:
            return "nurture"
        return "low"


def _compute_company_score(
    account: Account | None, touches: list[Touch],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if account is not None:
        score += 40
        reasons.append("既存Accountとして名寄せ済み")

    if touches:
        score += 20
        reasons.append(f"接点あり({len(touches)}件)")
        high_intent = [t for t in touches if t.channel in HIGH_INTENT_CHANNELS]
        ratio = len(high_intent) / len(touches)
        pts = round(ratio * 40)
        if pts:
            score += pts
            reasons.append(f"高関心度の接点比率 {ratio:.0%}(+{pts}点)")

    return min(score, 100), reasons


def _compute_person_score(
    lead: Lead, touches: list[Touch], now: datetime,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if lead.title and any(k in lead.title for k in SENIOR_TITLE_KEYWORDS):
        score += 30
        reasons.append(f"役職「{lead.title}」から意思決定への関与が見込まれる")

    if touches:
        recent = [
            t for t in touches
            if (now - t.occurred_at).days <= TOUCH_RECENCY_WINDOW_DAYS
        ]
        freq_pts = min(len(recent) * 10, 40)
        if freq_pts:
            score += freq_pts
            reasons.append(
                f"直近{TOUCH_RECENCY_WINDOW_DAYS}日以内の接点 {len(recent)}件(+{freq_pts}点)"
            )

        high_intent = [t for t in touches if t.channel in HIGH_INTENT_CHANNELS]
        if high_intent:
            pts = min(len(high_intent) * 10, 30)
            score += pts
            reasons.append(f"高関心度の反応 {len(high_intent)}件(+{pts}点)")

    return min(score, 100), reasons


def compute_lead_score(
    lead: Lead, account: Account | None, touches: list[Touch],
    now: datetime | None = None,
) -> LeadScore:
    now = now or datetime.now(timezone.utc)
    company_score, company_reasons = _compute_company_score(account, touches)
    person_score, person_reasons = _compute_person_score(lead, touches, now)
    return LeadScore(
        company_score=company_score, person_score=person_score,
        company_reasons=company_reasons, person_reasons=person_reasons,
    )


def list_lead_scores(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """一覧画面(Lead一覧のパイプライン/象限ビュー)向けに、テナント内の
    全Leadのスコアを一括計算する。Lead単位で list_touches を都度呼ぶと
    N+1になるため、Account/Touchをそれぞれ1クエリで先に読み込んでおく。"""
    leads = session.execute(
        select(Lead).where(Lead.tenant_id == tenant_id).order_by(Lead.updated_at.desc())
    ).scalars().all()
    if not leads:
        return []

    account_ids = {lead.matched_account_id for lead in leads if lead.matched_account_id}
    accounts = {
        a.id: a for a in session.execute(
            select(Account).where(Account.id.in_(account_ids))
        ).scalars()
    } if account_ids else {}

    touches_by_lead: dict[uuid.UUID, list[Touch]] = {}
    for touch in session.execute(
        select(Touch).where(
            Touch.tenant_id == tenant_id,
            Touch.lead_id.in_([lead.id for lead in leads]),
        )
    ).scalars():
        touches_by_lead.setdefault(touch.lead_id, []).append(touch)

    now = datetime.now(timezone.utc)
    return [
        {
            "lead": lead,
            "score": compute_lead_score(
                lead, accounts.get(lead.matched_account_id), touches_by_lead.get(lead.id, []), now,
            ),
        }
        for lead in leads
    ]
