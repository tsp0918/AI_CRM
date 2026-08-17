"""config/endpoints.yaml ローダー(docs/BULK_SIMULATION_SPEC.md §2.2)。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(:-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name, _, default = match.groups()
            return os.environ.get(name, default or "")
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class Endpoints:
    def __init__(self, raw: dict):
        self.crm: dict = raw["crm"]
        self.erp: dict = raw["erp"]
        self.aitm: dict = raw["aitm"]

    def all_base_urls(self) -> list[tuple[str, str]]:
        """安全ガード・ヘルスチェック対象の (name, base_url) 一覧。
        値が null(未起動・未使用と判明済み)のものは除外する。"""
        pairs: list[tuple[str, str]] = [
            ("crm", self.crm["base_url"]),
            ("erp", self.erp["base_url"]),
        ]
        for name, url in self.aitm.items():
            if name in ("org_id", "bearer", "signing_secret", "test_watchlist_enabled"):
                continue
            if url:
                pairs.append((f"aitm.{name}", url))
        return pairs


def load_endpoints(path: str | Path = "simulation/config/endpoints.yaml") -> Endpoints:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Endpoints(_expand_env(raw))
