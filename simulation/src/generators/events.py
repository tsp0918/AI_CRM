"""業務イベント生成(docs/BULK_SIMULATION_SPEC.md §5・§6)。

P1(--dry-run)の完了条件は「生成イベント数が§5・§6の設計値と一致すること
(商談60・見積90・契約35・出荷69)」。件数がぴったり合う範囲は
`scenario.yaml`の`expected_totals`・`apportion_exact`で吸収し、日付・
取引先・金額など「形」の部分は各分布に従った乱数で決める。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..apportion import apportion_exact, largest_remainder
from ..rng import Rng
from .masters import MasterData, SimAccount


@dataclass
class Event:
    seq: int
    date: date
    kind: str
    sim_id: str
    payload: dict = field(default_factory=dict)


@dataclass
class SimEngagement:
    sim_id: str
    date: date
    quadrant: str
    account: SimAccount
    line_items: int
    amount: float
    currency: str
    destination: str
    sales_rep: str


_QUADRANT_ACCOUNT_POOL = {
    "existing_product_existing_customer": ["erp_domestic", "erp_overseas", "trading_company"],
    "existing_product_new_customer": ["crm_new"],
    "new_product_existing_customer": ["rnd"],
    "new_product_new_customer": ["rnd"],
}


class _Emitter:
    def __init__(self):
        self.events: list[Event] = []
        self._seq = 0

    def emit(self, d: date, kind: str, sim_id: str, **payload) -> Event:
        self._seq += 1
        ev = Event(seq=self._seq, date=d, kind=kind, sim_id=sim_id, payload=payload)
        self.events.append(ev)
        return ev


def _pick_account(masters: MasterData, quadrant: str, rng: Rng, cursors: dict[str, int]) -> SimAccount:
    pools = _QUADRANT_ACCOUNT_POOL[quadrant]
    pool_name = rng.choice(pools) if len(pools) > 1 else pools[0]
    candidates = [
        a for a in masters.accounts_by_category(pool_name)
        if not a.is_end_user_only and (pool_name != "trading_company" or a.end_user_of)
    ]
    i = cursors.get(pool_name, 0) % len(candidates)
    cursors[pool_name] = i + 1
    return candidates[i]


def generate_all_events(scenario: dict, masters: MasterData, rng: Rng) -> tuple[list[Event], dict]:
    """イベント一覧と、dry-runサマリ用の集計dictを返す。"""
    em = _Emitter()
    period = scenario["scale"]["medium"]["period"]
    year = date.fromisoformat(period["start"]).year
    lead = scenario["lead_times_days"]

    # --- §5.1 商談60件 ---
    monthly = scenario["engagement_monthly_distribution"]
    total_engagements = scenario["scale"]["medium"]["engagements"]
    assert sum(monthly) == total_engagements

    quadrant_counts = largest_remainder(scenario["quadrant_mix"], total_engagements)
    quadrant_list: list[str] = []
    for k, c in quadrant_counts.items():
        quadrant_list += [k] * c
    rng.shuffle(quadrant_list)

    engagements: list[SimEngagement] = []
    cursors: dict[str, int] = {}
    eng_seq = 1
    q_idx = 0
    for month_idx, count in enumerate(monthly, start=1):
        for _ in range(count):
            d = date(year, month_idx, rng.randint(1, 28))
            quadrant = quadrant_list[q_idx]
            q_idx += 1
            account = _pick_account(masters, quadrant, rng, cursors)
            line_items = int(rng.weighted_choice(scenario["line_items_per_engagement"]))
            amt_cfg = scenario["amount_jpy"]
            amount = rng.lognormal_amount(median=amt_cfg["median"], low=amt_cfg["min"], high=amt_cfg["max"])
            currency = rng.weighted_choice(scenario["currency_mix"])
            destination = rng.weighted_choice(scenario["destination_mix"])
            rep = rng.choice(scenario["sales_reps"])
            sim_id = f"SIM-ENG-{eng_seq:04d}"
            eng_seq += 1
            eng = SimEngagement(
                sim_id=sim_id, date=d, quadrant=quadrant, account=account,
                line_items=line_items, amount=amount, currency=currency,
                destination=destination, sales_rep=rep,
            )
            engagements.append(eng)
            em.emit(
                d, "engagement_created", sim_id, quadrant=quadrant,
                account=account.sim_id, currency=currency, amount=amount,
                destination=destination, sales_rep=rep,
                rnd_origin=quadrant.startswith("new_product"),
            )
    engagements.sort(key=lambda e: e.date)

    # --- §5.2 商談 → 見積(quote_reach_rate 0.75 → 45件が見積に到達) ---
    quote_bearing_n = round(total_engagements * scenario["funnel"]["quote_reach_rate"])
    quote_bearing = rng.sample(engagements, k=quote_bearing_n)
    quote_bearing_ids = {e.sim_id for e in quote_bearing}

    target_quotes = scenario["expected_totals"]["medium"]["quotes"]
    revision_values = sorted(int(k) for k in scenario["funnel"]["quote_revisions"])
    revision_props = [scenario["funnel"]["quote_revisions"][v] for v in revision_values]
    revision_counts = apportion_exact(quote_bearing_n, revision_values, revision_props, target_quotes)
    rng.shuffle(revision_counts)

    review_kinds = scenario["quote_revision_kinds"]
    review_kind_names = list(review_kinds.keys())
    review_kind_props = [review_kinds[k]["rate"] for k in review_kind_names]

    quotes: list[dict] = []
    review_triggering: list[dict] = []
    for eng, n_rev in zip(quote_bearing, revision_counts):
        base_date = eng.date + timedelta(days=rng.normal_days(lead["engagement_to_quote"]))
        prev_date = base_date
        for rev_i in range(n_rev):
            if rev_i == 0:
                d = base_date
                hash_changes = True  # 初回は必ず新規審査
                revision_kind = "initial"
            else:
                d = prev_date + timedelta(days=rng.normal_days(lead["quote_revision_interval"]))
                revision_kind = rng.weighted_choice(dict(zip(review_kind_names, review_kind_props)))
                hash_changes = review_kinds[revision_kind]["hash_changes"]
            prev_date = d
            quote_sim_id = f"{eng.sim_id}-Q{rev_i + 1}"
            kind = "quote_created" if rev_i == 0 else "quote_revised"
            em.emit(
                d, kind, quote_sim_id, engagement=eng.sim_id, revision=rev_i + 1,
                revision_kind=revision_kind, hash_changes=hash_changes,
            )
            record = {"sim_id": quote_sim_id, "engagement": eng.sim_id, "date": d, "hash_changes": hash_changes}
            quotes.append(record)
            if hash_changes:
                review_triggering.append(record)

    # --- §6.2 仮審査 needs_review(90件の見積に対して率0.20 → 18件) ---
    needs_review_n = round(target_quotes * scenario["exception_rates"]["provisional_needs_review"])
    needs_review = rng.sample(review_triggering, k=min(needs_review_n, len(review_triggering)))
    for r in needs_review:
        em.emit(r["date"], "provisional_needs_review", r["sim_id"], engagement=r["engagement"])
    override_n = round(len(needs_review) * scenario["exception_rates"]["override_success"])
    overridden = rng.sample(needs_review, k=override_n)
    for r in overridden:
        em.emit(
            r["date"] + timedelta(days=1), "override_applied", r["sim_id"], engagement=r["engagement"],
            reason_code="end_user_certificate_obtained", approval_level="department_head",
        )
    unresolved = [r for r in needs_review if r not in overridden]
    for r in unresolved:
        em.emit(r["date"] + timedelta(days=2), "review_unresolved", r["sim_id"], engagement=r["engagement"])

    # --- §5.3 見積 → 契約(win_rate 0.58 → 60件基準で丸め35件を契約化) ---
    target_contracts = scenario["expected_totals"]["medium"]["contracts"]
    contract_engagements = rng.sample(quote_bearing, k=target_contracts)
    contract_ids = {e.sim_id for e in contract_engagements}

    rejected_pool = rng.sample(contract_engagements, k=scenario["exception_rates"]["formal_rejected_count"])
    rejected_ids = {e.sim_id for e in rejected_pool}
    license_shortage_pool = rng.sample(
        contract_engagements, k=scenario["exception_rates"]["license_shortage_count"]
    )
    license_shortage_ids = {e.sim_id for e in license_shortage_pool}

    contracts: list[dict] = []
    for eng in contract_engagements:
        eng_quotes = [q for q in quotes if q["engagement"] == eng.sim_id]
        last_quote_date = max(q["date"] for q in eng_quotes)
        d = last_quote_date + timedelta(days=rng.normal_days(lead["quote_to_contract"]))
        contract_sim_id = f"{eng.sim_id}-C1"
        rejected = eng.sim_id in rejected_ids
        shortage = eng.sim_id in license_shortage_ids
        em.emit(
            d, "contract_created", contract_sim_id, engagement=eng.sim_id,
            formal_rejected=rejected, license_shortage=shortage,
        )
        contracts.append({"sim_id": contract_sim_id, "engagement": eng.sim_id, "date": d, "rejected": rejected})

    lost_engagements = [e for e in engagements if e.sim_id not in contract_ids]
    for eng in lost_engagements:
        had_review = eng.sim_id in quote_bearing_ids
        d = eng.date + timedelta(days=rng.normal_days(lead["engagement_to_quote"] + lead["quote_to_contract"]))
        em.emit(d, "lost_deal", eng.sim_id, review_withdrawn=had_review)

    # --- §5.4 契約 → 出荷 → 請求(split_shipments、35契約に対し合計69件) ---
    # rejectedの契約は法務確認待ちで出荷に進まないため、出荷可能な契約数を
    # 母数にして配分する(そうしないと合計が69に届かない)。
    shippable_contracts = [c for c in contracts if not c["rejected"]]
    ship_values = sorted(int(k) for k in scenario["funnel"]["split_shipments"])
    ship_props = [scenario["funnel"]["split_shipments"][v] for v in ship_values]
    target_shipments = scenario["expected_totals"]["medium"]["shipments"]
    split_counts = apportion_exact(len(shippable_contracts), ship_values, ship_props, target_shipments)
    rng.shuffle(split_counts)

    shipments: list[dict] = []
    for contract, n_ship in zip(shippable_contracts, split_counts):
        first = contract["date"] + timedelta(days=rng.normal_days(lead["contract_to_first_shipment"]))
        d = first
        for i in range(n_ship):
            if i > 0:
                d = d + timedelta(days=rng.normal_days(lead["shipment_interval"]))
            ship_sim_id = f"{contract['sim_id']}-S{i + 1}"
            em.emit(d, "shipment", ship_sim_id, contract=contract["sim_id"], split_index=i + 1, split_total=n_ship)
            shipments.append({"sim_id": ship_sim_id, "contract": contract["sim_id"], "date": d})
            inv_date = d + timedelta(days=rng.normal_days(lead["shipment_to_invoice"]))
            em.emit(inv_date, "invoice", f"{ship_sim_id}-INV", shipment=ship_sim_id)

    # --- §5.5 期中イベント ---
    monitoring_dates = [date(year, 7, 15), date(year, 10, 15)]
    monitoring_targets = rng.sample(contracts, k=min(scenario["exception_rates"]["monitoring_hit_count"], len(contracts)))
    for d, target in zip(monitoring_dates, monitoring_targets):
        em.emit(d, "monitoring_added", "SIM-WATCHLIST-ZETA", company_name="SIM Monitoring Zeta Corp")
        em.emit(d, "monitoring_hit", target["sim_id"], contract=target["sim_id"])

    deemed_export_n = scenario["exception_rates"]["deemed_export_count"]
    deemed_targets = rng.sample(engagements, k=deemed_export_n)
    for eng in deemed_targets:
        d = eng.date + timedelta(days=rng.normal_days(lead["engagement_to_quote"] // 2, min_days=1))
        em.emit(d, "deemed_export", eng.sim_id, engagement=eng.sim_id)

    return_n = scenario["exception_rates"]["return_count"]
    return_targets = rng.sample(shipments, k=min(return_n, len(shipments)))
    for s in return_targets:
        d = s["date"] + timedelta(days=rng.normal_days(45, min_days=30))
        em.emit(d, "return", f"{s['sim_id']}-RET", shipment=s["sim_id"])

    unmapped_products = [p for p in masters.products if p.erp_material_code is None]
    unmapped_n = scenario["exception_rates"]["unmapped_product_count"]
    unmapped_targets = rng.sample(quotes, k=min(unmapped_n, len(quotes)))
    for q, product in zip(unmapped_targets, unmapped_products):
        em.emit(q["date"], "unmapped_product_used", q["sim_id"], product=product.sim_id)

    quarterly_months = [1, 4, 7, 10]
    erp_accounts = masters.accounts_by_category("erp_domestic") + masters.accounts_by_category("erp_overseas")
    for i, m in enumerate(quarterly_months[: scenario["exception_rates"]["erp_standalone_order_count"]]):
        account = erp_accounts[i % len(erp_accounts)]
        d = date(year, m, 20)
        em.emit(d, "erp_standalone_order", f"SIM-ERP-ORDER-{i + 1:02d}", account=account.sim_id)

    party_match_n = round(25 * scenario["exception_rates"]["party_match"])
    party_possible_n = round(25 * scenario["exception_rates"]["party_possible_match"])
    for entry in [w for w in masters.watchlist if w.kind == "match"][:party_match_n]:
        em.emit(engagements[0].date, "party_screening_match", entry.account_sim_id or "unknown", company_name=entry.company_name)
    for entry in [w for w in masters.watchlist if w.kind == "possible_match"][:party_possible_n]:
        em.emit(engagements[0].date, "party_screening_possible_match", entry.account_sim_id or "unknown", company_name=entry.company_name)

    em.events.sort(key=lambda e: (e.date, e.seq))

    summary = {
        "period": (period["start"], period["end"]),
        "accounts": len(masters.accounts) - sum(1 for a in masters.accounts if a.is_end_user_only),
        "end_users": sum(1 for a in masters.accounts if a.is_end_user_only),
        "products": len(masters.products),
        "unmapped_products": len(unmapped_products),
        "licenses": len(masters.licenses),
        "engagements": len(engagements),
        "quadrant_counts": quadrant_counts,
        "quotes": len(quotes),
        "contracts": len(contracts),
        "shipments": len(shipments),
        "invoices": len(shipments),
        "needs_review": len(needs_review),
        "override": len(overridden),
        "override_unresolved": len(unresolved),
        "formal_rejected": len(rejected_pool),
        "license_shortage": len(license_shortage_pool),
        "monitoring_hit": len(monitoring_targets),
        "return": len(return_targets),
        "deemed_export": len(deemed_targets),
        "unmapped_product_used": min(unmapped_n, len(quotes)),
        "erp_standalone_orders": min(scenario["exception_rates"]["erp_standalone_order_count"], len(quarterly_months)),
        "party_match": party_match_n,
        "party_possible_match": party_possible_n,
        "lost_deals": len(lost_engagements),
        "total_events": len(em.events),
    }
    return em.events, summary
