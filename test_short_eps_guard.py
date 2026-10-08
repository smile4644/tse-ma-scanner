import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import short_eps_guard as g


def candidate(code, score=9):
    return {
        "code": code.split(".")[0],
        "name": "test_" + code,
        "ticker": code,
        "price": 100,
        "short_score": score,
        "screening": {"short_earnings_rank": "C"},
    }


def cache_entry(prior, forecast):
    return {
        "status": "ok",
        "prior_year_eps": 100,
        "current_year_eps_estimate": 90,
        "prior_period_end": prior,
        "forecast_period_end": forecast,
    }


def fixture():
    full = candidate("4996.T")
    half = candidate("2127.T")
    split = candidate("1925.T", 8)
    doc = {
        "target_date": "2026-10-07",
        "session": "close",
        "write_status": "accepted",
        "source_generated_at_jst": "2026-10-07T20:44:32+09:00",
        "counts": {},
        "screening_summary": {},
        "screening_excluded": [],
        "short_9_of_9_candidates": [copy.deepcopy(full), copy.deepcopy(half)],
        "near_short_9_of_9_candidates": [],
        "short_8_of_9_candidates": [copy.deepcopy(split)],
        "short_7_of_9_candidates": [],
        "all_ma_below_candidates": [copy.deepcopy(full)],
        "near_all_ma_below_candidates": [copy.deepcopy(half)],
        "near_all_ma_below_price_candidates": [copy.deepcopy(half)],
        "near_all_ma_below_touch_candidates": [],
    }
    cache = {
        "target_date": "2026-10-07",
        "entries": {
            "4996.T": cache_entry("2025-10-31", "2026-10-31"),
            "2127.T": cache_entry("2026-03-31", "2026-09-30"),
            "1925.T": cache_entry("2026-03-31", "2027-03-31"),
            # Keep fixture coverage at the production gate (2/4 = 50%) so
            # candidate filtering behavior can be tested independently.
            "9999.T": cache_entry("2025-12-31", "2026-12-31"),
        }
    }
    return doc, cache


class GuardTests(unittest.TestCase):
    def test_full_year_passes(self):
        verdict = g.eps_validation(cache_entry("2025-10-31", "2026-10-31"), "4996.T")
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["period_gap_days"], 365)

    def test_half_year_fails(self):
        verdict = g.eps_validation(cache_entry("2026-03-31", "2026-09-30"), "2127.T")
        self.assertFalse(verdict["passed"])
        self.assertIn("eps_forecast_not_comparable_full_year", verdict["reasons"])

    def test_known_split_is_quarantined_even_if_annual(self):
        verdict = g.eps_validation(cache_entry("2026-03-31", "2027-03-31"), "1925.T")
        self.assertFalse(verdict["passed"])
        self.assertIn("known_split_eps_basis_unverified", verdict["reasons"])

    def test_old_period_not_quarantined_forever_by_split(self):
        verdict = g.eps_validation(cache_entry("2028-03-31", "2029-03-31"), "1925.T")
        self.assertTrue(verdict["passed"])

    def test_missing_dates_fail_closed(self):
        verdict = g.eps_validation({"status": "ok"}, "4996.T")
        self.assertFalse(verdict["passed"])

    def test_missing_fundamental_fail_closed(self):
        self.assertFalse(g.eps_validation(None, "4996.T")["passed"])

    def test_applies_across_overlapping_groups_and_updates_counts(self):
        doc, cache = fixture()
        out = g.apply_guard(doc, cache)
        self.assertEqual(out["counts"]["short_9_of_9"], 1)
        self.assertEqual(out["counts"]["short_8_of_9"], 0)
        self.assertEqual(out["counts"]["all_ma_below"], 1)
        self.assertEqual(out["counts"]["near_all_ma_below"], 0)
        self.assertEqual(out["counts"]["near_all_ma_below_price"], 0)
        self.assertEqual(out["eps_comparability_guard"]["flagged_unique_tickers"], 2)

    def test_preserves_original_exclusion_reasons(self):
        doc, cache = fixture()
        doc["screening_excluded"].append({"code": "0000", "exclusion_reasons": ["other"]})
        out = g.apply_guard(doc, cache)
        self.assertEqual(out["screening_excluded"][0]["exclusion_reasons"], ["other"])

    def test_mismatched_cache_dates_error(self):
        doc, cache = fixture()
        cache["target_date"] = "2026-10-06"
        with self.assertRaises(ValueError):
            g.apply_guard(doc, cache)

    def test_missing_group_error(self):
        doc, cache = fixture()
        del doc["short_7_of_9_candidates"]
        with self.assertRaises(ValueError):
            g.apply_guard(doc, cache)

    def test_archive_is_untouched_without_overwrite(self):
        doc, cache = fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "short_archive").mkdir()
            p = root / "short_archive/2026-10-07_close.json"
            p.write_text(json.dumps(doc), encoding="utf-8")
            (root / "short_fundamental_eps_cache.json").write_text(
                json.dumps(cache), encoding="utf-8")
            g.run(root, "close", date(2026, 10, 7), False)
            self.assertEqual(json.loads(p.read_text())["counts"], {})
            verified = root / "validated_short_2026-10-07_close.json"
            self.assertEqual(json.loads(verified.read_text())["counts"]["short_9_of_9"], 1)

    def test_overwrite_updates_matching_latest_and_archive(self):
        doc, cache = fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "short_archive").mkdir()
            archive = root / "short_archive/2026-10-07_close.json"
            latest = root / "latest_short_close.json"
            archive.write_text(json.dumps(doc), encoding="utf-8")
            latest.write_text(json.dumps(doc), encoding="utf-8")
            (root / "short_fundamental_eps_cache.json").write_text(
                json.dumps(cache), encoding="utf-8")
            g.run(root, "close", date(2026, 10, 7), True)
            for p in (archive, latest):
                self.assertEqual(json.loads(p.read_text())["counts"]["short_9_of_9"], 1)


if __name__ == "__main__":
    unittest.main()

