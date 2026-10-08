#!/usr/bin/env python3
"""Fail-closed EPS comparability guard for TSE MA short scanner results.

Run after short_scanner.py and before committing results.
This guard intentionally DOES NOT guess stock-split-adjusted EPS.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = 1
JST = ZoneInfo("Asia/Tokyo")
MIN_ANNUAL_GAP_DAYS = 330
MAX_ANNUAL_GAP_DAYS = 400

# Verified corporate actions, NOT an exhaustive list. Do not guess EPS basis.
# Issuer verification required for both forecast and prior actual.
KNOWN_SPLIT_REVIEW = {
    "7649.T": {"effective_date": "2026-09-01", "ratio": "1:2"},
    "8273.T": {"effective_date": "2026-03-01", "ratio": "1:3"},
    "1925.T": {"effective_date": "2026-10-01", "ratio": "1:2"},
}

GROUPS = (
    "short_9_of_9_candidates",
    "near_short_9_of_9_candidates",
    "short_8_of_9_candidates",
    "short_7_of_9_candidates",
    "all_ma_below_candidates",
    "near_all_ma_below_candidates",
)
DERIVED = (
    "near_all_ma_below_price_candidates",
    "near_all_ma_below_touch_candidates",
)
COUNTS = {
    "short_9_of_9": "short_9_of_9_candidates",
    "near_short_9_of_9": "near_short_9_of_9_candidates",
    "short_8_of_9": "short_8_of_9_candidates",
    "short_7_of_9": "short_7_of_9_candidates",
    "all_ma_below": "all_ma_below_candidates",
    "near_all_ma_below": "near_all_ma_below_candidates",
    "near_all_ma_below_price": "near_all_ma_below_price_candidates",
    "near_all_ma_below_touch": "near_all_ma_below_touch_candidates",
}

def eps_validation(fundamental: dict | None, ticker: str) -> dict:
    issues = []
    days = None
    forecast_end = (fundamental or {}).get("forecast_period_end")
    prior_end = (fundamental or {}).get("prior_period_end")
    if not fundamental or fundamental.get("status") != "ok":
        issues.append("eps_fundamental_not_ok")
    else:
        try:
            days = (date.fromisoformat(forecast_end) -
                    date.fromisoformat(prior_end)).days
        except (TypeError, ValueError):
            issues.append("eps_comparison_period_missing_or_invalid")
        else:
            if not MIN_ANNUAL_GAP_DAYS <= days <= MAX_ANNUAL_GAP_DAYS:
                issues.append("eps_forecast_not_comparable_full_year")
    split = KNOWN_SPLIT_REVIEW.get(ticker)
    if split and forecast_end and prior_end:
        try:
            split_day = date.fromisoformat(split["effective_date"])
            if date.fromisoformat(prior_end) < split_day <= date.fromisoformat(forecast_end):
                issues.append("known_split_eps_basis_unverified")
        except ValueError:
            issues.append("known_split_eps_basis_unverified")
    return {
        "passed": not issues,
        "reasons": issues,
        "forecast_period_end": forecast_end,
        "prior_period_end": prior_end,
        "period_gap_days": days,
        "annual_comparison_range_days": [MIN_ANNUAL_GAP_DAYS, MAX_ANNUAL_GAP_DAYS],
        "known_split": KNOWN_SPLIT_REVIEW.get(ticker),
    }

def apply_guard(document: dict, cache: dict) -> dict:
    if document.get("target_date") != cache.get("target_date"):
        raise ValueError("Result and EPS cache have different target dates")
    if document.get("session") not in {"noon", "close"}:
        raise ValueError("Unsupported result session")
    if any(not isinstance(document.get(g), list) for g in GROUPS + DERIVED):
        raise ValueError("Missing candidate groups: incompatible short-result schema")
    if not isinstance(cache.get("entries"), dict):
        raise ValueError("Missing EPS cache entries")
    if document.get("write_status") not in (None, "accepted"):
        raise ValueError("Short result has a rejected write status")

    # Compute each ticker's verdict once; repeated groups share a verdict.
    verdicts = {}
    all_items = [x for group in GROUPS for x in document[group]]
    for item in all_items:
        ticker = item.get("ticker")
        if not ticker:
            raise ValueError("Candidate ticker missing")
        verdicts[ticker] = eps_validation(cache["entries"].get(ticker), ticker)

    before = {k: len(document[k]) for k in GROUPS + DERIVED}
    excluded_codes = set()
    reasons_by_ticker = {}
    screened_out = []
    for group in GROUPS + DERIVED:
        kept = []
        for item in document[group]:
            ticker = item.get("ticker")
            verdict = verdicts.get(ticker)
            if verdict is None:
                raise ValueError(f"Derived candidate absent from main groups: {ticker}")
            item["eps_validation"] = verdict
            if verdict["passed"]:
                kept.append(item)
            else:
                excluded_codes.add(ticker)
                reasons_by_ticker[ticker] = verdict["reasons"]
                # Capture original membership and preserve all relevant reasons.
                screened_out.append({
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "ticker": ticker,
                    "group": group,
                    "price": item.get("price"),
                    "exclusion_reasons": verdict["reasons"],
                    "eps_validation": verdict,
                })
        document[group] = kept

    for count_key, group in COUNTS.items():
        document.setdefault("counts", {})[count_key] = len(document[group])
    existing = document.setdefault("screening_excluded", [])
    existing.extend(screened_out)
    fundamental_summary = document.get("screening_summary", {}).get("fundamental", {})
    requested = fundamental_summary.get("requested", len(cache["entries"]))
    comparable_count = sum(
        eps_validation(v, ticker)["passed"]
        for ticker, v in cache["entries"].items()
    )
    comparable_ratio = comparable_count / requested if requested else 1.0
    # Keep the quality gate aligned to the actual usable annual EPS data,
    # not the weaker "status=ok" TDnet count.
    if comparable_ratio < 0.50:
        raise RuntimeError(
            f"Comparable full-year EPS coverage too low: "
            f"{comparable_count}/{requested} ({comparable_ratio:.1%})"
        )
    quality = {
        "version": VERSION,
        "comparable_eps_count": comparable_count,
        "comparable_eps_requested": requested,
        "comparable_eps_ratio": round(comparable_ratio, 4),
        "validated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "decision": "fail_closed",
        "annual_comparison_gap_days": [MIN_ANNUAL_GAP_DAYS, MAX_ANNUAL_GAP_DAYS],
        "corporate_actions": "explicit verified split watchlist only; not exhaustive",
        "before": before,
        "after": {k: len(document[k]) for k in GROUPS + DERIVED},
        "flagged_unique_tickers": len(excluded_codes),
        "reason_counts_unique": dict(Counter(
            reason for reasons in reasons_by_ticker.values()
            for reason in reasons
        )),
        "note": "Exclusions are provisional. Recover true full-year EPS at TDnet source for definitive ranking.",
    }
    document["eps_comparability_guard"] = quality
    document.setdefault("screening_summary", {})["eps_comparability_guard"] = quality
    return document

def atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def run(results_dir: Path, session: str, target_date: date, overwrite: bool) -> dict:
    archive = results_dir / "short_archive" / f"{target_date}_{session}.json"
    cache_path = results_dir / "short_fundamental_eps_cache.json"
    if not archive.is_file() or not cache_path.is_file():
        raise FileNotFoundError("Short result archive or EPS cache missing")
    doc = json.loads(archive.read_text(encoding="utf-8"))
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    if doc.get("target_date") != target_date.isoformat():
        raise ValueError("Archive date mismatch")
    result = apply_guard(doc, cache)

    if overwrite:
        # Must operate after short_scanner.py has written archive and latest.
        atomic_write_json(archive, result)
        latest = results_dir / f"latest_short_{session}.json"
        if not latest.exists():
            raise FileNotFoundError("Latest short result missing")
        current = json.loads(latest.read_text(encoding="utf-8"))
        if (current.get("target_date") == result.get("target_date") and
            current.get("source_generated_at_jst") == result.get("source_generated_at_jst")):
            atomic_write_json(latest, result)
        else:
            print("Latest differs from archival source; latest left intact")
    else:
        atomic_write_json(
            results_dir / f"validated_short_{target_date}_{session}.json",
            result
        )
    return result

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--session", choices=["noon", "close"], required=True)
    parser.add_argument("--target-date", required=True, type=date.fromisoformat)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    result = run(args.results_dir, args.session, args.target_date, args.overwrite)
    print(json.dumps(result["eps_comparability_guard"], ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

