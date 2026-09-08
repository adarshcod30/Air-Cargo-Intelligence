"""Tests for trend, anomaly and forecast maths.

These are the numbers the language model will be handed to narrate, so
the grounding guarantee rests entirely on them being right here.
"""

import numpy as np
import pytest

from services.analytics.anomaly import detect, detect_statistical, robust_z_scores
from services.analytics.forecast import (
    _next_label,
    forecast,
    mape,
    rolling_origin_backtest,
    seasonal_naive,
)
from services.analytics.trend import cagr, compute_trend, pct_change, seasonal_decompose


class TestGrowthMaths:
    def test_percentage_change(self):
        assert pct_change(110, 100) == 10.0
        assert pct_change(90, 100) == -10.0

    def test_zero_base_refuses(self):
        """An airport with no cargo last year is common here, and
        'infinite growth' is not a publishable number."""
        assert pct_change(50, 0) is None

    def test_cagr(self):
        assert cagr(100, 200, 3) == pytest.approx(25.99, abs=0.01)

    def test_cagr_refuses_zero_or_negative_base(self):
        assert cagr(0, 100, 3) is None
        assert cagr(-5, 100, 3) is None


class TestTrendSeries:
    def test_year_lag_differs_by_period_kind(self):
        """A fiscal series steps one year at a time; a monthly series
        needs a 12-step lag. Using 12 on annual data yields no YoY at
        all, which looks like missing data rather than a bug."""
        fy = compute_trend(["2015-FY", "2016-FY"], [201506, 201606], [100.0, 120.0])
        assert fy[1].yoy_pct == 20.0

        months = [f"2024-{i:02d}" for i in range(1, 13)] + ["2025-01"]
        keys = [202400 + i for i in range(1, 13)] + [202501]
        vals = [100.0] * 12 + [150.0]
        m = compute_trend(months, keys, vals)
        assert m[12].yoy_pct == 50.0
        assert m[5].yoy_pct is None       # not yet a year of history

    def test_series_is_sorted_before_computing(self):
        pts = compute_trend(["2024-02", "2024-01"], [202402, 202401], [120.0, 100.0])
        assert [p.period for p in pts] == ["2024-01", "2024-02"]
        assert pts[1].mom_pct == 20.0

    def test_share_of_total(self):
        pts = compute_trend(["2024-01"], [202401], [25.0], totals=[100.0])
        assert pts[0].share_of_total == 25.0

    def test_stl_refuses_short_series(self):
        """STL needs two full cycles; forcing it on less produces
        components that look meaningful and are not."""
        assert seasonal_decompose([1.0] * 10, period=12) is None

    def test_stl_refuses_flat_series(self):
        assert seasonal_decompose([5.0] * 30, period=12) is None


class TestAnomalyDetection:
    def test_robust_to_the_outlier_it_is_looking_for(self):
        """Mean and standard deviation are dragged by the outlier itself,
        masking exactly the point that matters. Median and MAD are not."""
        vals = [100, 102, 98, 101, 99, 103, 100, 102, 500, 101, 99, 100]
        z = robust_z_scores(vals)
        assert abs(z[8]) > 10
        assert all(abs(x) < 5 for i, x in enumerate(z) if i != 8)

    def test_spike_is_flagged(self):
        """Values are kilograms; the baseline must be a real volume."""
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        base = [100, 102, 98, 101, 99, 103, 100, 102, 500, 101, 99, 100]
        vals = [v * 1000.0 for v in base]
        found = detect_statistical(per, vals)
        assert [a.period for a in found] == ["2024-09"]
        assert found[0].severity == "HIGH"

    def test_mostly_zero_series_is_not_scored(self):
        """A series that is mostly zeros has a median near zero, so every
        real value looks like a wild outlier. SpiceJet's international
        cargo was reported as "+29,886% versus an expected 14 tonnes"
        when the airline had simply started flying those routes. That is
        a structural break, not an anomaly."""
        per = [f"{2010 + i}-FY" for i in range(10)]
        vals = [0.0] * 6 + [1_500_000.0, 2_800_000.0, 3_300_000.0, 4_200_000.0]
        assert detect_statistical(per, vals) == []

    def test_low_baseline_series_is_not_scored(self):
        """Percentage swings around a tiny baseline are not newsworthy
        even when every point is non-zero."""
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        vals = [50.0, 60, 55, 52, 58, 51, 57, 54, 900, 53, 56, 55]
        assert detect_statistical(per, vals) == []

    def test_a_real_series_with_a_genuine_spike_still_fires(self):
        """The guards must not silence the case they exist to protect."""
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        vals = [1_000_000.0] * 8 + [4_000_000.0] + [1_000_000.0] * 3
        found = detect_statistical(per, vals)
        assert [a.period for a in found] == ["2024-09"]

    def test_flat_series_produces_nothing(self):
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        assert detect_statistical(per, [100.0] * 12, min_kg=1) == []

    def test_tiny_volumes_are_ignored(self):
        """2kg to 20kg is a 900% rise and worth nobody's attention."""
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        vals = [1.0, 1, 1, 1, 1, 1, 1, 1, 50, 1, 1, 1]
        assert detect_statistical(per, vals) == []

    def test_short_series_produces_nothing(self):
        assert detect_statistical(["2024-01", "2024-02"], [1000.0, 9000.0]) == []

    def test_consensus_between_methods_raises_severity(self):
        rng = np.random.default_rng(0)
        vals = [(100 + 20 * np.sin(i / 12 * 2 * np.pi) + rng.normal(0, 1)) * 1000
                for i in range(48)]
        vals[30] = 400_000.0
        per = [f"{2020 + i // 12}-{i % 12 + 1:02d}" for i in range(48)]
        found = {a.period: a for a in detect(per, vals)}
        assert per[30] in found
        assert found[per[30]].severity in ("MEDIUM", "HIGH")


class TestForecasting:
    def test_mape_skips_zero_actuals(self):
        assert mape([100, 0, 200], [110, 5, 180]) == 10.0

    def test_seasonal_naive_repeats_last_cycle(self):
        vals = list(range(24))
        assert seasonal_naive(vals, 12, 3) == [12.0, 13.0, 14.0]

    def test_period_labels_advance_by_kind(self):
        assert _next_label("2026-11", 3) == "2027-02"
        assert _next_label("2015-FY", 1) == "2016-FY"
        assert _next_label("2023-A", 2) == "2025-A"

    def test_backtest_never_sees_its_own_future(self):
        """Rolling-origin: each fold trains only on earlier points. A
        random split would leak and inflate the score."""
        vals = [100 + i for i in range(30)]
        scores = rolling_origin_backtest(vals, season=12, folds=3)
        assert "seasonal_naive" in scores

    def test_short_series_are_not_forecast(self):
        assert forecast(["2024-01", "2024-02"], [1.0, 2.0]) == []

    def test_forecast_carries_an_interval_and_a_score(self):
        vals = [100 + 10 * np.sin(i / 2) + i for i in range(30)]
        per = [f"{2022 + i // 12}-{i % 12 + 1:02d}" for i in range(30)]
        out = forecast(per, vals, horizon=3)
        assert len(out) == 3
        for p in out:
            assert p.lower_kg is not None and p.upper_kg is not None
            assert p.lower_kg <= p.predicted_kg <= p.upper_kg
            assert p.backtest_mape is not None

    def test_forecasts_are_never_negative(self):
        """Negative tonnage is not a physical quantity."""
        vals = [50.0, 40, 30, 20, 10, 5, 2, 1, 1, 1, 1, 1]
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        for p in forecast(per, vals, horizon=3):
            assert p.predicted_kg >= 0 and p.lower_kg >= 0

    def test_annual_series_use_no_month_season(self):
        vals = [100.0 + 10 * i for i in range(10)]
        per = [f"{2010 + i}-FY" for i in range(10)]
        out = forecast(per, vals, horizon=2)
        assert out and out[0].period_label == "2020-FY"


class TestYearOnYearFromPublishedPriorYear:
    """AAI prints the prior-year figure beside each month, and exposes
    only a handful of recent months. Deriving a twelve-month lag would
    leave year-on-year growth permanently null even though the number is
    printed on the page."""

    def test_prior_year_column_is_used_when_present(self):
        pts = compute_trend(
            ["2026-04"], [202604], [110.0], prior_year=[100.0]
        )
        assert pts[0].yoy_pct == 10.0

    def test_falls_back_to_the_lag_when_absent(self):
        months = [f"2024-{i:02d}" for i in range(1, 13)] + ["2025-01"]
        keys = [202400 + i for i in range(1, 13)] + [202501]
        vals = [100.0] * 12 + [150.0]
        pts = compute_trend(months, keys, vals, prior_year=[None] * 13)
        assert pts[12].yoy_pct == 50.0

    def test_zero_prior_year_does_not_divide(self):
        pts = compute_trend(["2026-04"], [202604], [110.0], prior_year=[0.0])
        assert pts[0].yoy_pct is None


class TestAnomalyFalsePositiveGuards:
    """Every case here is a real series that produced a false alert.

    The metric that decides whether an alert feed is read is the false
    positive rate, not recall. A feed that cries wolf gets switched off,
    and then the real anomalies go unseen too.
    """

    @staticmethod
    def _kg(vals):
        return [v * 1000.0 for v in vals]

    def test_launch_curve_is_not_an_anomaly(self):
        """Spicejet international: an airline starting those routes. Its
        median is 14 tonnes against a peak of 6,506, so every later year
        scored as a wild outlier against its own pre-launch history."""
        vals = self._kg([0, 0, 0, 0, 0, 0, 9, 14.2, 1495.8, 275.2, 3363.7,
                         3364.5, 2874.8, 3125.8, 4258, 4258, 6505.9])
        per = [f"{2007 + i}-FY" for i in range(len(vals))]
        assert detect_statistical(per, vals) == []

    def test_series_with_a_gap_then_growth_is_not_an_anomaly(self):
        """Go Air domestic: operations, a gap, then a much larger scale."""
        vals = self._kg([2971, 2971, 1595, 1595, 0, 0, 0, 0, 0, 0, 24485,
                         24485, 43857, 43857, 55410, 55410, 56871, 56871, 5729])
        per = [f"{2005 + i}-FY" for i in range(len(vals))]
        assert detect_statistical(per, vals) == []

    def test_steady_growth_is_not_an_anomaly(self):
        vals = self._kg([100.0 * i for i in range(1, 15)])
        per = [f"{2010 + i}-FY" for i in range(len(vals))]
        assert detect_statistical(per, vals) == []

    def test_a_real_spike_in_a_stable_series_still_fires(self):
        """The guards must not silence what they exist to protect."""
        vals = self._kg([1000.0] * 8 + [4000.0] + [1000.0] * 3)
        per = [f"2024-{i:02d}" for i in range(1, 13)]
        found = detect_statistical(per, vals)
        assert len(found) == 1 and found[0].period == "2024-09"

    def test_leading_zeros_are_trimmed(self):
        """Zeros a series opens with mean 'this route did not exist yet',
        not 'cargo collapsed'. Left in, the launch is the anomaly."""
        from services.analytics.anomaly import trim_leading_zeros
        per, vals = trim_leading_zeros(
            ["a", "b", "c", "d"], [0.0, 0.0, 5.0, 7.0]
        )
        assert per == ["c", "d"] and vals == [5.0, 7.0]

    def test_trailing_zeros_are_kept(self):
        """A collapse to zero at the END is exactly what should alert."""
        from services.analytics.anomaly import trim_leading_zeros
        per, vals = trim_leading_zeros(["a", "b", "c"], [5.0, 7.0, 0.0])
        assert vals == [5.0, 7.0, 0.0]


class TestSarimaOnNonSeasonalSeries:
    """Annual and fiscal series arrive with season=1, meaning "no
    seasonality". statsmodels rejects seasonal_order=(1,0,0,1) outright -
    periodicity must exceed 1 - so SARIMA silently failed on every one of
    them and the baseline won by default rather than on merit. A bare
    `except: return None` hid it."""

    def test_fits_a_non_seasonal_series(self):
        from services.analytics.forecast import _fit_sarima
        vals = [100.0, 120, 115, 140, 135, 160, 155, 180, 175, 200]
        assert _fit_sarima(vals, 1, 2) is not None

    def test_backtest_scores_both_models(self):
        vals = [100.0, 120, 115, 140, 135, 160, 155, 180, 175, 200]
        scores = rolling_origin_backtest(vals, season=1)
        assert "sarima" in scores and "seasonal_naive" in scores

    def test_a_trending_series_prefers_sarima(self):
        """On a clear trend the baseline repeats the last value and loses,
        which is the whole point of scoring them against each other."""
        vals = [100.0 + 20 * i for i in range(12)]
        per = [f"{2010 + i}-FY" for i in range(12)]
        out = forecast(per, vals, horizon=2)
        assert out and out[0].model == "sarima"
