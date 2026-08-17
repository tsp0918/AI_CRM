"""期待値台帳(docs/BULK_SIMULATION_SPEC.md §7)。

シミュレータが「投入したもの」と「こうなっているはず」を自分で記録する。
P5の突合(verify/reconcile.py)がこの台帳を基準にする。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class LedgerEntry:
    entity_type: str  # account | product | engagement | quote | contract | shipment | invoice | return
    sim_id: str
    created_on: str  # ISO date(仮想日付)
    crm: dict = field(default_factory=dict)
    erp: dict = field(default_factory=dict)
    aitm: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)


class Ledger:
    def __init__(self):
        self.entries: dict[str, LedgerEntry] = {}

    def record(self, entry: LedgerEntry) -> None:
        self.entries[entry.sim_id] = entry

    def get(self, sim_id: str) -> LedgerEntry | None:
        return self.entries.get(sim_id)

    def by_type(self, entity_type: str) -> list[LedgerEntry]:
        return [e for e in self.entries.values() if e.entity_type == entity_type]

    def to_json(self, path: str | Path) -> None:
        data = {sim_id: asdict(e) for sim_id, e in self.entries.items()}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "Ledger":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        ledger = cls()
        for sim_id, e in raw.items():
            ledger.entries[sim_id] = LedgerEntry(**e)
        return ledger
