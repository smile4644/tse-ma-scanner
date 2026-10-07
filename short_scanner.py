from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import scanner

SHORT_SCHEMA_VERSION = 1
MAX_NEAR_FALL_PCT = 1.0
SHORT_FUNDAMENTAL_CACHE_FILENAME = "short_fundamental_eps_cache.json"

WEEKLY_MA_UNDEREXTENSION_THRESHOLDS = {
    "w13": -10.0,
    "w26": -15.0,
    "w52": -20.0,
}
MONTHLY_MA_UNDEREXTENSION_THRESHOLDS = {
    "m12": -15.0,
    "m24": -30.0,
    "m60": -40.0,
}
MONTHLY_MA_UNDEREXTENSION_COMPOSITE_MAX_GAP_PCT = -15.0
MONTHLY_MA_UNDEREXTENSION_COMPOSITE_AVERAGE_GAP_PCT = -25.0


def safe_float(value):
    try:
        if value is None:
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    except Exception:
        return None


def short_state_pass(state: int | None) -> bool:
    return state in (0, 1)


def state_is_below_ma(state: int | None) -> bool:
    return state in (0, 3, 6)


def diagnostic_to_item(symbol: dict) -> dict | None:
    if symbol.get("available_ma_count") != len(scanner.SPECS):
        return None
    vectors = {
        "state": symbol.get("state_vector") or [],
        "ma": symbol.get("ma_vector") or [],
        "ma_prev": symbol.get("ma_prev_vector") or [],
        "gap_pct": symbol.get("gap_pct_vector") or [],
    }
    if any(len(v) != len(scanner.SPECS) for v in vectors.values()):
        return None

    states = {}
    short_score = 0
    for index, (key, label, _length, _timeframe) in enumerate(scanner.SPECS):
        state = vectors["state"][index]
        ma = safe_float(vectors["ma"][index])
        ma_prev = safe_float(vectors["ma_prev"][index])
        gap = safe_float(vectors["gap_pct"][index])
        if state is None or ma is None or ma_prev is None or gap is None:
            return None
        state = int(state)
        passed = short_state_pass(state)
        short_score += int(passed)
        states[key] = {
            "label": label,
            "state": state,
            "state_label": scanner.STATE_LABELS.get(state, str(state)),
            "short_pass": passed,
            "ma": ma,
            "ma_prev": ma_prev,
            "gap_pct": gap,
        }

    price = safe_float(symbol.get("price"))
    if price is None or price <= 0:
        return None

    candle = {
        "day": {
            "high": safe_float(symbol.get("day_high")),
            "low": safe_float(symbol.get("day_low")),
            "close": price,
        },
        "week": {
            "high": safe_float(symbol.get("week_high")),
            "low": safe_float(symbol.get("week_low")),
            "close": price,
        },
        "month": {
            "high": safe_float(symbol.get("month_high")),
            "low": safe_float(symbol.get("month_low")),
            "close": price,
        },
    }
    if any(
        candle[tf][field] is None
        for tf in candle
        for field in ("high", "low")
    ):
        return None

    all_ma_below = all(
        state_is_below_ma(states[key]["state"])
        for key in scanner.MA_KEYS
    )
    item = {
        "code": str(symbol.get("code")),
        "name": symbol.get("name"),
        "ticker": symbol.get("ticker"),
        "universe_rank": symbol.get("universe_rank"),
        "price": price,
        "price_source": symbol.get("price_source"),
        "last_bar": symbol.get("last_bar"),
        "intraday_fresh": symbol.get("intraday_fresh"),
        "quarantined": bool(symbol.get("quarantined")),
        "short_score": short_score,
        "short_score_label": f"{short_score}/{len(scanner.SPECS)}",
        "states": states,
        "candle": candle,
        "all_ma_below": all_ma_below,
        "short_failures": [
            f"{states[key]['label']} {states[key]['state_label']}"
            for key in scanner.MA_KEYS
            if not states[key]["short_pass"]
        ],
    }
    item.update(lowest_ma_fields(item))
    return item


def lowest_ma_fields(item: dict | None) -> dict:
    if not item:
        return {
            "lowest_ma_key": None,
            "lowest_ma_label": None,
            "lowest_ma_value": None,
            "price_to_lowest_ma_pct": None,
            "required_fall_to_lowest_ma_pct": None,
        }
    price = safe_float(item.get("price"))
    states = item.get("states") or {}
    values = []
    for key in scanner.MA_KEYS:
        state = states.get(key) or {}
        ma = safe_float(state.get("ma"))
        if ma is not None:
            values.append((key, state.get("label"), ma))
    if price is None or price <= 0 or len(values) != len(scanner.MA_KEYS):
        return {
            "lowest_ma_key": None,
            "lowest_ma_label": None,
            "lowest_ma_value": None,
            "price_to_lowest_ma_pct": None,
            "required_fall_to_lowest_ma_pct": None,
        }
    key, label, min_ma = min(values, key=lambda x: x[2])
    required_fall = max(0.0, (1.0 - min_ma / price) * 100.0)
    return {
        "lowest_ma_key": key,
        "lowest_ma_label": label,
        "lowest_ma_value": round(min_ma, 6),
        "price_to_lowest_ma_pct": round((price / min_ma - 1.0) * 100.0, 6),
        "required_fall_to_lowest_ma_pct": round(required_fall, 6),
    }


def near_short_9_of_9_candidate(item: dict) -> dict | None:
    if item.get("short_score") == len(scanner.SPECS):
        return None
    price = safe_float(item.get("price"))
    if price is None or price <= 0:
        return None
    tf_map = {"D": "day", "W": "week", "M": "month"}

    def passes(fall_pct: float) -> bool:
        delta = price * fall_pct / 100.0
        simulated_price = price - delta
        if simulated_price <= 0:
            return False
        for key, _label, length, timeframe in scanner.SPECS:
            state = item["states"][key]
            candle = item["candle"][tf_map[timeframe]]
            simulated_ma = float(state["ma"]) - delta / length
            simulated_low = min(float(candle["low"]), simulated_price)
            simulated_high = float(candle["high"])
            simulated_state = scanner.classify_state(
                simulated_ma,
                float(state["ma_prev"]),
                simulated_low,
                simulated_high,
            )
            if not short_state_pass(simulated_state):
                return False
        return True

    if not passes(MAX_NEAR_FALL_PCT):
        return None
    lo, hi = 0.0, MAX_NEAR_FALL_PCT
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if passes(mid):
            hi = mid
        else:
            lo = mid
    result = dict(item)
    result["near_short_9_of_9_required_fall_pct"] = round(hi, 6)
    result["near_short_9_of_9_target_price"] = round(
        price * (1.0 - hi / 100.0), 6
    )
    result["near_short_9_of_9_from_score"] = item.get("short_score")
    return result


def near_all_ma_below_candidate(item: dict) -> dict | None:
    if item.get("all_ma_below"):
        return None
    price = safe_float(item.get("price"))
    min_ma = safe_float(item.get("lowest_ma_value"))
    if price is None or price <= 0 or min_ma is None or min_ma <= 0:
        return None

    required = max(0.0, (1.0 - min_ma / price) * 100.0)
    if required > MAX_NEAR_FALL_PCT:
        return None

    tf_map = {key: tf for key, _label, _length, tf in scanner.SPECS}
    tf_name = {"D": "day", "W": "week", "M": "month"}
    blockers = []
    for key in scanner.MA_KEYS:
        state = item["states"][key]
        if state_is_below_ma(state["state"]):
            continue
        timeframe = tf_name[tf_map[key]]
        ma = safe_float(state.get("ma"))
        candle_high = safe_float(item["candle"][timeframe].get("high"))
        high_to_ma_pct = (
            (candle_high / ma - 1.0) * 100.0
            if candle_high is not None and ma not in (None, 0)
            else None
        )
        blockers.append({
            "key": key,
            "label": state["label"],
            "state": state["state"],
            "state_label": state["state_label"],
            "ma": state["ma"],
            "gap_pct": state.get("gap_pct"),
            "timeframe": timeframe,
            "candle_high": round(candle_high, 6) if candle_high is not None else None,
            "high_to_ma_pct": round(high_to_ma_pct, 6) if high_to_ma_pct is not None else None,
        })

    blocking_timeframes = [
        tf for tf in ("day", "week", "month")
        if any(x["timeframe"] == tf for x in blockers)
    ]
    if "month" in blocking_timeframes:
        earliest_recheck = "next_month"
    elif "week" in blocking_timeframes:
        earliest_recheck = "next_week"
    elif "day" in blocking_timeframes:
        earliest_recheck = "next_trading_day"
    else:
        earliest_recheck = None

    result = dict(item)
    result["near_all_ma_below_type"] = (
        "price_above_min_ma" if price > min_ma else "candle_touch_after_price_clear"
    )
    result["near_all_ma_below_required_fall_pct"] = round(required, 6)
    result["near_all_ma_below_target_price"] = round(min(price, min_ma), 6)
    result["blocking_ma"] = [x["key"] for x in blockers]
    result["blocking_ma_labels"] = [x["label"] for x in blockers]
    result["blocking_ma_details"] = blockers
    result["blocking_high_gap_pct"] = {
        x["key"]: x["high_to_ma_pct"] for x in blockers
    }
    result["blocking_timeframes"] = blocking_timeframes
    result["earliest_recheck"] = earliest_recheck
    return result


def ma_underextension_result(
    item: dict | None,
    thresholds: dict[str, float],
    *,
    missing_reason: str,
    underextended_reason: str,
) -> dict:
    if not item:
        return {
            "status": "unavailable",
            "comparison": "unavailable",
            "reason": "item_missing",
            "thresholds_pct": thresholds,
        }
    states = item.get("states") or {}
    gaps = {}
    for key, threshold in thresholds.items():
        gap = safe_float((states.get(key) or {}).get("gap_pct"))
        if gap is None:
            return {
                "status": "unavailable",
                "comparison": "unavailable",
                "reason": missing_reason,
                "thresholds_pct": thresholds,
            }
        gaps[key] = round(gap, 4)
    underextended = all(gaps[key] <= threshold for key, threshold in thresholds.items())
    return {
        "status": "ok",
        "comparison": "underextended" if underextended else "acceptable",
        "reason": underextended_reason if underextended else None,
        "thresholds_pct": thresholds,
        "gaps_pct": gaps,
        "threshold_margin_pct": {
            key: round(thresholds[key] - gaps[key], 4) for key in thresholds
        },
    }


def weekly_ma_underextension_result(item: dict | None) -> dict:
    return ma_underextension_result(
        item,
        WEEKLY_MA_UNDEREXTENSION_THRESHOLDS,
        missing_reason="weekly_ma_gap_missing",
        underextended_reason="all_weekly_ma_gaps_le_negative_thresholds",
    )


def monthly_ma_underextension_result(item: dict | None) -> dict:
    return ma_underextension_result(
        item,
        MONTHLY_MA_UNDEREXTENSION_THRESHOLDS,
        missing_reason="monthly_ma_gap_missing",
        underextended_reason="all_monthly_ma_gaps_le_negative_thresholds",
    )


def monthly_ma_composite_underextension_result(item: dict | None) -> dict:
    thresholds = {
        "maximum_each_gap_pct": MONTHLY_MA_UNDEREXTENSION_COMPOSITE_MAX_GAP_PCT,
        "maximum_average_gap_pct": MONTHLY_MA_UNDEREXTENSION_COMPOSITE_AVERAGE_GAP_PCT,
    }
    if not item:
        return {
            "status": "unavailable",
            "comparison": "unavailable",
            "reason": "item_missing",
            "thresholds_pct": thresholds,
        }
    states = item.get("states") or {}
    gaps = {}
    for key in ("m12", "m24", "m60"):
        gap = safe_float((states.get(key) or {}).get("gap_pct"))
        if gap is None:
            return {
                "status": "unavailable",
                "comparison": "unavailable",
                "reason": "monthly_ma_gap_missing",
                "thresholds_pct": thresholds,
            }
        gaps[key] = round(gap, 4)
    average_gap = sum(gaps.values()) / len(gaps)
    maximum_gap = max(gaps.values())
    underextended = (
        maximum_gap <= MONTHLY_MA_UNDEREXTENSION_COMPOSITE_MAX_GAP_PCT
        and average_gap <= MONTHLY_MA_UNDEREXTENSION_COMPOSITE_AVERAGE_GAP_PCT
    )
    return {
        "status": "ok",
        "comparison": "underextended" if underextended else "acceptable",
        "reason": (
            "all_monthly_ma_gaps_le_ceiling_and_average_le_threshold"
            if underextended else None
        ),
        "thresholds_pct": thresholds,
        "gaps_pct": gaps,
        "maximum_gap_pct": round(maximum_gap, 4),
        "average_gap_pct": round(average_gap, 4),
    }


def fundamental_worsening(fundamental: dict | None) -> bool:
    if not fundamental or fundamental.get("status") != "ok":
        return False
    current_eps = safe_float(fundamental.get("current_year_eps_estimate"))
    prior_eps = safe_float(fundamental.get("prior_year_eps"))
    return (
        current_eps is not None
        and prior_eps is not None
        and current_eps < prior_eps
    )


def short_earnings_rank(fundamental: dict | None) -> tuple[str | None, list[str]]:
    if not fundamental_worsening(fundamental):
        return None, []
    triggers = ["forecast_eps_worsening"]
    company_miss = (
        (fundamental.get("latest_result_vs_company_forecast") or {}).get("comparison")
        == "missed"
    )
    consensus_miss = (
        (fundamental.get("latest_result_vs_market_consensus") or {}).get("comparison")
        == "missed"
    )
    progress_bad = (
        (fundamental.get("latest_progress_vs_prior") or {}).get("comparison")
        == "deteriorated"
    )
    result_miss = company_miss or consensus_miss
    if company_miss:
        triggers.append("company_forecast_missed")
    if consensus_miss:
        triggers.append("market_consensus_missed")
    if progress_bad:
        triggers.append("progress_deteriorated")
    rank = "A" if result_miss and progress_bad else "B" if (result_miss or progress_bad) else "C"
    return rank, triggers


def short_screening_exclusion_reasons(
    item: dict,
    fundamental: dict | None,
) -> list[str]:
    reasons = []
    price = safe_float(item.get("price"))
    if price is None:
        reasons.append("price_unavailable")
    elif price > scanner.MAX_CANDIDATE_PRICE:
        reasons.append("price_above_limit")

    if not fundamental or fundamental.get("status") != "ok":
        reasons.append("earnings_data_unavailable")
    elif not fundamental_worsening(fundamental):
        reasons.append("earnings_not_worsening")

    monthly = monthly_ma_underextension_result(item)
    if monthly.get("comparison") == "underextended":
        reasons.append("monthly_ma_excessive_underextension")

    monthly_composite = monthly_ma_composite_underextension_result(item)
    if (
        monthly.get("comparison") != "underextended"
        and monthly_composite.get("comparison") == "underextended"
    ):
        reasons.append("monthly_ma_composite_underextension")

    weekly = weekly_ma_underextension_result(item)
    if weekly.get("comparison") == "underextended":
        reasons.append("weekly_ma_excessive_underextension")

    if item.get("quarantined"):
        reasons.append("source_symbol_quarantined")
    return list(dict.fromkeys(reasons))


def attach_short_screening_fields(item: dict, fundamental: dict | None) -> dict:
    rank, triggers = short_earnings_rank(fundamental)
    current_eps = safe_float((fundamental or {}).get("current_year_eps_estimate"))
    prior_eps = safe_float((fundamental or {}).get("prior_year_eps"))
    worsening_rate = (
        (current_eps - prior_eps) / abs(prior_eps) * 100.0
        if current_eps is not None and prior_eps not in (None, 0)
        else None
    )
    item["screening"] = {
        "price_limit": scanner.MAX_CANDIDATE_PRICE,
        "price_pass": item.get("price") is not None and item["price"] <= scanner.MAX_CANDIDATE_PRICE,
        "earnings_rule": "current-year company forecast EPS < latest prior full-year actual EPS",
        "earnings_status": (fundamental or {}).get("status", "not_checked"),
        "earnings_worsening": fundamental_worsening(fundamental),
        "current_year_eps_estimate": current_eps,
        "prior_year_eps": prior_eps,
        "earnings_worsening_rate_pct": round(worsening_rate, 4) if worsening_rate is not None else None,
        "short_earnings_rank": rank,
        "short_earnings_triggers": triggers,
        "latest_result_vs_company_forecast": (fundamental or {}).get("latest_result_vs_company_forecast"),
        "latest_result_vs_market_consensus": (fundamental or {}).get("latest_result_vs_market_consensus"),
        "latest_progress_vs_prior": (fundamental or {}).get("latest_progress_vs_prior"),
        "weekly_ma_underextension": weekly_ma_underextension_result(item),
        "monthly_ma_underextension": monthly_ma_underextension_result(item),
        "monthly_ma_composite_underextension": monthly_ma_composite_underextension_result(item),
        "exclusion_reasons": short_screening_exclusion_reasons(item, fundamental),
        "shortability_status": "not_checked",
    }
    return item


def source_path_for(results_dir: Path, session: str, target_date: date | None) -> Path:
    if target_date is None:
        return results_dir / f"latest_{session}.json"
    return results_dir / "archive" / f"{target_date.isoformat()}_{session}.json"


def seed_short_fundamental_cache(
    results_dir: Path,
    cache_path: Path,
    target_date: date,
) -> int:
    """Reuse same-day TDnet results already collected by the long scanner."""
    short_entries = scanner.load_fundamental_cache(cache_path, target_date)
    long_cache_path = results_dir / scanner.FUNDAMENTAL_CACHE_FILENAME
    long_entries = scanner.load_fundamental_cache(long_cache_path, target_date)
    merged = dict(long_entries)
    merged.update(short_entries)
    if merged == short_entries:
        return 0
    scanner.atomic_write_json(cache_path, {
        "schema_version": scanner.FUNDAMENTAL_CACHE_SCHEMA_VERSION,
        "generated_at_jst": datetime.now(scanner.JST).isoformat(timespec="seconds"),
        "target_date": target_date.isoformat(),
        "definition": (
            "Shared same-day TDnet XBRL cache; short scanner applies "
            "current-year forecast EPS < prior-year actual EPS"
        ),
        "entries": merged,
    })
    return len(merged) - len(short_entries)


def short_scan(
    session: str,
    results_dir: Path,
    target_date: date | None = None,
) -> dict:
    source_path = source_path_for(results_dir, session, target_date)
    if not source_path.exists():
        raise FileNotFoundError(f"source scan not found: {source_path}")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("write_status") != "accepted":
        raise RuntimeError("source scan is not accepted")
    if source.get("session") != session:
        raise RuntimeError("source session mismatch")
    if source.get("ma_order") != scanner.MA_KEYS:
        raise RuntimeError("source MA order mismatch")
    effective_date = date.fromisoformat(source["target_date"])
    if target_date is not None and effective_date != target_date:
        raise RuntimeError("source target_date mismatch")

    diagnostic_symbols = (source.get("diagnostics") or {}).get("symbols") or []
    items = [
        item for symbol in diagnostic_symbols
        if (item := diagnostic_to_item(symbol)) is not None
    ]

    raw_short_candidates = [x for x in items if x["short_score"] >= 7]
    raw_all_ma_below = [x for x in items if x["all_ma_below"]]
    raw_near_short_9 = [
        candidate for x in items
        if (candidate := near_short_9_of_9_candidate(x)) is not None
    ]
    raw_near_all_below = [
        candidate for x in items
        if (candidate := near_all_ma_below_candidate(x)) is not None
    ]

    pool_map = {}
    for x in raw_short_candidates + raw_all_ma_below + raw_near_short_9 + raw_near_all_below:
        if x.get("price") is not None and x["price"] <= scanner.MAX_CANDIDATE_PRICE:
            pool_map[x["ticker"]] = x
    screening_pool = list(pool_map.values())

    cache_path = results_dir / SHORT_FUNDAMENTAL_CACHE_FILENAME
    seeded_cache_entries = seed_short_fundamental_cache(
        results_dir,
        cache_path,
        effective_date,
    )
    fundamental_map, fundamental_summary = scanner.fetch_fundamental_screening(
        screening_pool,
        cache_path,
        effective_date,
    )
    fundamental_summary["seeded_from_long_cache"] = seeded_cache_entries

    all_raw_groups = raw_short_candidates + raw_all_ma_below + raw_near_short_9 + raw_near_all_below
    for x in all_raw_groups:
        attach_short_screening_fields(x, fundamental_map.get(x["ticker"]))

    def final_pass(item: dict) -> bool:
        return not short_screening_exclusion_reasons(
            item,
            fundamental_map.get(item["ticker"]),
        )

    short_candidates = [x for x in raw_short_candidates if final_pass(x)]
    all_ma_below = [x for x in raw_all_ma_below if final_pass(x)]
    near_short_9 = [x for x in raw_near_short_9 if final_pass(x)]
    near_all_below = [x for x in raw_near_all_below if final_pass(x)]

    rank_order = {"A": 0, "B": 1, "C": 2, None: 3}
    short_candidates.sort(key=lambda x: (
        -x["short_score"],
        rank_order.get((x.get("screening") or {}).get("short_earnings_rank"), 3),
        x.get("universe_rank") or 999999,
        x.get("code") or "",
    ))
    all_ma_below.sort(key=lambda x: (
        abs(safe_float(x.get("price_to_lowest_ma_pct")) or 999999.0),
        x.get("universe_rank") or 999999,
        x.get("code") or "",
    ))
    near_short_9.sort(key=lambda x: (
        x["near_short_9_of_9_required_fall_pct"],
        x.get("universe_rank") or 999999,
        x.get("code") or "",
    ))

    price_near = [x for x in near_all_below if x.get("near_all_ma_below_type") == "price_above_min_ma"]
    touch_near = [x for x in near_all_below if x.get("near_all_ma_below_type") == "candle_touch_after_price_clear"]
    price_near.sort(key=lambda x: (
        x["near_all_ma_below_required_fall_pct"],
        x.get("universe_rank") or 999999,
        x.get("code") or "",
    ))
    recheck_rank = {"next_trading_day": 0, "next_week": 1, "next_month": 2, None: 3}
    touch_near.sort(key=lambda x: (
        recheck_rank.get(x.get("earliest_recheck"), 3),
        abs(safe_float(x.get("price_to_lowest_ma_pct")) or 999999.0),
        x.get("universe_rank") or 999999,
        x.get("code") or "",
    ))
    near_all_below = price_near + touch_near

    exclusion_counts = dict(sorted(Counter(
        reason
        for item in screening_pool
        for reason in short_screening_exclusion_reasons(
            item, fundamental_map.get(item["ticker"])
        )
    ).items()))

    short_9 = [x for x in short_candidates if x["short_score"] == 9]
    short_8 = [x for x in short_candidates if x["short_score"] == 8]
    short_7 = [x for x in short_candidates if x["short_score"] == 7]

    document = {
        "schema_version": SHORT_SCHEMA_VERSION,
        "generated_at_jst": datetime.now(scanner.JST).isoformat(timespec="seconds"),
        "target_date": effective_date.isoformat(),
        "session": session,
        "source_scan": str(source_path),
        "source_schema_version": source.get("schema_version"),
        "source_generated_at_jst": source.get("generated_at_jst"),
        "source_price_series_mode": source.get("price_series_mode"),
        "strategy": "short_mirror_of_tse_ma_scanner_v19",
        "ma_order": scanner.MA_KEYS,
        "ma_labels": scanner.MA_LABELS,
        "definition": {
            "short_pass": "MA is falling and candle is below/touching MA: state 0 or 1",
            "short_9_of_9": "all 9 MAs pass the short condition",
            "near_short_9_of_9": "minimum price fall within 1% that makes all 9 states 0/1 using current candle high and recalculated MA",
            "all_ma_below": "all 9 candle positions are below MA regardless of MA direction: state 0/3/6",
            "near_all_ma_below": "A=price is within 1% above the lowest MA; B=price is already below every MA but candle high still touches/crosses an MA",
            "earnings": "forecast EPS must be strictly lower than prior full-year EPS",
            "earnings_rank": "A=EPS worsening + result miss + progress deterioration; B=EPS worsening + either result miss or progress deterioration; C=EPS worsening only",
            "downside_overextension": {
                "weekly": "exclude if w13<=-10%, w26<=-15%, w52<=-20% all hold",
                "monthly": "exclude if m12<=-15%, m24<=-30%, m60<=-40% all hold",
                "monthly_composite": "exclude if m12/m24/m60 are each <=-15% and their average <=-25%",
            },
            "price_max": scanner.MAX_CANDIDATE_PRICE,
            "shortability": "not_checked; borrow/stock-loan availability must be verified separately before trading",
        },
        "counts": {
            "short_9_of_9": len(short_9),
            "near_short_9_of_9": len(near_short_9),
            "short_8_of_9": len(short_8),
            "short_7_of_9": len(short_7),
            "all_ma_below": len(all_ma_below),
            "near_all_ma_below": len(near_all_below),
            "near_all_ma_below_price": len(price_near),
            "near_all_ma_below_touch": len(touch_near),
        },
        "screening_summary": {
            "source_diagnostic_symbols": len(diagnostic_symbols),
            "fully_reconstructed_symbols": len(items),
            "raw_short_7_plus": len(raw_short_candidates),
            "raw_all_ma_below": len(raw_all_ma_below),
            "raw_near_short_9_of_9": len(raw_near_short_9),
            "raw_near_all_ma_below": len(raw_near_all_below),
            "price_eligible_unique_symbols": len(screening_pool),
            "fundamental": fundamental_summary,
            "exclusion_reason_counts": exclusion_counts,
        },
        "short_9_of_9_candidates": short_9,
        "near_short_9_of_9_candidates": near_short_9,
        "short_8_of_9_candidates": short_8,
        "short_7_of_9_candidates": short_7,
        "all_ma_below_candidates": all_ma_below,
        "near_all_ma_below_candidates": near_all_below,
        "near_all_ma_below_price_candidates": price_near,
        "near_all_ma_below_touch_candidates": touch_near,
        "screening_excluded": [
            {
                "code": x.get("code"),
                "name": x.get("name"),
                "price": x.get("price"),
                "short_score": x.get("short_score"),
                "exclusion_reasons": short_screening_exclusion_reasons(
                    x, fundamental_map.get(x["ticker"])
                ),
            }
            for x in screening_pool
            if not final_pass(x)
        ],
    }

    latest_path = results_dir / f"latest_short_{session}.json"
    archive_path = results_dir / "short_archive" / f"{effective_date}_{session}.json"
    scanner.atomic_write_json(latest_path, document)
    scanner.atomic_write_json(archive_path, document)
    scanner.log(
        "short " + session + ": "
        f"9/9={len(short_9)} near9={len(near_short_9)} "
        f"8/9={len(short_8)} 7/9={len(short_7)} "
        f"all-below={len(all_ma_below)} near-all={len(near_all_below)}"
    )
    return document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["noon", "close"], required=True)
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--target-date", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    short_scan(args.session, Path(args.results_dir), args.target_date)


if __name__ == "__main__":
    main()

