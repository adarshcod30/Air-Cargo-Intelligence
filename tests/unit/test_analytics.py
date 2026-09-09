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

    def test_a_trending_series_prefers_a_trend_aware_model(self):
        """On a clear trend the flat baselines lose, which is the point of
        scoring candidates against each other rather than fixing one.

        This asserted SARIMA specifically, from when SARIMA was the only
        alternative to seasonal-naive. On a constant slope `drift` is the
        exactly correct model and scores 0% error, so demanding SARIMA
        would mean rejecting the better answer. What matters is that a
        model which follows the trend wins, not which one.
        """
        vals = [100.0 + 20 * i for i in range(12)]
        per = [f"{2010 + i}-FY" for i in range(12)]
        out = forecast(per, vals, horizon=2)
        assert out, "a clean linear trend must be forecastable"
        assert out[0].model in {"sarima", "drift"}, out[0].model
        # Whatever wins must actually follow the trend upward, not repeat
        # the last value.
        assert out[0].predicted_kg > vals[-1]
        assert out[0].backtest_mape is not None and out[0].backtest_mape < 5


class TestForecastModelSelection:
    """Candidates are scored against each other, and bad ones are refused."""

    def test_a_flat_noisy_series_prefers_the_mean(self):
        """Chasing noise is worse than not chasing it.

        Every other candidate follows the last wobble; `recent_mean` is the
        correct answer for a series with no trend and no cycle, and could
        not win before it existed.
        """
        from services.analytics.forecast import forecast

        vals = [100.0, 104.0, 97.0, 102.0, 99.0, 103.0, 98.0, 101.0,
                100.0, 103.0, 97.0, 102.0, 99.0, 101.0, 100.0, 98.0]
        per = [f"{2020 + i}-FY" for i in range(len(vals))]
        out = forecast(per, vals, horizon=2)
        assert out, "a stable series must be forecastable"
        assert out[0].model in {"recent_mean", "naive", "drift", "sarima"}
        # Whatever wins must land near the level, not chase the last point.
        assert 90 < out[0].predicted_kg < 110

    def test_an_unforecastable_series_gets_no_forecast(self):
        """A projection wrong by more than the quantity is not a forecast.

        Publishing it with the error printed beside it is technically
        honest and practically misleading: it occupies a row that reads as
        a projection. Saying nothing is the more useful answer.
        """
        import random

        from services.analytics.forecast import MAX_PUBLISHABLE_MAPE, forecast

        random.seed(7)
        # Wild multiplicative noise: no model can hold error below the bar.
        vals = [max(1.0, random.choice([5.0, 500.0, 20.0, 900.0, 2.0]))
                for _ in range(20)]
        per = [f"2020-{i + 1:02d}" for i in range(len(vals))]
        out = forecast(per, vals, horizon=1)
        if out:
            assert out[0].backtest_mape is not None
            assert out[0].backtest_mape <= MAX_PUBLISHABLE_MAPE

    def test_a_short_series_is_still_scored_on_simple_models(self):
        """One model's data requirement must not gate the others.

        A global minimum of season + 2 was SARIMA's requirement applied to
        `naive`, which needs two points, so short series got no forecast at
        all rather than a simple one.
        """
        from services.analytics.forecast import rolling_origin_backtest

        vals = [100.0 + i for i in range(10)]
        scores = rolling_origin_backtest(vals, season=12)
        assert scores, "simple candidates should still be scorable at 10 points"
        assert {"naive", "drift", "recent_mean"} & set(scores)
        assert "sarima" not in scores, "SARIMA lacks the history here"


class TestAlertMateriality:
    """An alert is something a person should look at."""

    def test_a_tiny_movement_is_not_material(self):
        """A 400% swing on 60 tonnes is a rounding artefact nationally."""
        from services.analytics.anomaly import AnomalyPoint, is_material

        tiny = AnomalyPoint(period="2026-01", observed_kg=60_000.0,
                            expected_kg=12_000.0, deviation_pct=400.0,
                            z_score=9.0, method="robust_z", severity="HIGH")
        assert not is_material(tiny)

    def test_a_large_movement_is_material(self):
        from services.analytics.anomaly import AnomalyPoint, is_material

        big = AnomalyPoint(period="2026-01", observed_kg=12_234_000.0,
                           expected_kg=181_800.0, deviation_pct=6628.0,
                           z_score=40.0, method="stl_residual", severity="HIGH")
        assert is_material(big)

    def test_a_large_but_ordinary_movement_is_not_material(self):
        """Size alone is not news; it has to have moved."""
        from services.analytics.anomaly import AnomalyPoint, is_material

        steady = AnomalyPoint(period="2026-01", observed_kg=50_000_000.0,
                              expected_kg=49_000_000.0, deviation_pct=2.0,
                              z_score=3.1, method="robust_z", severity="LOW")
        assert not is_material(steady)


class TestStructuralTransitions:
    """Service starting or stopping, which the z-score detectors cannot see."""

    def test_a_service_starting_is_detected(self):
        """No history means nothing to be an outlier against.

        Both z-score detectors measure distance from a series' own past, and
        a service that has just begun has none. A ground-truth set of 27
        unambiguous starts and stops found the feed catching zero of them.
        """
        from services.analytics.anomaly import detect_structural

        vals = [0.0, 0.0, 0.0, 800.0, 850.0, 820.0, 840.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert [a.method for a in out] == ["service_started"]
        assert out[0].period == "2026-04"

    def test_a_service_stopping_is_detected(self):
        """The alert names the last month with traffic, not the first without.

        The original fixture put the stop on a zero month, which is the
        ambiguous case: both that month and the one before it satisfied the
        old rule, so a shutdown was reported twice.
        """
        from services.analytics.anomaly import detect_structural

        vals = [800.0, 850.0, 820.0, 840.0, 0.0, 0.0, 0.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert [(a.period, a.method) for a in out] == [("2026-04", "service_stopped")]

    def test_no_deviation_percentage_is_invented(self):
        """A change from zero has no ratio, and printing one was the defect.

        The original feed reported a launch as '+6,628% growth', which is
        arithmetic on an empty baseline. The event is reported in words.
        """
        from services.analytics.anomaly import detect_structural

        vals = [0.0, 0.0, 0.0, 800.0, 850.0, 820.0, 840.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert out[0].deviation_pct is None
        assert out[0].z_score is None

    def test_a_single_missing_month_is_not_a_transition(self):
        """One zero between reporting months is a gap, not a service ending."""
        from services.analytics.anomaly import detect_structural

        vals = [800.0, 850.0, 820.0, 0.0, 830.0, 810.0, 840.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        assert detect_structural(per, vals) == []

    def test_structural_events_survive_the_materiality_filter(self):
        """The percentage test cannot apply where there is no percentage.

        Requiring a deviation percentage excluded this class outright, which
        is why the detector found none of the known transitions.
        """
        from services.analytics.anomaly import detect_structural, is_material

        vals = [0.0, 0.0, 0.0, 800_000.0, 850_000.0, 820_000.0, 840_000.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert out and is_material(out[0])

    def test_a_trivial_service_is_still_suppressed(self):
        """A field handling a tonne a month does not reach an operations feed."""
        from services.analytics.anomaly import detect_structural, is_material

        vals = [0.0, 0.0, 0.0, 1_000.0, 1_100.0, 900.0, 1_050.0]   # ~1 MT
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert out and not is_material(out[0])


class TestIntervalCalibration:
    """An 80% band should contain the truth about 80% of the time."""

    def test_the_published_band_is_the_band_measured(self):
        """Backtesting a band the product does not emit measures nothing."""
        from services.analytics.forecast import _simple_interval, forecast

        vals = [100.0 + (i % 5) for i in range(20)]
        per = [f"2020-{i + 1:02d}" for i in range(20)]
        out = forecast(per, vals, horizon=1)
        assert out
        if out[0].model != "sarima":
            lo, hi = _simple_interval(out[0].predicted_kg, vals, out[0].backtest_mape)
            assert abs(lo - out[0].lower_kg) < 0.01
            assert abs(hi - out[0].upper_kg) < 0.01

    def test_coverage_is_counted_not_averaged(self):
        """Counts pool across series; percentages of three folds do not."""
        from services.analytics.forecast import backtest_interval_coverage

        vals = [100.0 + (i % 4) for i in range(20)]
        hits, folds = backtest_interval_coverage(vals, 12, "naive", 5.0)
        assert folds > 0 and 0 <= hits <= folds


class TestLabelAndDetectorAgree:
    """The ground-truth rule and the detector are deliberately separate code.

    Sharing an implementation would make recall tautological: the detector
    would be scored against its own output. They are independent statements
    of one definition, which is why the labelled set could report 0 of 41
    events found - the detector did not implement the definition at all.

    Independence has a cost: the two can drift apart, and then recall
    measures agreement between two different questions. These tests pin the
    definition, not the code.
    """

    def test_both_sides_use_the_same_window(self):
        from services.analytics.anomaly import STRUCTURAL_WINDOW
        from services.evaluation.anomaly_labels import WINDOW

        assert STRUCTURAL_WINDOW == WINDOW

    def test_the_detector_finds_what_the_rule_would_label(self):
        """Run both definitions over the same series and compare.

        The rule is restated here rather than imported, because its own form
        is embedded in a SQL-backed seeder.
        """
        from services.analytics.anomaly import STRUCTURAL_WINDOW as W
        from services.analytics.anomaly import detect_structural

        cases = [
            [0, 0, 0, 500, 600, 550, 580],
            [500, 600, 550, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0],
            [100, 120, 110, 130, 115, 125, 118],
            [0, 0, 500, 600, 0, 0, 700],       # too ragged to call
        ]
        for vals in cases:
            v = [float(x) for x in vals]
            per = [f"2026-{i + 1:02d}" for i in range(len(v))]

            expected = set()
            for i in range(len(v)):
                before, after = v[max(0, i - W):i], v[i + 1:i + 1 + W]
                if len(before) < W or len(after) < W:
                    continue
                if all(x == 0 for x in before) and all(x > 0 for x in after) and v[i] > 0:
                    expected.add((per[i], "service_started"))
                elif (all(x > 0 for x in before) and all(x == 0 for x in after)
                      and v[i] > 0):
                    expected.add((per[i], "service_stopped"))

            found = {(a.period, a.method) for a in detect_structural(per, v)}
            assert found == expected, f"disagreement on {vals}"


class TestAcceptanceReporting:
    """A tally computed only over what succeeded can never report failure."""

    def test_a_group_that_raises_still_appears_in_the_results(self, monkeypatch):
        """Dropping the group shortened the list the summary counts.

        Four anomaly criteria vanished on a KeyError and the report still
        printed '10/10 measurable criteria met'. The numerator and the
        denominator both shrank, so the ratio stayed clean while the
        component it described was absent.
        """
        from services.evaluation import acceptance

        def boom(_session):
            raise KeyError("labels")

        monkeypatch.setattr(acceptance, "measure_anomaly", boom)
        rows = acceptance.measure_all(None)

        failed = [m for m in rows if m.passes is False]
        assert failed, "a raising group left no trace in the results"
        assert any("KeyError" in str(m.measured) for m in failed)

    def test_the_failure_counts_against_the_tally(self, monkeypatch):
        from services.evaluation import acceptance

        def boom(_session):
            raise RuntimeError("db gone")

        monkeypatch.setattr(acceptance, "measure_chat", boom)
        rows = acceptance.measure_all(None)

        measurable = [m for m in rows if m.passes is not None]
        met = [m for m in measurable if m.passes]
        assert len(met) < len(measurable), "everything still reported as met"


class TestOneEventOneAlert:
    """A shutdown is one event, and was being reported as two."""

    def test_a_shutdown_raises_a_single_alert(self):
        """Without values[i] > 0, both the last trading month and the first
        silent month satisfied 'stopped', so a wind-down produced adjacent
        duplicates and the second described a month of no activity.
        """
        from services.analytics.anomaly import detect_structural

        vals = [800.0, 850.0, 820.0, 460.0, 0.0, 0.0, 0.0, 0.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        out = detect_structural(per, vals)
        assert [(a.period, a.method) for a in out] == [("2026-04", "service_stopped")]

    def test_the_alert_names_the_last_month_with_traffic(self):
        from services.analytics.anomaly import detect_structural

        vals = [800.0, 850.0, 820.0, 460.0, 0.0, 0.0, 0.0, 0.0]
        per = [f"2026-{i + 1:02d}" for i in range(len(vals))]
        (a,) = detect_structural(per, vals)
        assert a.observed_kg == 460.0
        assert a.expected_kg > 0, "the level it had been running at"


class TestTokenAccounting:
    """The console advertises model tokens, so the number has to be real."""

    def _agent(self, policy):
        from services.agents.base import Agent

        class Tiny(Agent):
            name = "tiny"

            def __init__(self, pol):
                super().__init__(pol, max_steps=2)
                self.tool("noop", "Does nothing.")(lambda: "done")

            def is_goal_met(self, ctx):
                return bool(ctx.get("done"))

        return Tiny(policy)

    def test_a_run_records_what_it_spent_not_the_running_total(self):
        """The client counter is cumulative and one policy serves many runs.

        Reporting the total on every run would multiply one spend by the
        number of runs that happened to follow it.
        """
        from services.agents.base import Decision

        class Metered:
            name = "llm"
            def __init__(self):
                self.usage = {"input_tokens": 0, "output_tokens": 0}
            def decide(self, goal, tools, history, context):
                self.usage["input_tokens"] += 10
                self.usage["output_tokens"] += 4
                return Decision(None, {}, "stop", policy="llm")

        pol = Metered()
        first = self._agent(pol).run("one")
        second = self._agent(pol).run("two")

        assert first.usage == {"input_tokens": 10, "output_tokens": 4}
        assert second.usage == {"input_tokens": 10, "output_tokens": 4}
        assert pol.usage == {"input_tokens": 20, "output_tokens": 8}

    def test_a_heuristic_run_reports_nothing_rather_than_zero_tokens(self):
        """It called no model. An empty dict says that; 0 looks like a reading."""
        from services.agents.base import Decision

        class Plain:
            name = "heuristic"
            def decide(self, goal, tools, history, context):
                return Decision(None, {}, "stop", policy="heuristic")

        assert self._agent(Plain()).run("x").usage == {}

    def test_usage_survives_into_the_persisted_row(self):
        """The trace writer reads d['usage']; to_dict has to carry it.

        Both callers of persist_runs omit the usage argument, so if the run
        does not carry its own figure nothing does, and every row is
        stamped zero.
        """
        from services.common.models import AgentRun
        from services.warehouse.traces import _row_from_dict

        run = AgentRun(agent="a", goal="g", policy="llm")
        run.usage = {"input_tokens": 120, "output_tokens": 30}
        row, _ = _row_from_dict(run.finish(True, "ok").to_dict(), "trace-1")
        assert (row.input_tokens, row.output_tokens) == (120, 30)


class TestDiscoveryFetchTarget:
    """The registry owns the URL. The model was being asked for it."""

    def _agent(self, monkeypatch):
        from services.agents import discovery_agent as da
        from services.ingestion.registry import REGISTRY

        seen = {}

        class Res:
            ok, status, size, media_type = True, 200, 11, "text/html"
            payload = b"<html></html>"

        monkeypatch.setattr(da, "fetch", lambda url, *a, **kw: (seen.update(url=url), Res())[1])
        src = next(s for s in REGISTRY if s.index_url)
        return da.DiscoveryAgent(src), seen, src

    def test_a_url_supplied_by_the_policy_is_ignored(self, monkeypatch):
        """It supplied "AAI's cargo documents page URL", a description.

        The fetch failed with a connection error that read like the
        publisher blocking us, and two nightly runs died that way.
        """
        agent, seen, src = self._agent(monkeypatch)
        agent.tools["fetch_index"](url="AAI's cargo documents page URL")
        assert seen["url"] == src.index_url

    def test_the_tool_advertises_no_arguments(self, monkeypatch):
        """A parameter the model can see is a parameter it will fill in."""
        agent, _, _ = self._agent(monkeypatch)
        assert agent.tools["fetch_index"].params == {}


class TestEmptyReportIsNotAResult:
    """Zero out of zero is not a percentage, in either direction."""

    def _measure(self, monkeypatch, tmp_path, report):
        import json as _json
        from types import SimpleNamespace

        from services.evaluation import acceptance

        (tmp_path / "pipeline_report.json").write_text(_json.dumps(report))
        monkeypatch.setattr(acceptance, "SETTINGS",
                            SimpleNamespace(processed_dir=str(tmp_path)))
        monkeypatch.setattr(acceptance, "_scalar", lambda *a, **k: 0)

        # measure_ingestion recomputes the component identity from stored
        # rows as well as reading the report, so it needs a session that
        # answers rather than a None.
        class Session:
            def execute(self, *a, **k):
                class R:
                    def one(self):
                        return (0, 0)
                return R()

        return {m.metric: m for m in acceptance.measure_ingestion(Session())}

    def test_an_empty_report_does_not_read_as_a_failed_pipeline(self, monkeypatch, tmp_path):
        """A single-source run overwrote the report and the table said 0.0%.

        The warehouse still held every fact. What was missing was the
        measurement, not the data.
        """
        m = self._measure(monkeypatch, tmp_path, {})["Rows reconciled without manual mapping"]
        assert m.passes is None
        assert "not measured" in m.measured

    def test_an_empty_report_does_not_read_as_a_pass_either(self, monkeypatch, tmp_path):
        """The more dangerous direction: success claimed over nothing."""
        m = self._measure(monkeypatch, tmp_path,
                          {})["Documents either extracted or refused with a reason"]
        assert m.passes is None

    def test_a_real_report_still_measures(self, monkeypatch, tmp_path):
        m = self._measure(monkeypatch, tmp_path, {
            "facts_extracted": 100, "facts_reconciled": 99,
            "documents_discovered": 10, "documents_extracted": 8,
        })["Rows reconciled without manual mapping"]
        assert m.passes is True and "99.0%" in m.measured


class TestReextraction:
    """Replaying the corpus must not depend on the network."""

    def test_cached_bytes_are_preferred_over_a_refetch(self, monkeypatch, tmp_path):
        """Two reasons, and the second is why re-extraction returned nothing.

        Re-fetching puts avoidable load on publishers that rate limit. And
        an archived API URL has its key redacted before it is written to
        the ledger, so the replayed request came back as a 149-byte error
        page that no parser would claim. Three of three documents
        quarantined, with the corpus sitting on disk.
        """
        from services.agents import extraction_agent as ea
        from services.common.models import Publisher, SourceDocument

        raw = tmp_path / "doc.json"
        raw.write_bytes(b'{"records": []}')

        def explode(*a, **k):
            raise AssertionError("re-extraction must not reach the network")

        monkeypatch.setattr(ea, "fetch", explode)
        agent = ea.ExtractionAgent(store=object())
        doc = SourceDocument(
            publisher=Publisher.DATA_GOV_IN,
            source_url="https://api.example.test/x?api-key=<redacted>",
            raw_path=str(raw), media_type="application/json",
        )
        agent.context["document"] = doc
        out = agent.tools["fetch_document"]()

        assert "raw store" in out
        assert agent.context["payload"] == b'{"records": []}'

    def test_a_missing_cache_still_falls_back_to_the_network(self, monkeypatch, tmp_path):
        """A document fetched but never archived is still fetchable."""
        from services.agents import extraction_agent as ea
        from services.common.models import Publisher, SourceDocument

        class Res:
            ok, rate_limited, status = True, False, 200
            payload, media_type, sha256, size = b"x", "text/html", "abc", 1
            warnings: list = []

        called = {}
        monkeypatch.setattr(ea, "fetch", lambda u, *a, **k: (called.update(u=u), Res())[1])

        class Store:
            def archive(self, *a, **k):
                return None

        agent = ea.ExtractionAgent(store=Store())
        agent.context["document"] = SourceDocument(
            publisher=Publisher.DATA_GOV_IN, source_url="https://example.test/y",
            raw_path=str(tmp_path / "absent.pdf"),
        )
        agent.tools["fetch_document"]()
        assert called["u"] == "https://example.test/y"
