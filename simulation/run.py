#!/usr/bin/env python3
"""3システム横断シミュレーション エントリポイント
(docs/BULK_SIMULATION_SPEC.md §3.2)。

  python simulation/run.py --dry-run       # イベント列だけ生成(P1)
  python simulation/run.py --scale=smoke   # 商談3件のスモーク(P3、未実装)
  python simulation/run.py --scale=medium  # 本実行(P4、未実装)

現時点ではP1(--dry-run)のみ実装している。実APIを叩く--scale実行は
P2(マスタ投入)・flows/*・clients/*の実装後に追加する。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulation.src.config import load_endpoints
from simulation.src.generators.events import generate_all_events
from simulation.src.generators.masters import generate_masters
from simulation.src.rng import Rng
from simulation.src.safety import assert_safe_environment

ROOT = Path(__file__).resolve().parent
EXPECTED_TOTALS_KEYS = ("engagements", "quotes", "contracts", "shipments")


def _load_scenario() -> dict:
    import yaml
    with open(ROOT / "config" / "scenario.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def cmd_dry_run() -> int:
    scenario = _load_scenario()
    cfg = load_endpoints(ROOT / "config" / "endpoints.yaml")
    assert_safe_environment(cfg)  # §2.3 dry-runでもAPIを叩かないため無害だが、常に最初に通す

    rng = Rng(scenario["seed"])
    masters = generate_masters(rng)
    events, summary = generate_all_events(scenario, masters, rng)

    # 完了条件1: 日付順に並び、時間の巻き戻りがない
    for a, b in zip(events, events[1:]):
        assert a.date <= b.date, f"時系列逆転: {a} -> {b}"

    # 完了条件2: §5・§6の設計値と一致する(商談60・見積90・契約35・出荷69)
    expected = scenario["expected_totals"]["medium"]
    mismatches = [
        f"{k}: expected={expected[k]} actual={summary[k]}"
        for k in EXPECTED_TOTALS_KEYS if summary[k] != expected[k]
    ]

    run_id = datetime.now().strftime("run_%Y%m%d-%H%M%S")
    out_dir = ROOT / "out" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "events.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(asdict(ev), ensure_ascii=False, default=_json_default) + "\n")

    print(f"[dry-run] period {summary['period'][0]} .. {summary['period'][1]}")
    print(
        f"[dry-run] accounts={summary['accounts']}(+{summary['end_users']} end-users)  "
        f"products={summary['products']}(+{summary['unmapped_products']} unmapped)  "
        f"licenses={summary['licenses']}"
    )
    print(
        f"[dry-run] engagements={summary['engagements']}  quotes={summary['quotes']}  "
        f"contracts={summary['contracts']}  shipments={summary['shipments']}  "
        f"invoices={summary['invoices']}"
    )
    print(f"[dry-run] quadrants: {summary['quadrant_counts']}")
    print(
        f"[dry-run] exceptions: party_match={summary['party_match']} "
        f"party_possible={summary['party_possible_match']} "
        f"needs_review={summary['needs_review']} override={summary['override']} "
        f"override_unresolved={summary['override_unresolved']}"
    )
    print(
        f"          rejected={summary['formal_rejected']} "
        f"license_short={summary['license_shortage']} monitoring={summary['monitoring_hit']} "
        f"return={summary['return']} deemed_export={summary['deemed_export']} "
        f"unmapped={summary['unmapped_product_used']} erp_standalone={summary['erp_standalone_orders']} "
        f"lost_deals={summary['lost_deals']}"
    )
    print(f"[dry-run] total events: {summary['total_events']}")
    print(f"[dry-run] events written to {out_dir / 'events.jsonl'}")

    if mismatches:
        print("\nNG: §10.1完了条件を満たしていません。")
        for m in mismatches:
            print(f"  - {m}")
        return 1

    print("\n[dry-run] OK: P1完了条件(商談60・見積90・契約35・出荷69)を満たしました。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scale", choices=["smoke", "medium"])
    parser.add_argument("--resume")
    parser.add_argument("--verify-only")
    parser.add_argument("--cleanup")
    args = parser.parse_args()

    if args.dry_run:
        return cmd_dry_run()
    if args.scale or args.resume or args.verify_only or args.cleanup:
        print("未実装: P2(マスタ投入)・flows実装後に対応します。", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
