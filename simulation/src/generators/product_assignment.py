"""P4実行時の品目割当。

P1の`generate_all_events`はdry-run完了条件(イベント総数)の検証にしか
使わないため、明細の件数(1〜3件)は生成するが実際の品目コードまでは
割り当てていない。P4(実行)ではここで初めて具体的な品目を選ぶ。

§6.1の象限に応じて母集団を分ける:
  existing_product系 → EAR99/3C001/3B001/1C350(意図的な未マッピング2件を除く)
  new_product系(R&D起点) → UNKNOWN 2件
"""

from __future__ import annotations

from ..rng import Rng

# masters.pyのproduct生成順(sim_id)に対応。P2実行時のgenerate_masters()と
# 同じseedなので、この並びは決定的に再現される。
EAR99_MAPPED = ["SIM-PROD-03", "SIM-PROD-04", "SIM-PROD-05", "SIM-PROD-06"]
EAR99_UNMAPPED = ["SIM-PROD-01", "SIM-PROD-02"]
LICENSE_3C001 = ["SIM-PROD-07", "SIM-PROD-08", "SIM-PROD-09", "SIM-PROD-10"]
LICENSE_3B001 = ["SIM-PROD-11", "SIM-PROD-12"]
LICENSE_1C350 = ["SIM-PROD-13"]
UNKNOWN_RND = ["SIM-PROD-14", "SIM-PROD-15"]

EXISTING_PRODUCT_POOL = EAR99_MAPPED + LICENSE_3C001 + LICENSE_3B001 + LICENSE_1C350


def pick_line_items(
    rng: Rng, quadrant: str, n_items: int, *,
    force_unmapped: bool = False, force_license_shortage: bool = False,
) -> list[tuple[str, float]]:
    """`[(product_sim_id, quantity), ...]`を返す。"""
    if quadrant.startswith("new_product"):
        pool = UNKNOWN_RND
    else:
        pool = list(EXISTING_PRODUCT_POOL)

    picks: list[str] = []
    if force_unmapped:
        picks.append(rng.choice(EAR99_UNMAPPED))
        n_items = max(0, n_items - 1)
    if force_license_shortage:
        picks.append(rng.choice(LICENSE_3C001))
        n_items = max(0, n_items - 1)

    for _ in range(n_items):
        picks.append(rng.choice(pool))

    quantities = [float(rng.randint(1, 20)) for _ in picks]
    # 強制品目(unmapped/license_shortage)は先頭に積んであるので、その
    # インデックスだけ数量を上書きする。
    offset = 0
    if force_unmapped:
        offset += 1
    if force_license_shortage:
        # SIM-LIC-02(CN, 300枠)を明確に超える数量にする
        # (§6.2「ライセンス枠不足」を確実に再現するため)。
        quantities[offset] = 500.0

    return list(zip(picks, quantities))
