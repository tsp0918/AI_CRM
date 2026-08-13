"""デモ用Lead/シーケンスデータの投入コマンド。

  python scripts/seed_demo_leads.py --tenant-id <uuid>

指定テナントのLead関連データ(sequence_draft/sequence_enrollment/
sequence_step/sequence/touch/lead)を全削除したうえで、4象限(hot/watch/
nurture/low)にまたがるダミーLeadと、1件のデモシーケンスを実際の
サービス層(record_touch / enroll_lead / generate_due_drafts)を通して
作り直す。scripts/seed_demo_pipeline.py が Engagement 側のデモデータを
作るのと対になる、Lead側のデモデータ投入スクリプト。

§7.4: crm_app ロール(非 superuser)で接続し、RLS のテナント文脈を
自分で SET してから実行する。
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.enums import (
    CampaignChannelType, LeadSourceChannel, LeadStatus, SequenceStepChannel,
    TouchChannel,
)
from crm_mvp.models import Campaign, Lead
from crm_mvp.services.lead_lifecycle import convert_lead, promote_lead, record_touch
from crm_mvp.services.sequences import (
    create_sequence, enroll_lead, generate_due_drafts, mark_draft_reviewed,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)

LEAD_TABLES = [
    "sequence_draft", "sequence_enrollment", "sequence_step", "sequence",
    "touch", "lead", "campaign",
]


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def wipe_lead_data(session: Session) -> None:
    for table in LEAD_TABLES:
        session.execute(
            text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
        )


def build_demo_campaigns(session: Session) -> dict[str, Campaign]:
    webinar = Campaign(
        tenant_id=TENANT_ID, name="2026夏 製造業向けウェビナー",
        channel_type=CampaignChannelType.EVENT, owner_team="marketing",
        cost=500000, start_date=days_ago(20).date(), end_date=days_ago(13).date(),
    )
    outbound = Campaign(
        tenant_id=TENANT_ID, name="IS 新規開拓アウトバウンドリスト Q3",
        channel_type=CampaignChannelType.OUTBOUND_SEQUENCE, owner_team="inside_sales",
    )
    session.add_all([webinar, outbound])
    session.flush()
    return {"webinar": webinar, "outbound": outbound}


def build_demo_sequence(session: Session):
    """3ステップ + 分岐。ステップ0への反応(クリック/資料DL)があれば
    架電をスキップしてホットパス(order2)へ、無ければ既定の架電フォロー
    (order1)へ進む。"""
    return create_sequence(
        session, TENANT_ID, name="製造業 新規開拓 3ステップ",
        description="資料DL・問い合わせ後のフォローアップ想定(反応で分岐)",
        steps=[
            {
                "channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                "subject_template": "{{company_name}}様へのご案内",
                "body_template": (
                    "{{full_name}}様\n\nお世話になっております。{{recent_signal}}を"
                    "拝見し、ご連絡いたしました。導入事例など詳しい資料をお送り"
                    "できますが、一度お話しする機会をいただけますでしょうか。"
                ),
                "reaction_channels": [TouchChannel.EMAIL_CLICK, TouchChannel.CONTENT_DOWNLOAD],
                "on_reaction_next_order": 2,  # 反応ありならホットパスへ直行
                "on_no_reaction_next_order": 1,  # 反応なしなら既定どおり架電へ
            },
            {
                "channel": SequenceStepChannel.CALL_TASK, "delay_days": 3,
                "body_template": (
                    "{{full_name}}様({{title}})へお電話し、検討状況とお困りごとを伺う。"
                ),
            },
            {
                "channel": SequenceStepChannel.EMAIL, "delay_days": 1,
                "subject_template": "ぜひ一度お話ししましょう",
                "body_template": (
                    "{{full_name}}様\n\nご関心をお寄せいただきありがとうございます。"
                    "{{recent_signal}}を拝見し、ぜひ一度お話しできればと存じます。"
                    "今週中でご都合の良い日時はございますか。"
                ),
            },
        ],
        actor=f"human:{ACTOR_ID}",
    )


def build_lead_hot(session: Session, sequence, campaign: Campaign | None = None) -> Lead:
    """Hot: 高関心度の接点複数 + 役職マッチ。ステップ0への反応(メール内
    リンクのクリック)を記録し、分岐でステップ1(架電)を飛ばしてステップ2
    (ホットパス)まで進める — 「step2まで実行が進んだ状態」の実行済みビュー。
    """
    lead = Lead(
        tenant_id=TENANT_ID, company_name="関東鋳造株式会社", full_name="小林 直樹",
        title="生産技術部長", email="kobayashi@example.com",
        source_channel=LeadSourceChannel.CONTENT, status=LeadStatus.WORKING,
        source_campaign_id=campaign.id if campaign else None,
        owner="IS 田中", written_by=f"human:{ACTOR_ID}", created_at=days_ago(6),
    )
    session.add(lead)
    session.flush()

    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.CONTENT_DOWNLOAD,
        occurred_at=days_ago(6), source_system="manual", actor="system",
        campaign_id=campaign.id if campaign else None,
        raw_payload={"note": "導入事例資料をダウンロード"},
    )
    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.FORM_SUBMIT,
        occurred_at=days_ago(5), source_system="manual", actor="system",
    )

    enroll_lead(session, TENANT_ID, lead, sequence, actor=f"human:{ACTOR_ID}", now=days_ago(5))
    first_drafts = generate_due_drafts(session, TENANT_ID, now=days_ago(5))  # ステップ0生成
    mark_draft_reviewed(first_drafts[0], reviewed_by=f"human:{ACTOR_ID}", now=days_ago(4))

    # ステップ0メール送付後、本文中のリンクをクリック(=反応あり)
    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.EMAIL_CLICK,
        occurred_at=days_ago(4), source_system="manual", actor="system",
        raw_payload={"note": "案内メール内のリンクをクリック"},
    )
    generate_due_drafts(session, TENANT_ID, now=days_ago(2))  # 反応検知→架電を飛ばしてホットパス(order2)へ
    return lead


def build_lead_watch(session: Session, sequence, campaign: Campaign | None = None) -> Lead:
    """Watch: 高関心度の接点はあるが1件のみ、役職不明。ステップ0への反応が
    無いため、既定どおりステップ1(架電)まで進める — 分岐しなかった側の
    パスを見せる対比。"""
    lead = Lead(
        tenant_id=TENANT_ID, company_name="三河バルブ工業株式会社", full_name="佐藤 健一",
        source_channel=LeadSourceChannel.OUTBOUND, status=LeadStatus.WORKING,
        source_campaign_id=campaign.id if campaign else None,
        owner="IS 田中", written_by=f"human:{ACTOR_ID}", created_at=days_ago(3),
    )
    session.add(lead)
    session.flush()

    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.FORM_SUBMIT,
        occurred_at=days_ago(3), source_system="manual", actor="system",
        campaign_id=campaign.id if campaign else None,
    )

    enroll_lead(session, TENANT_ID, lead, sequence, actor=f"human:{ACTOR_ID}", now=days_ago(3))
    generate_due_drafts(session, TENANT_ID, now=days_ago(3))  # ステップ0生成、反応なし
    generate_due_drafts(session, TENANT_ID, now=days_ago(0))  # 反応なし確定→ステップ1(架電)へ
    return lead


def build_lead_nurture(session: Session) -> Lead:
    """Nurture: 役職は決裁層に近いが、反応は低関心度チャネル(メール開封)
    のみ。まだシーケンスには登録しない(様子見の状態を再現)。"""
    lead = Lead(
        tenant_id=TENANT_ID, company_name="相模鉄工株式会社", full_name="田村 誠",
        title="工場長", source_channel=LeadSourceChannel.EVENT, status=LeadStatus.WORKING,
        owner="IS 鈴木", written_by=f"human:{ACTOR_ID}", created_at=days_ago(10),
    )
    session.add(lead)
    session.flush()

    for d in (9, 6, 3):
        record_touch(
            session, TENANT_ID, lead, channel=TouchChannel.EMAIL_OPEN,
            occurred_at=days_ago(d), source_system="manual", actor="system",
        )
    return lead


def build_lead_converted(session: Session, campaign: Campaign | None = None) -> Lead:
    """既に案件化済みのLead。SQLまで進めてから convert_lead を実行し、
    Engagement側の「Lead発生経緯」カードを実データで確認できるようにする。
    """
    lead = Lead(
        tenant_id=TENANT_ID, company_name="長野電子工業株式会社", full_name="高橋 美咲",
        title="購買部 課長", email="takahashi@example.com",
        source_channel=LeadSourceChannel.OUTBOUND, status=LeadStatus.WORKING,
        source_campaign_id=campaign.id if campaign else None,
        owner="IS 田中", written_by=f"human:{ACTOR_ID}", created_at=days_ago(18),
    )
    session.add(lead)
    session.flush()

    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.CALL_CONNECTED,
        occurred_at=days_ago(16), source_system="manual", actor="system",
        campaign_id=campaign.id if campaign else None,
        raw_payload={"note": "初回架電、来期予算での更新を検討中とのこと"},
    )
    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.CONTENT_DOWNLOAD,
        occurred_at=days_ago(12), source_system="manual", actor="system",
        raw_payload={"note": "料金表資料をダウンロード"},
    )
    record_touch(
        session, TENANT_ID, lead, channel=TouchChannel.FORM_SUBMIT,
        occurred_at=days_ago(9), source_system="manual", actor="system",
        raw_payload={"note": "商談希望フォームを送信"},
    )

    promote_lead(session, lead, LeadStatus.MQL)
    promote_lead(session, lead, LeadStatus.SQL)
    convert_lead(session, TENANT_ID, lead, actor=f"human:{ACTOR_ID}")
    return lead


def build_lead_low(session: Session) -> Lead:
    """Low: 登録されたばかりで接点も役職情報もない、素の初期状態。"""
    lead = Lead(
        tenant_id=TENANT_ID, company_name="北信精機株式会社", full_name="木村 さゆり",
        title="総務", source_channel=LeadSourceChannel.INBOUND, status=LeadStatus.NEW,
        owner="IS 鈴木", written_by=f"human:{ACTOR_ID}", created_at=days_ago(1),
    )
    session.add(lead)
    session.flush()
    return lead


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存のLead関連データはこのテナント分のみ全削除される)",
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
        wipe_lead_data(session)
        session.commit()

        campaigns = build_demo_campaigns(session)
        sequence = build_demo_sequence(session)
        build_lead_hot(session, sequence, campaign=campaigns["webinar"])
        build_lead_watch(session, sequence, campaign=campaigns["outbound"])
        build_lead_nurture(session)
        build_lead_converted(session, campaign=campaigns["outbound"])
        build_lead_low(session)
        session.commit()

    print(f"Rebuilt demo lead/sequence data for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
