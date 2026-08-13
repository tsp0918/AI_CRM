"""§7.1 decays_at 算出ロジックのテスト。"""

from __future__ import annotations

from datetime import datetime, timezone

from crm_mvp.enums import Criterion
from crm_mvp.services.decay_policy import compute_decays_at


class TestHrCycleCriteria:
    def test_economic_buyer_before_april_targets_april_first(self):
        from_date = datetime(2026, 2, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.ECONOMIC_BUYER, from_date)
        assert result == datetime(2026, 4, 1, tzinfo=timezone.utc)

    def test_champion_between_april_and_october_targets_october_first(self):
        from_date = datetime(2026, 5, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.CHAMPION, from_date)
        assert result == datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_after_october_wraps_to_next_year_april(self):
        from_date = datetime(2026, 11, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.ECONOMIC_BUYER, from_date)
        assert result == datetime(2027, 4, 1, tzinfo=timezone.utc)

    def test_on_the_boundary_date_targets_next_cycle_not_itself(self):
        from_date = datetime(2026, 4, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.ECONOMIC_BUYER, from_date)
        assert result == datetime(2026, 10, 1, tzinfo=timezone.utc)


class TestFiscalYearCriteria:
    def test_budget_before_march_targets_same_year_end(self):
        from_date = datetime(2026, 1, 15, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.BUDGET, from_date)
        assert result == datetime(2026, 3, 31, tzinfo=timezone.utc)

    def test_budget_after_march_wraps_to_next_year(self):
        from_date = datetime(2026, 6, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.BUDGET, from_date)
        assert result == datetime(2027, 3, 31, tzinfo=timezone.utc)


class TestFixedDecayCriteria:
    def test_timing_decays_in_90_days(self):
        from_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.TIMING, from_date)
        assert (result - from_date).days == 90

    def test_unlisted_criterion_falls_back_to_default(self):
        from_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = compute_decays_at(Criterion.IDENTIFIED_PAIN, from_date)
        assert (result - from_date).days == 180

    def test_result_is_always_in_the_future(self):
        for criterion in Criterion:
            from_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
            result = compute_decays_at(criterion, from_date)
            assert result > from_date
