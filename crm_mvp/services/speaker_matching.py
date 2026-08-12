"""話者同定: 発言者を Contact / GraphNode に対応付ける(HANDOVER.md §5 Phase2-7)。

IngestionSource.participants に結果を書き込む形を想定している
（ingestion.py のコメント: 「話者と CRM 上の Contact / GraphNode の
対応付け結果」）。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from .graph_resolution import resolve_or_create_node


@dataclass(slots=True)
class SpeakerMatch:
    speaker_label: str
    contact_id: uuid.UUID | None
    node_id: uuid.UUID
    created_placeholder: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["contact_id"] = str(d["contact_id"]) if d["contact_id"] else None
        d["node_id"] = str(d["node_id"])
        return d


def match_speakers(
    session: Session,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    speakers: list[dict],
) -> list[SpeakerMatch]:
    """speakers: [{"label": "田中さん", "email": "tanaka@example.com"} ...]"""
    results: list[SpeakerMatch] = []
    for speaker in speakers:
        label = speaker.get("label") or speaker.get("name") or "不明な発言者"
        email = speaker.get("email")

        node, created = resolve_or_create_node(
            session, tenant_id, account_id, name=label, email=email,
        )
        results.append(SpeakerMatch(
            speaker_label=label,
            contact_id=node.contact_id,
            node_id=node.id,
            created_placeholder=created,
        ))
    return results
