from __future__ import annotations

import unittest
from datetime import date

import scanner


class FakeConsensusProvider:
    name = "fake"

    def fetch_latest_result_comparisons(self, tickers, target_date):
        return {
            ticker: {
                "status": "ok",
                "comparison": "missed" if ticker == "0001.T" else "met",
                "provider": self.name,
            }
            for ticker in tickers
        }


class ResultExpectationTests(unittest.TestCase):
    def test_company_forecast_miss_is_detected(self):
        self.assertEqual(
            scanner.exact_period_company_comparison(90.0, 100.0),
            "missed",
        )

    def test_company_forecast_met_includes_equal(self):
        self.assertEqual(
            scanner.exact_period_company_comparison(100.0, 100.0),
            "met",
        )

    def test_missing_forecast_is_not_excluded(self):
        result = {"status": "unavailable", "comparison": "unavailable"}
        self.assertTrue(scanner.comparison_passes(result))
        self.assertEqual(
            scanner.exact_period_company_comparison(100.0, None),
            "unavailable",
        )

    def test_explicit_miss_is_excluded(self):
        self.assertFalse(scanner.comparison_passes({"comparison": "missed"}))

    def test_consensus_provider_interface_is_replaceable(self):
        provider = FakeConsensusProvider()
        values = provider.fetch_latest_result_comparisons(
            ["0001.T", "0002.T"], date(2026, 10, 5)
        )
        self.assertFalse(scanner.comparison_passes(values["0001.T"]))
        self.assertTrue(scanner.comparison_passes(values["0002.T"]))

    def test_default_consensus_provider_does_not_exclude(self):
        provider = scanner.UnavailableConsensusProvider()
        values = provider.fetch_latest_result_comparisons(
            ["0001.T"], date(2026, 10, 5)
        )
        self.assertTrue(scanner.comparison_passes(values["0001.T"]))

    def test_exclusion_reasons_are_auditable(self):
        fundamental = {
            "latest_result_vs_company_forecast": {"comparison": "missed"},
            "latest_result_vs_market_consensus": {"comparison": "met"},
        }
        self.assertEqual(
            scanner.result_expectation_exclusion_reasons(fundamental),
            ["latest_result_below_company_forecast"],
        )


if __name__ == "__main__":
    unittest.main()
