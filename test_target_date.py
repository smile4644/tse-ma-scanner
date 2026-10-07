from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

import scanner


class TargetDateRegressionTests(unittest.TestCase):
    def test_noon_comparison_requires_same_schema_and_price_series(self):
        good = {
            "schema_version": scanner.SCHEMA_VERSION,
            "target_date": "2026-10-02",
            "session": "noon",
            "price_series_mode": scanner.PRICE_SERIES_MODE,
        }
        self.assertTrue(
            scanner.noon_comparison_compatible(
                good,
                date(2026, 10, 2),
            )
        )

        old_schema = dict(good)
        old_schema["schema_version"] = scanner.SCHEMA_VERSION - 1
        self.assertFalse(
            scanner.noon_comparison_compatible(
                old_schema,
                date(2026, 10, 2),
            )
        )

        wrong_mode = dict(good)
        wrong_mode["price_series_mode"] = "adjusted_close_auto_adjust_true"
        self.assertFalse(
            scanner.noon_comparison_compatible(
                wrong_mode,
                date(2026, 10, 2),
            )
        )

        wrong_date = dict(good)
        wrong_date["target_date"] = "2026-10-01"
        self.assertFalse(
            scanner.noon_comparison_compatible(
                wrong_date,
                date(2026, 10, 2),
            )
        )

    def test_past_noon_is_supported(self):
        scanner.validate_target_date(
            date(2026, 10, 2),
            date(2026, 10, 3),
        )
        self.assertEqual(
            scanner.intraday_period_for(
                "noon",
                date(2026, 10, 2),
                date(2026, 10, 3),
            ),
            "5d",
        )

    def test_past_close_remains_supported(self):
        scanner.validate_target_date(
            date(2026, 10, 2),
            date(2026, 10, 3),
        )
        self.assertEqual(
            scanner.intraday_period_for(
                "close",
                date(2026, 10, 2),
                date(2026, 10, 3),
            ),
            "1d",
        )

    def test_future_date_is_rejected(self):
        with self.assertRaises(RuntimeError):
            scanner.validate_target_date(
                date(2026, 10, 4),
                date(2026, 10, 3),
            )

    def test_same_day_noon_uses_one_day_intraday(self):
        self.assertEqual(
            scanner.intraday_period_for(
                "noon",
                date(2026, 10, 2),
                date(2026, 10, 2),
            ),
            "1d",
        )

    def test_historical_noon_uses_target_date_and_stops_at_1030(self):
        idx = pd.DatetimeIndex(
            [
                "2026-10-01 11:30:00+09:00",
                "2026-10-02 09:00:00+09:00",
                "2026-10-02 10:25:00+09:00",
                "2026-10-02 10:30:00+09:00",
                "2026-10-02 11:25:00+09:00",
                "2026-10-02 12:35:00+09:00",
                "2026-10-02 15:30:00+09:00",
                "2026-10-03 09:00:00+09:00",
            ]
        )

        frame = pd.DataFrame(
            {
                "Open": [
                    50,
                    100,
                    101,
                    102,
                    900,
                    950,
                    999,
                    1000,
                ],
                "High": [
                    60,
                    101,
                    103,
                    104,
                    999,
                    999,
                    1000,
                    1001,
                ],
                "Low": [
                    40,
                    99,
                    100,
                    101,
                    1,
                    1,
                    998,
                    999,
                ],
                "Close": [
                    55,
                    100,
                    102,
                    103,
                    999,
                    980,
                    999,
                    1000,
                ],
                "Volume": [
                    5,
                    10,
                    20,
                    30,
                    999,
                    999,
                    1,
                    1,
                ],
            },
            index=idx,
        )

        bar = scanner.intraday_bar(
            frame,
            target_date=date(2026, 10, 2),
            session="noon",
        )

        self.assertIsNotNone(bar)
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["high"], 104.0)
        self.assertEqual(bar["low"], 99.0)
        self.assertEqual(bar["close"], 103.0)
        self.assertEqual(bar["volume"], 60)
        self.assertTrue(bar["fresh"])
        self.assertEqual(
            bar["expected_minimum_time"],
            "10:25",
        )
        self.assertIn(
            "2026-10-02T10:30:00+09:00",
            bar["last_bar"],
        )

    def test_close_intraday_cutoff_stays_1530(self):
        idx = pd.DatetimeIndex(
            [
                "2026-10-02 09:00:00+09:00",
                "2026-10-02 15:25:00+09:00",
                "2026-10-02 15:30:00+09:00",
                "2026-10-02 15:35:00+09:00",
            ]
        )

        frame = pd.DataFrame(
            {
                "Open": [
                    100,
                    101,
                    102,
                    900,
                ],
                "High": [
                    101,
                    103,
                    104,
                    999,
                ],
                "Low": [
                    99,
                    100,
                    101,
                    1,
                ],
                "Close": [
                    100,
                    102,
                    103,
                    999,
                ],
                "Volume": [
                    10,
                    20,
                    30,
                    999,
                ],
            },
            index=idx,
        )

        bar = scanner.intraday_bar(
            frame,
            target_date=date(2026, 10, 2),
            session="close",
        )

        self.assertIsNotNone(bar)
        self.assertEqual(bar["close"], 103.0)
        self.assertEqual(bar["volume"], 60)
        self.assertTrue(bar["fresh"])
        self.assertEqual(
            bar["expected_minimum_time"],
            "15:29",
        )
        self.assertIn(
            "2026-10-02T15:30:00+09:00",
            bar["last_bar"],
        )

    def test_session_intervals_keep_noon_light_and_close_exact(self):
        self.assertEqual(
            scanner.INTRADAY_SESSION_TIMES["noon"]["interval"],
            "5m",
        )
        self.assertEqual(
            scanner.INTRADAY_SESSION_TIMES["close"]["interval"],
            "1m",
        )

    def test_same_day_close_uses_confirmed_daily_bar(self):
        self.assertTrue(
            scanner.uses_confirmed_daily_close("close")
        )
        self.assertFalse(
            scanner.uses_confirmed_daily_close("noon")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
