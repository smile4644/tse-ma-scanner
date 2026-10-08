"""Select comparable fiscal-year TDnet forecast EPS, not interim EPS.

This module is deliberately independent of tdnet/yfinance so the selection
logic can be unit-tested offline. The scanner supplies tdnet mapper callables.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any

MIN_YEAR_DAYS = 330
MAX_YEAR_DAYS = 400
MAX_STALE_FISCAL_END_DAYS = 120


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def numeric(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def collect_eps_forecasts(
    statements: Any,
    filing: Any,
    expected_ck: str,
    mapper_context: Any,
    mappers: tuple[Any, ...],
) -> list[dict]:
    """Retain ALL same-filing EPS forecasts (not just extract_values' first CK)."""
    out = []
    seen = set()
    for item in statements:
        if not any(mapper(item, mapper_context) == expected_ck for mapper in mappers):
            continue
        period = getattr(item, "period", None)
        end = as_date(getattr(period, "end_date", None))
        start = as_date(getattr(period, "start_date", None))
        value = numeric(getattr(item, "value", None))
        if end is None or value is None:
            continue
        days = (end - start).days if start is not None else None
        # Explicit interim duration is never treated as annual even if an
        # unrelated annual actual happens to be about one year older.
        if days is not None and not (MIN_YEAR_DAYS <= days <= MAX_YEAR_DAYS):
            continue
        dedup = (end, value, days)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({
            "filing": filing,
            "end": end,
            "start": start,
            "value": value,
            "period_days": days,
        })
    return out


def select_full_year_forecast(
    records: list[dict],
    annual_actuals: list[tuple],
    asof: date,
) -> tuple[Any, date, float, tuple] | None:
    """Match guidance to an audited/reported previous FULL fiscal year.

    A valid pair has forecast fiscal-year end 330–400 days after prior actual.
    Choose the latest *fiscal period* first, then most recent filing date,
    rather than choosing the latest interim forecast from one filing.
    """
    prior = []
    for raw in annual_actuals:
        if len(raw) < 3:
            continue
        end = as_date(raw[0])
        if end and numeric(raw[1]) is not None:
            prior.append((end, raw))
    if not prior:
        return None
    newest_prior_end = max(p[0] for p in prior)
    possibilities = []
    for row in records:
        end = as_date(row.get("end"))
        filing = row.get("filing")
        published = as_date(getattr(filing, "pubdate", None))
        if published is None:
            published = as_date(getattr(filing, "date", None))
        if published is None:
            # Some tdnet filing wrappers expose filing date only via
            # scanner.filing_date(): supplied as 'filing_date' in that case.
            published = as_date(row.get("filing_date"))
        if published is None or published > asof or end is None:
            continue
        if end <= newest_prior_end:
            continue  # historical guidance for an already closed fiscal year
        if end < asof - timedelta(days=MAX_STALE_FISCAL_END_DAYS):
            continue  # do not resurrect expired forecasts after fiscal rollover
        if row.get("period_days") is not None and not (
            MIN_YEAR_DAYS <= row["period_days"] <= MAX_YEAR_DAYS
        ):
            continue
        matched = [
            (p_end, raw) for p_end, raw in prior
            if MIN_YEAR_DAYS <= (end - p_end).days <= MAX_YEAR_DAYS
        ]
        if not matched:
            continue
        matched.sort(key=lambda x: x[0], reverse=True)
        value = numeric(row.get("value"))
        if value is None:
            continue
        possibilities.append((end, published, filing, value, matched[0][1]))

    if not possibilities:
        return None
    # Within a fiscal period use the latest filing's guidance, including
    # negative EPS revisions, which must not be overridden by older filings.
    possibilities.sort(key=lambda x: (x[0], x[1]), reverse=True)
    end, _published, filing, value, matched_actual = possibilities[0]
    return filing, end, value, matched_actual
