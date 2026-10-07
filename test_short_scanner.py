import unittest
import json
import tempfile
from datetime import date
from pathlib import Path

import scanner
import short_scanner


def diagnostic_symbol(
    states,
    *,
    price=99.0,
    mas=None,
    ma_prevs=None,
    gaps=None,
    high=99.5,
    low=98.5,
):
    mas = mas or [100.0] * 9
    ma_prevs = ma_prevs or [101.0] * 9
    gaps = gaps or [-1.0] * 9
    return {
        "code": "0001",
        "name": "TEST",
        "ticker": "0001.T",
        "universe_rank": 1,
        "price": price,
        "price_source": "test",
        "last_bar": "test",
        "intraday_fresh": True,
        "quarantined": False,
        "available_ma_count": 9,
        "state_vector": states,
        "ma_vector": mas,
        "ma_prev_vector": ma_prevs,
        "gap_pct_vector": gaps,
        "day_high": high,
        "day_low": low,
        "week_high": high,
        "week_low": low,
        "month_high": high,
        "month_low": low,
    }


class ShortScannerTests(unittest.TestCase):
    def test_seed_short_fundamental_cache_reuses_same_day_entries(self):
        target = date(2026, 10, 7)
        with tempfile.TemporaryDirectory() as temp_dir:
            results_dir = Path(temp_dir)
            long_path = results_dir / scanner.FUNDAMENTAL_CACHE_FILENAME
            short_path = results_dir / short_scanner.SHORT_FUNDAMENTAL_CACHE_FILENAME
            cache_doc = {
                "schema_version": scanner.FUNDAMENTAL_CACHE_SCHEMA_VERSION,
                "target_date": target.isoformat(),
                "entries": {"1234.T": {"status": "ok"}},
            }
            long_path.write_text(json.dumps(cache_doc), encoding="utf-8")

            seeded = short_scanner.seed_short_fundamental_cache(
                results_dir, short_path, target
            )

            self.assertEqual(seeded, 1)
            loaded = scanner.load_fundamental_cache(short_path, target)
            self.assertEqual(loaded["1234.T"]["status"], "ok")

    def test_short_9_of_9_uses_state_zero_or_one(self):
        item = short_scanner.diagnostic_to_item(
            diagnostic_symbol([0, 1, 0, 1, 0, 1, 0, 1, 0])
        )
        self.assertEqual(item["short_score"], 9)

    def test_all_ma_below_ignores_ma_direction(self):
        item = short_scanner.diagnostic_to_item(
            diagnostic_symbol([0, 3, 6, 0, 3, 6, 0, 3, 6])
        )
        self.assertTrue(item["all_ma_below"])
        self.assertEqual(item["short_score"], 3)

    def test_near_short_9_can_flip_slightly_rising_d5(self):
        item = short_scanner.diagnostic_to_item(
            diagnostic_symbol(
                [6] + [0] * 8,
                ma_prevs=[99.9] + [101.0] * 8,
            )
        )
        candidate = short_scanner.near_short_9_of_9_candidate(item)
        self.assertIsNotNone(candidate)
        self.assertGreater(candidate["near_short_9_of_9_required_fall_pct"], 0.4)
        self.assertLess(candidate["near_short_9_of_9_required_fall_pct"], 0.7)

    def test_near_short_9_crosses_downtrend_ma_from_above(self):
        item = short_scanner.diagnostic_to_item(
            diagnostic_symbol(
                [2] + [0] * 8,
                price=100.5,
                high=100.6,
                low=100.2,
            )
        )
        candidate = short_scanner.near_short_9_of_9_candidate(item)
        self.assertIsNotNone(candidate)
        self.assertGreater(candidate["near_short_9_of_9_required_fall_pct"], 0.5)
        self.assertLess(candidate["near_short_9_of_9_required_fall_pct"], 0.8)

    def test_downside_overextension_is_mirrored(self):
        item = short_scanner.diagnostic_to_item(
            diagnostic_symbol(
                [0] * 9,
                gaps=[-1, -2, -3, -11, -16, -21, -16, -31, -41],
            )
        )
        self.assertEqual(
            short_scanner.weekly_ma_underextension_result(item)["comparison"],
            "underextended",
        )
        self.assertEqual(
            short_scanner.monthly_ma_underextension_result(item)["comparison"],
            "underextended",
        )

    def test_earnings_rank_a_requires_miss_and_progress_deterioration(self):
        fundamental = {
            "status": "ok",
            "current_year_eps_estimate": 80,
            "prior_year_eps": 100,
            "latest_result_vs_company_forecast": {"comparison": "missed"},
            "latest_result_vs_market_consensus": {"comparison": "unavailable"},
            "latest_progress_vs_prior": {"comparison": "deteriorated"},
        }
        rank, triggers = short_scanner.short_earnings_rank(fundamental)
        self.assertEqual(rank, "A")
        self.assertIn("forecast_eps_worsening", triggers)
        self.assertIn("progress_deteriorated", triggers)


if __name__ == "__main__":
    unittest.main()

