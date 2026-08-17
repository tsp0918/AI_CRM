"""P0完了条件の確認スクリプト(docs/BULK_SIMULATION_SPEC.md §2)。

  - 3システムが起動していること(§2.1)
  - 安全ガードが通ること(§2.3)
  - 署名付きリクエストが往復すること(§2.2)

実行: PYTHONPATH=. .venv/bin/python simulation/scripts/p0_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from simulation.src.clients.base import SignedSimClient
from simulation.src.config import load_endpoints
from simulation.src.safety import assert_safe_environment

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "endpoints.yaml"


def main() -> int:
    cfg = load_endpoints(CONFIG_PATH)

    print("=== §2.1 起動確認 ===")
    all_up = True
    for name, url in cfg.all_base_urls():
        try:
            resp = httpx.get(f"{url}/health", timeout=5.0)
            ok = resp.status_code == 200
        except httpx.HTTPError as exc:
            ok = False
            resp = exc
        print(f"  {name:20s} {url:35s} -> {'OK' if ok else 'NG (' + str(resp) + ')'}")
        all_up = all_up and ok
    if not all_up:
        print("NG: 起動していないシステムがあります。P0を完了できません。")
        return 1

    print("\n=== §2.3 安全ガード ===")
    assert_safe_environment(cfg)
    print("  OK: 全接続先がローカルホスト、かつテスト用ウォッチリストが有効")

    print("\n=== §2.2 署名付きリクエスト往復確認(AI_TM screening) ===")
    client = SignedSimClient(
        cfg.aitm["screening"], tenant_id=cfg.aitm["org_id"],
        bearer=cfg.aitm["bearer"], signing_secret=cfg.aitm["signing_secret"],
    )
    try:
        resp = client.post(
            "/api/screen",
            {"company_name": "SIM-P0-CHECK", "country": "JP"},
        )
    finally:
        client.close()
    print(f"  POST {cfg.aitm['screening']}/api/screen -> {resp.status_code}")
    print(f"  body: {resp.text[:300]}")
    if resp.status_code >= 400:
        print("NG: 署名付きリクエストの往復に失敗しました。")
        return 1

    print("\nP0 完了条件を満たしました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
