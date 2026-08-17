"""仮想時計(docs/BULK_SIMULATION_SPEC.md §3.4)。業務日付を進める。実時間とは切り離す。"""

from __future__ import annotations

from datetime import date, timedelta


class VirtualClock:
    def __init__(self, start: date, end: date):
        self.today = start
        self.end = end

    def advance_to(self, d: date) -> None:
        assert d >= self.today, "時間を巻き戻してはいけない"
        self.today = min(d, self.end)

    def business_days_from(self, d: date, n: int) -> date:
        """土日を除いて n 営業日後。"""
        cur = d
        remaining = n
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        while remaining > 0:
            cur += timedelta(days=step)
            if cur.weekday() < 5:  # Mon-Fri
                remaining -= 1
        return cur
