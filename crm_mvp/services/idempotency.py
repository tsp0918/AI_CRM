"""冪等キー生成(HANDOVER.md §5 Phase5, item 20)。

外部スクリーニングへの再送信で重複リクエストを防ぐためのキー。
AI_TM 側の契約: hash(subject_normalized, check_type, policy_version)
(CRM_INTEGRATION_HANDOVER.md §11 参照)。
"""

from __future__ import annotations

import hashlib
import re


def normalize_subject(name: str) -> str:
    """会社名の表記ゆれを吸収する最小限の正規化(前後空白・連続空白・大文字小文字)。"""
    return re.sub(r"\s+", " ", name.strip().lower())


def compute_idempotency_key(
    subject: str, check_type: str, policy_version: str,
) -> str:
    normalized = normalize_subject(subject)
    payload = f"{normalized}|{check_type}|{policy_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
