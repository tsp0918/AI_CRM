"""§7.1: 証拠の失効基準(decays_at)を criterion ごとに算出する。

HANDOVER.md §7.1 暫定案を正式採用(2026-08-13 ユーザー決定):
  - economic_buyer / champion: 人事異動基準(次の4月・10月)
    → 決裁者・チャンピオンは異動で交代しうる情報のため
  - budget: 予算期基準(次の決算期末)
    → 予算は決算期をまたぐと再確認が必要になるため
  - その他: 固定日数
    → 鮮度が落ちやすいもの(timing, competition)ほど短く設定する

決算期末は3月末を既定とする(テナント別の会計年度設定は将来課題)。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..enums import Criterion

HR_CYCLE_MONTHS: tuple[int, int] = (4, 10)
FISCAL_YEAR_END_MONTH = 3  # 3月末決算を既定と仮定

HR_CYCLE_CRITERIA: frozenset[Criterion] = frozenset({
    Criterion.ECONOMIC_BUYER, Criterion.CHAMPION,
})
FISCAL_YEAR_CRITERIA: frozenset[Criterion] = frozenset({Criterion.BUDGET})

# 鮮度が落ちやすいものほど短い日数にする。
FIXED_DECAY_DAYS: dict[Criterion, int] = {
    Criterion.TIMING: 90,
    Criterion.COMPETITION: 120,
    Criterion.IDENTIFIED_PAIN: 180,
    Criterion.METRICS: 180,
    Criterion.DECISION_CRITERIA: 180,
    Criterion.DECISION_PROCESS: 180,
    Criterion.PAPER_PROCESS: 180,
}
DEFAULT_FIXED_DECAY_DAYS = 180


def _next_hr_cycle_date(from_date: date) -> date:
    """from_date より後の直近の 4/1 または 10/1 を返す。"""
    candidates = [
        date(year, month, 1)
        for year in (from_date.year, from_date.year + 1)
        for month in HR_CYCLE_MONTHS
    ]
    return min(c for c in candidates if c > from_date)


def _next_fiscal_year_end(from_date: date) -> date:
    """from_date より後の直近の決算期末日を返す(既定: 3月末)。"""
    candidate = date(from_date.year, FISCAL_YEAR_END_MONTH, 31)
    if candidate <= from_date:
        candidate = date(from_date.year + 1, FISCAL_YEAR_END_MONTH, 31)
    return candidate


def compute_decays_at(
    criterion: Criterion, from_date: datetime | None = None,
) -> datetime:
    """criterion に応じた失効日時を返す。常に from_date より未来になる。"""
    now = from_date or datetime.now(timezone.utc)
    today = now.date()

    if criterion in HR_CYCLE_CRITERIA:
        target = _next_hr_cycle_date(today)
        return datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)

    if criterion in FISCAL_YEAR_CRITERIA:
        target = _next_fiscal_year_end(today)
        return datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)

    days = FIXED_DECAY_DAYS.get(criterion, DEFAULT_FIXED_DECAY_DAYS)
    return now + timedelta(days=days)
