"""比率どおりの近似配分をしつつ、合計を厳密に一致させるためのヘルパー。

docs/BULK_SIMULATION_SPEC.md §10.1 の完了条件は「生成されたイベント数が
§5・§6の設計値と一致する(商談60・見積90・契約35・出荷69)」ことを要求する。
`funnel.quote_reach_rate`・`funnel.quote_revisions` から素直に期待値計算す
ると見積総数は 86〜87 程度になり、90 に対して数件の差が生じる(内訳分布の
「形」は仕様書の比率に従わせつつ、合計だけは §10.1 の設計値に厳密採用する
必要があるため)。`largest_remainder` で比率どおりの近似配分をしたうえで、
`apportion_exact` が過不足分を隣接する値の階層(1件→2件など)へ1件ずつ
動かして合計を目標値にちょうど合わせる。
"""

from __future__ import annotations


def largest_remainder(proportions: dict, total: int) -> dict:
    """比例配分の議席配分法(最大剰余法)。`{key: weight}` → `{key: count}`、
    counts の合計は必ず `total` に一致する。"""
    weight_sum = sum(proportions.values())
    raw = {k: total * w / weight_sum for k, w in proportions.items()}
    floors = {k: int(v) for k, v in raw.items()}
    remainder = total - sum(floors.values())
    order = sorted(raw.keys(), key=lambda k: raw[k] - floors[k], reverse=True)
    for k in order[:remainder]:
        floors[k] += 1
    return floors


def apportion_exact(n_items: int, values: list[int], proportions: list[float], target_sum: int) -> list[int]:
    """`n_items` 個それぞれに `values` のいずれか(連番の整数、例[1,2,3,4])を
    割り当て、割当個数の合計が `n_items`、値の合計が `target_sum` に厳密に
    一致するようにする。`proportions` は各値の目安比率(概形を決めるだけで、
    最終的な端数調整で崩れうる)。"""
    assert values == sorted(values) and all(
        values[i + 1] - values[i] == 1 for i in range(len(values) - 1)
    ), "values は連番の整数である必要がある(1件ずつの調整で合計を厳密一致させるため)"

    dist = dict(zip(values, proportions))
    counts = largest_remainder(dist, n_items)  # {value: 件数}, sum(counts)=n_items

    current = sum(v * c for v, c in counts.items())
    diff = target_sum - current

    while diff > 0:
        # 最も低い値の階層から1件を1つ上の階層へ動かす(+1)
        v = next(v for v in values[:-1] if counts[v] > 0)
        counts[v] -= 1
        counts[v + 1] += 1
        diff -= 1
    while diff < 0:
        # 最も高い値の階層から1件を1つ下の階層へ動かす(-1)
        v = next(v for v in reversed(values[1:]) if counts[v] > 0)
        counts[v] -= 1
        counts[v - 1] += 1
        diff += 1

    result: list[int] = []
    for v in values:
        result.extend([v] * counts[v])
    return result
