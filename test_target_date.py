from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

import scanner


class TargetDateRegressionTests(unittest.TestCase):
    def test_past_noon_is_supported(self):
        scanner.validate_target_date(date(2026, 10, 2), date(2026, 10, 3))
        self.assertEqual(
            scanner.intraday_period_for("noon", date(2026, 10, 2), date(2026, 10, 3)),
            "5d",
        )

    def test_past_close_remains_supported(self):
        scanner.validate_target_date(date(2026, 10, 2), date(2026, 10, 3))
        self.assertEqual(
            scanner.intraday_period_for("close", date(2026, 10, 2), date(2026, 10, 3)),
            "1d",
        )

    def test_future_date_is_rejected(self):
        with self.assertRaises(RuntimeError):
            scanner.validate_target_date(date(2026, 10, 4), date(2026, 10, 3))

    def test_same_day_noon_uses_one_day_intraday(self):
        self.assertEqual(
            scanner.intraday_period_for("noon", date(2026, 10, 2), date(2026, 10, 2)),
            "1d",
        )

    def test_historical_noon_uses_target_date_and_stops_at_1130(self):
        idx = pd.DatetimeIndex(
            [
                "2026-10-01 11:30:00+09:00",
                "2026-10-02 09:00:00+09:00",
                "2026-10-02 11:25:00+09:00",
                "2026-10-02 11:30:00+09:00",
                "2026-10-02 12:35:00+09:00",
                "2026-10-02 15:30:00+09:00",
                "2026-10-03 09:00:00+09:00",
            ]
        )
        frame = pd.DataFrame(
            {
                "Open": [50, 100, 101, 102, 900, 950, 999],
                "High": [60, 101, 103, 104, 999, 999, 1000],
                "Low": [40, 99, 100, 101, 1, 1, 998],
                "Close": [55, 100, 102, 103, 999, 980, 999],
                "Volume": [5, 10, 20, 30, 999, 999, 1],
            },
            index=idx,
        )

        bar = scanner.intraday_bar(frame, target_date=date(2026, 10, 2), session="noon")
        self.assertIsNotNone(bar)
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["high"], 104.0)
        self.assertEqual(bar["low"], 99.0)
        self.assertEqual(bar["close"], 103.0)
        self.assertEqual(bar["volume"], 60)
        self.assertTrue(bar["fresh"])
        self.assertIn("2026-10-02T11:30:00+09:00", bar["last_bar"])

    def test_close_intraday_cutoff_stays_1530(self):
        idx = pd.DatetimeIndex(
            [
                "2026-10-02 09:00:00+09:00",
                "2026-10-02 15:20:00+09:00",
                "2026-10-02 15:30:00+09:00",
                "2026-10-02 15:35:00+09:00",
            ]
        )
        frame = pd.DataFrame(
            {
                "Open": [100, 101, 102, 900],
                "High": [101, 103, 104, 999],
                "Low": [99, 100, 101, 1],
                "Close": [100, 102, 103, 999],
                "Volume": [10, 20, 30, 999],
            },
            index=idx,
        )

        bar = scanner.intraday_bar(frame, target_date=date(2026, 10, 2), session="close")
        self.assertIsNotNone(bar)
        self.assertEqual(bar["close"], 103.0)
        self.assertEqual(bar["volume"], 60)
        self.assertTrue(bar["fresh"])
        self.assertIn("2026-10-02T15:30:00+09:00", bar["last_bar"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
