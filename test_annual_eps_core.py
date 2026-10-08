import unittest
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import annual_eps_core as core


@dataclass
class FakeFiling:
    pubdate: str

@dataclass
class FakePeriod:
    start_date: date | None
    end_date: date

@dataclass
class FakeItem:
    value: float
    period: FakePeriod
    ck: str

def mapper(item, ctx):
    return item.ck


def record(eps, published, ending, beginning=None):
    return {
        "filing": FakeFiling(published),
        "end": date.fromisoformat(ending),
        "start": date.fromisoformat(beginning) if beginning else None,
        "value": eps,
        "period_days": (
            (date.fromisoformat(ending) - date.fromisoformat(beginning)).days
            if beginning else None
        ),
    }


class AnnualForecastSelectionTests(unittest.TestCase):
    def test_all_same_filing_eps_facts_are_retained(self):
        filing = FakeFiling("2026-07-09 15:30:00")
        statements = [
            FakeItem(94.49, FakePeriod(date(2026,3,1),date(2026,8,31)), "forecast_eps"),
            FakeItem(181.24,FakePeriod(date(2026,3,1),date(2027,2,28)), "forecast_eps"),
            FakeItem(9.00,FakePeriod(date(2026,3,1),date(2027,2,28)), "nonforecast"),
        ]
        out = core.collect_eps_forecasts(
            statements,filing,"forecast_eps",None,(mapper,))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["value"], 181.24)

    def test_full_year_guidance_selected_not_half_year(self):
        records = [
            record(94.49,"2026-07-09","2026-08-31","2026-03-01"),
            record(181.24,"2026-07-09","2027-02-28","2026-03-01"),
        ]
        annual_actuals=[(date(2026,2,28), 248.56, FakeFiling("2026-04-09"))]
        result=core.select_full_year_forecast(records,annual_actuals,date(2026,10,7))
        self.assertIsNotNone(result)
        self.assertEqual(result[2],181.24)
        self.assertEqual(result[1],date(2027,2,28))

    def test_no_full_year_value_fails_closed(self):
        records=[record(44.74,"2026-07-14","2026-08-31","2026-03-01")]
        prior=[(date(2026,2,28),79.40,FakeFiling("2026-04-14"))]
        self.assertIsNone(core.select_full_year_forecast(records,prior,date(2026,10,7)))

    def test_latest_revision_wins_even_if_negative(self):
        records=[
            record(100,"2026-05-13","2027-03-31","2026-04-01"),
            record(-15,"2026-08-09","2027-03-31","2026-04-01"),
        ]
        prior=[(date(2026,3,31),250,FakeFiling("2026-05-13"))]
        self.assertEqual(core.select_full_year_forecast(records,prior,date(2026,10,7))[2], -15)

    def test_future_filing_never_used_for_backtest(self):
        records=[record(80,"2026-10-08","2027-03-31","2026-04-01")]
        prior=[(date(2026,3,31),100,FakeFiling("2026-05-13"))]
        self.assertIsNone(core.select_full_year_forecast(records,prior,date(2026,10,7)))

    def test_expired_forecast_not_resurrected(self):
        records=[record(80,"2025-08-08","2026-03-31","2025-04-01")]
        prior=[(date(2025,3,31),100,FakeFiling("2025-05-13"))]
        self.assertIsNone(core.select_full_year_forecast(records,prior,date(2026,10,7)))

    def test_no_previous_annual_fails_closed(self):
        records=[record(80,"2026-08-08","2027-03-31","2026-04-01")]
        self.assertIsNone(core.select_full_year_forecast(records,[],date(2026,10,7)))

    def test_newer_actual_blocks_old_year_guidance(self):
        records=[record(80,"2026-08-08","2026-03-31","2025-04-01")]
        prior=[
            (date(2025,3,31),100,FakeFiling("2025-05-13")),
            (date(2026,3,31),105,FakeFiling("2026-05-13")),
        ]
        self.assertIsNone(core.select_full_year_forecast(records,prior,date(2026,10,7)))

    def test_non_finite_values_rejected(self):
        self.assertIsNone(core.numeric(float("nan")))
        self.assertIsNone(core.numeric(float("inf")))

    def test_annual_duration_checked_even_when_dates_match(self):
        records=[record(100,"2026-08-08","2027-03-31","2026-10-01")]
        prior=[(date(2026,3,31),100,FakeFiling("2026-05-13"))]
        self.assertIsNone(core.select_full_year_forecast(records,prior,date(2026,10,7)))

    def test_same_filing_duplicate_eps_removed(self):
        f=FakeFiling("2026-08-08")
        item=FakeItem(80,FakePeriod(date(2026,4,1),date(2027,3,31)),"forecast_eps")
        self.assertEqual(len(core.collect_eps_forecasts([item,item],f,"forecast_eps",None,(mapper,))),1)

    def test_different_financial_year_uses_most_recent(self):
        records=[
            record(55,"2026-01-20","2026-02-28","2025-03-01"),
            record(88,"2026-08-20","2027-02-28","2026-03-01"),
        ]
        annual=[
            (date(2025,2,28),110,FakeFiling("2025-05-14")),
            (date(2026,2,28),115,FakeFiling("2026-04-13")),
        ]
        self.assertEqual(core.select_full_year_forecast(records,annual,date(2026,10,7))[2],88)


if __name__ == "__main__":
    unittest.main()
