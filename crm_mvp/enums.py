"""共通列挙型。

設計方針:
- 列挙型は「製品側が定義する標準」であり、顧客ごとの追加は原則行わない。
- 顧客差分は GatePolicy の conditions(JSONB) と業種テンプレートで吸収する。
"""

from enum import StrEnum


class Stage(StrEnum):
    """商談ステージ。Lead から Contract まで単一エンティティで遷移する。"""

    LEAD = "lead"
    PROSPECT = "prospect"
    QUALIFIED = "qualified"           # 案件化
    PROPOSAL = "proposal"             # 提案・見積提出
    NEGOTIATION = "negotiation"       # 最終交渉
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class Confidence(StrEnum):
    """証拠強度。AI は CORROBORATED までしか昇格させられない。"""

    ASSERTED = "asserted"             # 担当者の主張
    CORROBORATED = "corroborated"     # 活動記録に裏付けあり（AI が昇格可）
    VERIFIED = "verified"             # 顧客側の発言・文書で確認（人のみ）


CONFIDENCE_ORDER: dict[Confidence, int] = {
    Confidence.ASSERTED: 1,
    Confidence.CORROBORATED: 2,
    Confidence.VERIFIED: 3,
}


class Criterion(StrEnum):
    """案件クオリフィケーションの評価軸（MEDDPICC + BANT を包含）。"""

    METRICS = "metrics"
    ECONOMIC_BUYER = "economic_buyer"
    DECISION_CRITERIA = "decision_criteria"
    DECISION_PROCESS = "decision_process"
    PAPER_PROCESS = "paper_process"           # 稟議・契約手続き
    IDENTIFIED_PAIN = "identified_pain"
    CHAMPION = "champion"
    COMPETITION = "competition"
    BUDGET = "budget"
    TIMING = "timing"


class BuyingCenterRole(StrEnum):
    DECIDER = "decider"
    CHAMPION = "champion"
    COACH = "coach"
    USER = "user"
    TECHNICAL_GATE = "technical_gate"         # 品証・技術評価などの関門
    FINANCE = "finance"
    INITIATOR = "initiator"


class Stance(StrEnum):
    """態度。役割とは直交する（決裁者かつ反対者は普通に存在する）。"""

    SUPPORTER = "supporter"
    NEUTRAL = "neutral"
    OPPONENT = "opponent"
    UNKNOWN = "unknown"


class AccessLevel(StrEnum):
    NONE = "none"                     # 未接触（相関図では点線ノード）
    CONTACTED = "contacted"
    ENGAGED = "engaged"


class EdgeType(StrEnum):
    REPORTS_TO = "reports_to"         # 組織階層
    APPROVES = "approves"             # 稟議ルート（組織階層と一致しない）
    INFLUENCES = "influences"
    CONFLICTS_WITH = "conflicts_with"


class GateKind(StrEnum):
    STAGE = "stage"                   # ステージ遷移
    ARTIFACT = "artifact"             # 見積・契約書などの発行
    FRESHNESS = "freshness"           # コンプライアンス項目の鮮度


class GateStrength(StrEnum):
    ADVISORY = "advisory"             # 表示のみ
    WARN = "warn"                     # 通過可・記録に残る
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class ArtifactType(StrEnum):
    QUOTE = "quote"
    CONTRACT = "contract"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AUTO_APPLIED = "auto_applied"
    SUPERSEDED = "superseded"


class SourceKind(StrEnum):
    TRANSCRIPT = "transcript"         # Teams / Zoom の文字起こし
    RECORDING = "recording"           # 録画・録音（STT 後に transcript 化）
    EMAIL = "email"
    FREE_NOTE = "free_note"           # 自由記述メモ（唯一の手入力口）
    CRM_SYNC = "crm_sync"             # 既存 CRM からの取り込み
    CALENDAR_SYNC = "calendar_sync"   # Outlook/Teams 等からの自動同期（会議録・出席者）


class ComplianceCheckType(StrEnum):
    ANTI_SOCIAL = "anti_social"
    CREDIT = "credit"
    SANCTIONS = "sanctions"
    EXPORT_CONTROL = "export_control"


class ComplianceOutcome(StrEnum):
    CLEAR = "clear"
    HIT = "hit"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class AutonomyMode(StrEnum):
    """フィールド単位の自動適用モード。承認実績に応じて自動昇格する。"""

    ALWAYS_CONFIRM = "always_confirm"
    AUTO_IF_TRUSTED = "auto_if_trusted"   # 承認率が閾値を超えたら自動適用
    ALWAYS_AUTO = "always_auto"
    NEVER_AI = "never_ai"                 # AI 書き込み禁止（VERIFIED 等）


class VerificationMethod(StrEnum):
    """§7.2: VERIFIED への昇格経路。標準は顧客文書、代替は上長確認。"""

    CUSTOMER_DOCUMENT = "customer_document"   # 標準: 顧客発の文書・発言の添付
    MANAGER_CONFIRMATION = "manager_confirmation"  # 代替: 上長による確認


class ActionItemStatus(StrEnum):
    """『次の一手』をタスク化した ActionItem の状態。"""

    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class ReviewStatus(StrEnum):
    """週次レビューでマネージャーが付ける簡易ステータス(2026-08-14)。
    自由記述のコメントだけだと横断的に拾えないため、タグとして持つ。"""

    ON_TRACK = "on_track"       # 順調
    AT_RISK = "at_risk"         # 要注意
    ESCALATE = "escalate"       # エスカレーション


class LeadStatus(StrEnum):
    """Lead(案件化前の見込み客)のライフサイクル。"""

    NEW = "new"                   # 初回接点のみ
    WORKING = "working"           # IS/SDR が接触中
    MQL = "mql"                   # スコア閾値到達(Marketing Qualified)
    SQL = "sql"                   # AEレビュー待ち(Sales Qualified)
    CONVERTED = "converted"       # Engagement へ案件化済み
    DISQUALIFIED = "disqualified"


class LeadSourceChannel(StrEnum):
    """Leadの獲得経路。"""

    INBOUND = "inbound"
    OUTBOUND = "outbound"
    EVENT = "event"
    REFERRAL = "referral"
    CONTENT = "content"
    PARTNER = "partner"


class TouchChannel(StrEnum):
    """Marketing/Inside Sales の個々の接点。"""

    FORM_SUBMIT = "form_submit"
    CONTENT_DOWNLOAD = "content_download"
    EVENT_ATTENDANCE = "event_attendance"
    CALL_ATTEMPTED = "call_attempted"
    CALL_CONNECTED = "call_connected"
    EMAIL_OPEN = "email_open"
    EMAIL_CLICK = "email_click"
    REFERRAL_TOUCH = "referral_touch"
    AD_CLICK = "ad_click"


class CampaignChannelType(StrEnum):
    """ROI集計の単位となるCampaignの種別。"""

    EVENT = "event"
    CONTENT = "content"
    PAID_ADS = "paid_ads"
    OUTBOUND_SEQUENCE = "outbound_sequence"
    PARTNER = "partner"


class SequenceStepChannel(StrEnum):
    """アウトバウンド・シーケンスの1ステップの実行チャネル。"""

    EMAIL = "email"
    CALL_TASK = "call_task"
    LINKEDIN = "linkedin"


class SequenceEnrollmentStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    OPTED_OUT = "opted_out"


class SequenceDraftStatus(StrEnum):
    """送信基盤が未接続のため、常に下書き止まり(実送信は行わない)。"""

    DRAFT = "draft"
    REVIEWED = "reviewed"      # IS担当者が確認し、手動で対応した
    DISMISSED = "dismissed"    # このステップは送らないと判断した


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ContractStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    SIGNED = "signed"
    ACTIVE = "active"
    TERMINATED = "terminated"


class MaterialType(StrEnum):
    """ERP品目マスタ(Material)の品目区分。erp-system の分類をそのまま踏襲する。"""

    FERT = "FERT"      # 完成品(製品)
    HALB = "HALB"      # 半製品(中間体)
    ROH = "ROH"        # 原材料
    HAWA = "HAWA"      # 商品(購入品・転売)


class FeftaJudgment(StrEnum):
    """外為法(輸出管理)判定。erp-system の品目マスタから引き継ぐ。

    CRM側では今回まだゲート判定等には使わないが、将来の輸出コンプライアンス
    連携に備えてフィールドだけ温存する。
    """

    APPROVED = "APPROVED"
    APPLICABLE = "APPLICABLE"
    PENDING = "PENDING"           # 審査中(実ERPエクスポートに実在。当初の設計メモには無かった)
    REJECTED = "REJECTED"         # 却下
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AuthorityLevel(StrEnum):
    """承認権限レベル(2026-08-13 user_matrix_CRM.csv の Authority 列)。

    値なし(NONE)が大多数で、承認権限を持つ人だけに Approver 系の値が
    付く。今回はUserに保持するだけ — 値引率承認フロー等の実際のゲート
    判定にはまだ使わない(将来の拡張として温存)。
    """

    NONE = "none"
    APPROVER = "approver"
    APPROVER_HIGH = "approver_high"
    APPROVER_SUPER = "approver_super"


class EngagementRelationshipType(StrEnum):
    """親商談(クロージング済みの案件)との関係。Engagement.parent_engagement_id
    が設定されている場合のみ意味を持つ(2026-08-13 ユーザー要望: 契約済み案件の
    継続/Upsell/Cross-sellを親商談に紐付けて追跡できる普遍的な構造)。"""

    RENEWAL = "renewal"           # 契約更新(同一商品構成での継続)
    UPSELL = "upsell"             # 既存契約の増量・上位グレード化
    CROSS_SELL = "cross_sell"     # 別カテゴリの商品の追加販売


class OutboxStatus(StrEnum):
    """外部システム(ERP/AI_TM)への送信保証キュー(CRM_連携引き継ぎ書.md §4.4)の状態。"""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DLQ = "dlq"           # 全リトライ失敗、手動再送待ち


class WebhookEventResult(StrEnum):
    """外部システムからのWebhook受信の処理結果(冪等性の記録用)。"""

    PROCESSED = "processed"
    DUPLICATE = "duplicate"   # 同一event_idを再受信(正常系、何もしない)
    STALE = "stale"           # revisionが現在値以下(古いイベント、破棄)
    ERROR = "error"


class OutboxResult(StrEnum):
    """dispatcher(Outboxの送信関数)がprocess_outboxに返す処理結果。"""

    SENT = "sent"
    RETRY = "retry"
    FAILED_NO_RETRY = "failed_no_retry"
