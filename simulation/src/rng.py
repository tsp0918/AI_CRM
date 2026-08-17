"""乱数(seed固定・再現性確保、docs/BULK_SIMULATION_SPEC.md §3.1)。"""

from __future__ import annotations

import random


class Rng:
    def __init__(self, seed: int):
        self._r = random.Random(seed)

    def weighted_choice(self, dist: dict) -> object:
        """`{key: weight}` から重み付きで1つ選ぶ(weightは正規化不要)。"""
        keys = list(dist.keys())
        weights = list(dist.values())
        return self._r.choices(keys, weights=weights, k=1)[0]

    def lognormal_amount(self, *, median: float, low: float, high: float) -> float:
        """中央値median、[low, high]の範囲に収まる程度のざっくりした対数正規分布。"""
        import math
        mu = math.log(median)
        sigma = 0.6
        value = self._r.lognormvariate(mu, sigma)
        return max(low, min(high, value))

    def normal_days(self, mean_days: float, *, min_days: int = 1) -> int:
        sigma = mean_days * 0.30
        value = self._r.normalvariate(mean_days, sigma)
        return max(min_days, round(value))

    def randint(self, a: int, b: int) -> int:
        return self._r.randint(a, b)

    def sample(self, population, k: int):
        return self._r.sample(population, k)

    def shuffle(self, seq: list) -> None:
        self._r.shuffle(seq)

    def choice(self, seq):
        return self._r.choice(seq)

    def random(self) -> float:
        return self._r.random()
