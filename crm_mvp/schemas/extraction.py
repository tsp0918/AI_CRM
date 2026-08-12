"""抽出契約（アプリ <-> LLM）。

要点は「毎回すべてを抽出させない」こと。
ゲート評価が返す missing = required - known を使って
その商談で今まさに不足している項目だけを抽出対象スキーマとして渡す。
トークンも減り、幻覚も減り、提案の関連性も上がる。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from ..enums import (
    AccessLevel, BuyingCenterRole, Confidence, Criterion, EdgeType, Stance,
)


class ExtractionTarget(BaseModel):
    """抽出してほしい1項目の指示。missing から動的に生成される。"""

    target_type: str
    field_path: str
    description: str
    json_schema: dict = Field(default_factory=dict)
    # この項目を得るための想定質問（獲得の一手にも再利用される）
    acquisition_hint: str | None = None


class ExtractionRequest(BaseModel):
    source_id: uuid.UUID
    engagement_id: uuid.UUID
    transcript: str
    occurred_at: datetime | None = None
    known_participants: list[dict] = Field(default_factory=list)
    known_nodes: list[dict] = Field(default_factory=list)
    targets: list[ExtractionTarget]


class ExtractedClaim(BaseModel):
    """LLM が返す1件。evidence_quote が無いものは受け付けない。"""

    target_type: str
    field_path: str
    value: dict
    model_score: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_quote: str
    evidence_offset: tuple[int, int] | None = None

    @field_validator("evidence_quote")
    @classmethod
    def _must_have_evidence(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("evidence_quote は必須。根拠なき提案は破棄する。")
        return v


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)
    # 「この商談で次に確認すべき1つ」。複数出さないことがUI上の要件。
    next_question: str | None = None
    summary: str | None = None
    extractor_version: str


# --- criterion ごとの値スキーマ（抽出指示の生成に使う） ---------------------

CRITERION_SCHEMAS: dict[Criterion, dict] = {
    Criterion.METRICS: {
        "type": "object",
        "properties": {
            "kpi": {"type": "string"},
            "baseline": {"type": "number"},
            "target": {"type": "number"},
            "unit": {"type": "string"},
            "annual_value": {"type": "number"},
        },
        "required": ["kpi"],
    },
    Criterion.ECONOMIC_BUYER: {
        "type": "object",
        "properties": {
            "node_id": {"type": "string"},
            "name": {"type": "string"},
            "title": {"type": "string"},
        },
    },
    Criterion.PAPER_PROCESS: {
        "type": "object",
        "properties": {
            "approval_layers": {"type": "integer"},
            "legal_review_required": {"type": "boolean"},
            "standard_lead_time_days": {"type": "integer"},
        },
    },
    Criterion.DECISION_PROCESS: {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": {"type": "string"}},
            "next_milestone": {"type": "string"},
            "next_milestone_date": {"type": "string", "format": "date"},
        },
    },
    Criterion.COMPETITION: {
        "type": "object",
        "properties": {
            "vendors": {"type": "array", "items": {"type": "string"}},
            "incumbent": {"type": "string"},
        },
    },
    Criterion.BUDGET: {
        "type": "object",
        "properties": {
            "amount": {"type": "number"},
            "fiscal_period": {"type": "string"},
            "secured": {"type": "boolean"},
        },
    },
    Criterion.TIMING: {
        "type": "object",
        "properties": {
            "target_date": {"type": "string", "format": "date"},
            "driver": {"type": "string"},
        },
    },
}

# 各項目を獲得するための一手。missing 表示時にそのまま担当者へ提示する。
ACQUISITION_PLAYS: dict[Criterion, str] = {
    Criterion.ECONOMIC_BUYER:
        "チャンピオンに『この規模だと最終決裁はどなたになりますか』と確認する",
    Criterion.PAPER_PROCESS:
        "稟議の階層数と法務レビューの要否を確認し、リードタイムを逆算する",
    Criterion.METRICS:
        "現状値と目標値を数字で聞き出し、年間効果額に換算して提案書に載せる",
    Criterion.CHAMPION:
        "予算とKPIを持ち、当方不在時にも社内を動かせる人物を特定する",
    Criterion.COMPETITION:
        "比較検討中のベンダーと、選定基準の優先順位を確認する",
    Criterion.DECISION_CRITERIA:
        "評価項目とその重み付けを、できれば書面で入手する",
    Criterion.BUDGET:
        "予算化済みか、これから確保するのかを確認する",
    Criterion.TIMING:
        "納期・稼働開始の要求日と、その背後にある理由を確認する",
    Criterion.IDENTIFIED_PAIN:
        "現状の痛みが誰のKPIに効いているかまで掘り下げる",
    Criterion.DECISION_PROCESS:
        "次のマイルストーンと、そこに至る社内手続きを確認する",
}
