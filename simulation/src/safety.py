"""本番・ステージングでの誤実行を防ぐガード(docs/BULK_SIMULATION_SPEC.md §2.3)。
実行の最初(run.py 冒頭)で必ず呼び出すこと。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from .config import Endpoints

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}


def assert_safe_environment(cfg: Endpoints) -> None:
    for name, url in cfg.all_base_urls():
        host = urlparse(url).hostname
        if host not in _ALLOWED_HOSTS and not os.getenv("SIM_ALLOW_REMOTE"):
            raise RuntimeError(
                f"{name} の接続先 {host} はローカルではありません。"
                f"検証環境であることを確認のうえ SIM_ALLOW_REMOTE=1 を設定してください。"
            )
    if not cfg.aitm.get("test_watchlist_enabled"):
        raise RuntimeError("テスト用ウォッチリストが有効になっていません(§4.4)")
