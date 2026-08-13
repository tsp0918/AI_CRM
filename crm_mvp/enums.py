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
