"""マスタデータ生成(docs/BULK_SIMULATION_SPEC.md §4)。

P1(--dry-run)ではメモリ上に生成するだけで、どのシステムにも投入しない。
P2で `simulation/src/clients/*` を介して実際にCRM/ERP/AI_TMへPOSTする際に、
この生成結果をそのまま入力として使う想定。すべての識別子に `SIM-`
プレフィックスを付け、クリーンアップ時に確実に絞り込めるようにする(§4冒頭)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..rng import Rng

# §4.4 テスト用ウォッチリスト。実在の掲載企業名を絶対に使わない。
WATCHLIST_MATCH_1 = "SIM Semiconductor Alpha Corp"
WATCHLIST_MATCH_2_END_USER = "SIM Precision Beta Industries"
WATCHLIST_POSSIBLE = [
    "SIM Materials Gamma Ltd",
    "SIM Components Delta GmbH",
    "SIM Systems Epsilon Pte",
]
WATCHLIST_MONITORING = "SIM Monitoring Zeta Corp"  # 初期clear→期中に追加


@dataclass
class SimAccount:
    sim_id: str
    legal_name: str
    category: str  # erp_domestic | erp_overseas | crm_new | trading_company | rnd
    country: str
    external_id: str | None = None  # ERP bp_code(ERP既存のみ)
    is_end_user_only: bool = False
    end_user_of: str | None = None  # 商社の場合、対応するエンドユーザーのsim_id


@dataclass
class SimProduct:
    sim_id: str
    name: str
    eccn: str
    classification: str  # NOT_APPLICABLE | APPLICABLE | UNKNOWN
    license_required: bool
    erp_material_code: str | None  # Noneの2件が品目マッピング未設定の再現用


@dataclass
class SimLicense:
    sim_id: str
    product_sim_id: str
    destinations: list[str]
    quantity: int
    expires_on: str | None = None  # ISO date、Noneなら期限なし


@dataclass
class WatchlistEntry:
    company_name: str
    kind: str  # match | possible_match | monitoring
    account_sim_id: str | None = None  # kind=monitoring は期中に追加するためNone可


@dataclass
class MasterData:
    accounts: list[SimAccount] = field(default_factory=list)
    products: list[SimProduct] = field(default_factory=list)
    licenses: list[SimLicense] = field(default_factory=list)
    watchlist: list[WatchlistEntry] = field(default_factory=list)

    def accounts_by_category(self, category: str) -> list[SimAccount]:
        return [a for a in self.accounts if a.category == category]


def _account(n: int) -> str:
    return f"SIM-ACC-{n:03d}"


def _product(n: int) -> str:
    return f"SIM-PROD-{n:02d}"


def _license(n: int) -> str:
    return f"SIM-LIC-{n:02d}"


def generate_masters(rng: Rng) -> MasterData:
    accounts: list[SimAccount] = []
    seq = 1

    # #1 ERP既存・国内 8社(うち1社はGamma=possible_match、1社はZeta=監視対象)
    domestic_names = [WATCHLIST_POSSIBLE[0], WATCHLIST_MONITORING] + [
        f"SIM Trading {i:02d} Co., Ltd." for i in range(1, 7)
    ]
    for name in domestic_names:
        accounts.append(SimAccount(
            sim_id=_account(seq), legal_name=name, category="erp_domestic",
            country="JP", external_id=f"SIM-BP-{seq:07d}",
        ))
        seq += 1

    # #2 ERP既存・海外 5社(KR/TW/US/DE/SG、うち1社はDelta=possible_match)
    overseas_countries = ["KR", "TW", "US", "DE", "SG"]
    overseas_names = [WATCHLIST_POSSIBLE[1]] + [
        f"SIM Overseas {c} Trading Ltd." for c in overseas_countries[1:]
    ]
    for country, name in zip(overseas_countries, overseas_names):
        accounts.append(SimAccount(
            sim_id=_account(seq), legal_name=name, category="erp_overseas",
            country=country, external_id=f"SIM-BP-{seq:07d}",
        ))
        seq += 1

    # #3 CRM発生(ERP未登録) 7社(うち1社はAlpha=match、1社はEpsilon=possible_match)
    crm_new_names = [WATCHLIST_MATCH_1, WATCHLIST_POSSIBLE[2]] + [
        f"SIM New Customer {i:02d} K.K." for i in range(1, 6)
    ]
    for name in crm_new_names:
        accounts.append(SimAccount(
            sim_id=_account(seq), legal_name=name, category="crm_new",
            country="JP", external_id=None,
        ))
        seq += 1

    # #4 商社(エンドユーザーが別) 3社。エンドユーザーはCN/TW。
    # 制裁ヒット2社のうち1社は必ず商社のエンドユーザー側に配置(§4.1)。
    trading_end_user_countries = ["CN", "TW", "CN"]
    trading_end_user_names = [
        WATCHLIST_MATCH_2_END_USER,  # 1社目のエンドユーザーがmatch
        "SIM End User Two Manufacturing",
        "SIM End User Three Manufacturing",
    ]
    for i, (eu_country, eu_name) in enumerate(zip(trading_end_user_countries, trading_end_user_names)):
        trader = SimAccount(
            sim_id=_account(seq), legal_name=f"SIM Trading House {i + 1:02d} Corp.",
            category="trading_company", country="JP", external_id=None,
        )
        seq += 1
        end_user = SimAccount(
            sim_id=_account(seq), legal_name=eu_name, category="trading_company",
            country=eu_country, external_id=None, is_end_user_only=True,
        )
        seq += 1
        trader.end_user_of = end_user.sim_id
        accounts.append(trader)
        accounts.append(end_user)

    # #5 R&D起点 2社
    for i in range(1, 3):
        accounts.append(SimAccount(
            sim_id=_account(seq), legal_name=f"SIM R&D Origin {i:02d} Inc.",
            category="rnd", country="JP", external_id=None,
        ))
        seq += 1

    # products (§4.2): EAR99 6 / 3C001 4 / 3B001 2 / 1C350 1 / 未判定(R&D) 2
    products: list[SimProduct] = []
    pn = 1

    def add_products(count: int, eccn: str, classification: str, license_required: bool, unmapped_slots: set[int]):
        nonlocal pn
        for _ in range(count):
            products.append(SimProduct(
                sim_id=_product(pn), name=f"SIM Product {eccn}-{pn:02d}", eccn=eccn,
                classification=classification, license_required=license_required,
                erp_material_code=None if pn in unmapped_slots else f"SIM-MAT-{pn:04d}",
            ))
            pn += 1

    # 品目マッピング未設定の2件(§6.2)はEAR99枠に配置する(通常フローの主流に
    # 混ぜることで「未マッピングでもゲートがフェイルクローズすること」を
    # 通常商談の中で自然に踏ませる)。
    add_products(6, "EAR99", "NOT_APPLICABLE", False, unmapped_slots={1, 2})
    add_products(4, "3C001", "APPLICABLE", True, unmapped_slots=set())
    add_products(2, "3B001", "APPLICABLE", True, unmapped_slots=set())
    add_products(1, "1C350", "APPLICABLE", True, unmapped_slots=set())
    add_products(2, "UNKNOWN", "UNKNOWN", False, unmapped_slots=set())

    # licenses (§4.3)
    licenses = [
        SimLicense(sim_id=_license(1), product_sim_id=_product(7), destinations=["KR", "TW"], quantity=5000),
        SimLicense(sim_id=_license(2), product_sim_id=_product(7), destinations=["CN"], quantity=300),
        SimLicense(sim_id=_license(3), product_sim_id=_product(11), destinations=["KR"], quantity=2000),
        SimLicense(sim_id=_license(4), product_sim_id=_product(13), destinations=["TW"], quantity=1000, expires_on="2026-09-30"),
    ]

    watchlist = [
        WatchlistEntry(WATCHLIST_MATCH_1, "match", account_sim_id=_account_id_by_name(accounts, WATCHLIST_MATCH_1)),
        WatchlistEntry(WATCHLIST_MATCH_2_END_USER, "match", account_sim_id=_account_id_by_name(accounts, WATCHLIST_MATCH_2_END_USER)),
        *[
            WatchlistEntry(name, "possible_match", account_sim_id=_account_id_by_name(accounts, name))
            for name in WATCHLIST_POSSIBLE
        ],
        WatchlistEntry(WATCHLIST_MONITORING, "monitoring", account_sim_id=_account_id_by_name(accounts, WATCHLIST_MONITORING)),
    ]

    return MasterData(accounts=accounts, products=products, licenses=licenses, watchlist=watchlist)


def _account_id_by_name(accounts: list[SimAccount], name: str) -> str | None:
    for a in accounts:
        if a.legal_name == name:
            return a.sim_id
    return None
