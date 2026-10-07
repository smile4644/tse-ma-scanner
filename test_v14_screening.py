from __future__ import annotations

import unittest

import scanner


def make_item(
    *,
    price=100.0,
    all_ma_above=False,
    max_ma=99.0,
    blocker_key="d5",
    monthly_gaps=(5.0, 10.0, 15.0),
    weekly_gaps=(5.0, 10.0, 15.0),
):
    states = {}
    for key, label, _length, _tf in scanner.SPECS:
        states[key] = {
            "label": label,
            "state": 8,
            "state_label": scanner.STATE_LABELS[8],
            "ma": 90.0,
            "ma_prev": 89.0,
            "gap_pct": 10.0,
        }
    states[blocker_key].update({
        "state": 7,
        "state_label": scanner.STATE_LABELS[7],
        "ma": max_ma,
        "gap_pct": (price / max_ma - 1.0) * 100.0,
    })
    for key, gap in zip(("m12", "m24", "m60"), monthly_gaps):
        states[key]["gap_pct"] = gap
        # 月足がblocker_keyの場合は上で指定したMA/stateを維持する。
        if key != blocker_key:
            states[key]["ma"] = price / (1.0 + gap / 100.0)
    for key, gap in zip(("w13", "w26", "w52"), weekly_gaps):
        states[key]["gap_pct"] = gap
        # 週足がblocker_keyの場合は上で指定したMA/stateを維持する。
        if key != blocker_key:
            states[key]["ma"] = price / (1.0 + gap / 100.0)
    return {
        "code": "TEST",
        "name": "TEST",
        "ticker": "TEST.T",
        "price": price,
        "score": 8,
        "available_ma_count": 9,
        "all_ma_above": all_ma_above,
        "universe_rank": 1,
        "states": states,
        "candle": {
            "day": {"low": 98.0, "high": 101.0},
            "week": {"low": 97.0, "high": 102.0},
            "month": {"low": 96.0, "high": 103.0},
        },
    }


def passing_fundamental():
    return {
        "status": "ok",
        "improving": True,
        "latest_result_vs_company_forecast": {"comparison": "met"},
        "latest_result_vs_market_consensus": {"comparison": "unavailable"},
        "latest_progress_vs_prior": {"comparison": "acceptable"},
    }


class V14ProgressTests(unittest.TestCase):
    def test_aida_like_progress_is_excluded(self):
        result = scanner.progress_deterioration_comparison(
            801.0, 6000.0, 1394.0, 6000.0,
            metric="ordinary_income",
        )
        self.assertEqual(result["comparison"], "deteriorated")
        self.assertAlmostEqual(result["current_progress_pct"], 13.35, places=2)
        self.assertAlmostEqual(result["prior_progress_pct"], 23.2333, places=4)
        self.assertLessEqual(result["progress_gap_pp"], -5.0)
        self.assertGreaterEqual(
            result["relative_progress_deterioration_pct"], 30.0
        )
        self.assertLessEqual(result["same_period_profit_yoy_pct"], -20.0)

    def test_relative_drop_alone_is_not_enough_when_gap_under_5pp(self):
        result = scanner.progress_deterioration_comparison(
            69.0, 1000.0, 100.0, 1000.0
        )
        self.assertEqual(result["comparison"], "acceptable")
        self.assertGreaterEqual(
            result["relative_progress_deterioration_pct"], 30.0
        )
        self.assertGreater(result["progress_gap_pp"], -5.0)

    def test_profit_decline_under_20pct_does_not_exclude(self):
        result = scanner.progress_deterioration_comparison(
            85.0, 1000.0, 100.0, 500.0
        )
        self.assertEqual(result["comparison"], "acceptable")
        self.assertGreaterEqual(
            result["relative_progress_deterioration_pct"], 30.0
        )
        self.assertLessEqual(result["progress_gap_pp"], -5.0)
        self.assertGreater(result["same_period_profit_yoy_pct"], -20.0)

    def test_missing_prior_forecast_is_unavailable_and_passes(self):
        result = scanner.progress_deterioration_comparison(
            80.0, 1000.0, 100.0, None
        )
        self.assertEqual(result["comparison"], "unavailable")
        self.assertTrue(scanner.progress_comparison_passes(result))


class V15MonthlyOverextensionTests(unittest.TestCase):
    def test_all_three_monthly_thresholds_are_excluded(self):
        item = make_item(monthly_gaps=(15.0, 30.0, 40.0))
        result = scanner.monthly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "overextended")

    def test_one_monthly_gap_under_its_threshold_passes(self):
        item = make_item(monthly_gaps=(15.0, 30.0, 39.99))
        result = scanner.monthly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "acceptable")

    def test_yamaha_like_monthly_gaps_are_excluded(self):
        item = make_item(monthly_gaps=(32.6474, 42.1820, 49.9076))
        fundamental = passing_fundamental()
        reasons = scanner.screening_exclusion_reasons(item, fundamental)
        self.assertEqual(reasons, ["monthly_ma_excessive_overextension"])

    def test_resona_like_monthly_gaps_are_excluded(self):
        item = make_item(monthly_gaps=(25.18, 52.58, 130.96))
        result = scanner.monthly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "overextended")

    def test_japan_post_bank_like_monthly_gaps_are_excluded(self):
        item = make_item(monthly_gaps=(17.68, 51.41, 108.13))
        result = scanner.monthly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "overextended")


class V16MonthlyCompositeOverextensionTests(unittest.TestCase):
    def test_aozora_like_gaps_are_excluded(self):
        item = make_item(monthly_gaps=(19.9859, 34.0853, 29.8797))
        result = scanner.monthly_ma_composite_overextension_result(item)
        self.assertEqual(result["comparison"], "overextended")
        self.assertAlmostEqual(result["average_gap_pct"], 27.9836, places=4)
        self.assertEqual(
            scanner.screening_exclusion_reasons(item, passing_fundamental()),
            ["monthly_ma_composite_overextension"],
        )

    def test_average_below_threshold_passes(self):
        item = make_item(monthly_gaps=(15.0, 20.0, 39.99))
        result = scanner.monthly_ma_composite_overextension_result(item)
        self.assertEqual(result["comparison"], "acceptable")

    def test_one_gap_below_floor_passes_even_if_average_is_high(self):
        item = make_item(monthly_gaps=(14.99, 30.0, 40.0))
        result = scanner.monthly_ma_composite_overextension_result(item)
        self.assertEqual(result["comparison"], "acceptable")

    def test_missing_ma_is_unavailable_and_does_not_exclude(self):
        item = make_item(monthly_gaps=(20.0, 30.0, 40.0))
        del item["states"]["m60"]
        result = scanner.monthly_ma_composite_overextension_result(item)
        self.assertEqual(result["comparison"], "unavailable")
        self.assertNotIn(
            "monthly_ma_composite_overextension",
            scanner.screening_exclusion_reasons(item, passing_fundamental()),
        )


class V15WeeklyOverextensionTests(unittest.TestCase):
    def test_all_three_weekly_thresholds_are_excluded(self):
        item = make_item(weekly_gaps=(10.0, 15.0, 20.0))
        result = scanner.weekly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "overextended")

    def test_one_weekly_gap_under_its_threshold_passes(self):
        item = make_item(weekly_gaps=(10.0, 15.0, 19.99))
        result = scanner.weekly_ma_overextension_result(item)
        self.assertEqual(result["comparison"], "acceptable")

    def test_mitsubishi_pencil_like_weekly_gaps_are_excluded(self):
        item = make_item(weekly_gaps=(14.6, 23.9, 30.0))
        fundamental = passing_fundamental()
        reasons = scanner.screening_exclusion_reasons(item, fundamental)
        self.assertEqual(reasons, ["weekly_ma_excessive_overextension"])


class V14NearAllTests(unittest.TestCase):
    def test_price_below_max_ma_is_type_a(self):
        item = make_item(price=100.0, max_ma=100.5, blocker_key="d5")
        result = scanner.near_all_ma_above_candidate(item)
        self.assertIsNotNone(result)
        self.assertEqual(result["near_all_ma_above_type"], "price_below_max_ma")
        self.assertAlmostEqual(
            result["highest_ma_to_price_required_pct"], 0.5, places=6
        )

    def test_price_clear_with_daily_touch_is_type_b(self):
        item = make_item(price=100.0, max_ma=99.0, blocker_key="d5")
        result = scanner.near_all_ma_above_candidate(item)
        self.assertIsNotNone(result)
        self.assertEqual(
            result["near_all_ma_above_type"],
            "candle_touch_after_price_clear",
        )
        self.assertEqual(result["earliest_recheck"], "next_trading_day")

    def test_monthly_touch_requires_next_month(self):
        item = make_item(price=100.0, max_ma=99.0, blocker_key="m12")
        result = scanner.near_all_ma_above_candidate(item)
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_recheck"], "next_month")

    def test_weekly_touch_requires_next_week(self):
        item = make_item(price=100.0, max_ma=99.0, blocker_key="w13")
        result = scanner.near_all_ma_above_candidate(item)
        self.assertIsNotNone(result)
        self.assertEqual(result["earliest_recheck"], "next_week")

    def test_blocking_low_gap_is_recorded(self):
        item = make_item(price=100.0, max_ma=99.0, blocker_key="d5")
        result = scanner.near_all_ma_above_candidate(item)
        self.assertIsNotNone(result)
        self.assertIn("d5", result["blocking_low_gap_pct"])
        self.assertAlmostEqual(
            result["blocking_low_gap_pct"]["d5"],
            (98.0 / 99.0 - 1.0) * 100.0,
            places=6,
        )


class V14AuditTests(unittest.TestCase):
    def test_aida_like_progress_reason_is_auditable(self):
        item = make_item(price=1232.0, monthly_gaps=(5.0, 10.0, 15.0))
        fundamental = passing_fundamental()
        fundamental["latest_progress_vs_prior"] = {"comparison": "deteriorated"}
        self.assertEqual(
            scanner.screening_exclusion_reasons(item, fundamental),
            ["quarter_progress_significantly_deteriorated"],
        )

    def test_multiple_exclusion_reasons_accumulate(self):
        item = make_item(monthly_gaps=(35.0, 40.0, 45.0))
        fundamental = passing_fundamental()
        fundamental["latest_progress_vs_prior"] = {"comparison": "deteriorated"}
        self.assertEqual(
            scanner.screening_exclusion_reasons(item, fundamental),
            [
                "quarter_progress_significantly_deteriorated",
                "monthly_ma_excessive_overextension",
            ],
        )

    def test_all_ma_sort_prefers_freshest_highest_ma_clearance(self):
        a = make_item(price=101.0, max_ma=100.0)
        b = make_item(price=105.0, max_ma=100.0)
        scanner.attach_ma_level_fields(a)
        scanner.attach_ma_level_fields(b)
        self.assertLess(scanner.all_ma_above_sort_key(a), scanner.all_ma_above_sort_key(b))

    def test_schema_is_v19(self):
        self.assertEqual(scanner.SCHEMA_VERSION, 19)


if __name__ == "__main__":
    unittest.main(verbosity=2)
