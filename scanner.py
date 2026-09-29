from __future__ import annotations

import argparse
import io
import json
import math
import re
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

JST = ZoneInfo("Asia/Tokyo")
JPX_MASTER_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
USER_AGENT = "tse-ma-scanner/1.1 (+github)"

SPECS = [
    ("d5", "日5", 5, "D"), ("d25", "日25", 25, "D"), ("d75", "日75", 75, "D"),
    ("w13", "週13", 13, "W"), ("w26", "週26", 26, "W"), ("w52", "週52", 52, "W"),
    ("m12", "月12", 12, "M"), ("m24", "月24", 24, "M"), ("m60", "月60", 60, "M"),
]
STATE_LABELS = {
    0: "↓下", 1: "↓接触", 2: "↓上",
    3: "→下", 4: "→接触", 5: "→上",
    6: "↑下", 7: "↑接触", 8: "↑上",
}
IMPORTANT_TOUCHES = {"d25", "d75", "w13", "w26", "m12"}


def log(message: str) -> None:
    print(f"[{datetime.now(JST).isoformat(timespec='seconds')}] {message}", flush=True)


def normalize_code(value) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, np.integer)):
        return str(int(value)).zfill(4)
    if isinstance(value, float) and value.is_integer():
        return str(int(value)).zfill(4)
    text = re.sub(r"\.0$", "", str(value).strip().upper())
    if re.fullmatch(r"\d{1,4}", text):
        return text.zfill(4)
    if re.fullmatch(r"\d{3}[A-Z]", text):
        return text
    return None


def load_jpx_master() -> pd.DataFrame:
    response = requests.get(JPX_MASTER_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    raw = pd.read_excel(io.BytesIO(response.content), engine="openpyxl")
    code_col = next(c for c in raw.columns if "コード" in str(c))
    name_col = next(c for c in raw.columns if "銘柄名" in str(c))
    market_col = next(c for c in raw.columns if "市場" in str(c) and "区分" in str(c))
    df = pd.DataFrame({
        "code": raw[code_col].map(normalize_code),
        "name": raw[name_col].astype(str).str.strip(),
        "market": raw[market_col].astype(str).str.strip(),
    })
    eligible = {"プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"}
    df = df[df["market"].isin(eligible) & df["code"].notna()].copy()
    df["ticker"] = df["code"] + ".T"
    return df.drop_duplicates("ticker").reset_index(drop=True)


def chunked(items: list[str], size: int = 100):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def split_yf_download(data: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    result = {}
    if data is None or data.empty:
        return result
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(map(str, data.columns.get_level_values(0)))
        level1 = set(map(str, data.columns.get_level_values(1)))
        for ticker in tickers:
            frame = None
            try:
                if ticker in level0:
                    frame = data[ticker]
                elif ticker in level1:
                    frame = data.xs(ticker, axis=1, level=1)
            except Exception:
                pass
            if frame is not None:
                frame = frame.dropna(how="all")
                if not frame.empty:
                    result[ticker] = frame
    elif len(tickers) == 1:
        frame = data.dropna(how="all")
        if not frame.empty:
            result[tickers[0]] = frame
    return result


def download_many(tickers: list[str], period: str, interval: str, batch_size: int = 100):
    result = {}
    batches = list(chunked(tickers, batch_size))
    for batch_no, batch in enumerate(batches, 1):
        last_error = None
        for attempt in range(3):
            try:
                data = yf.download(
                    batch, period=period, interval=interval, group_by="ticker",
                    auto_adjust=False, prepost=False, threads=True,
                    progress=False, timeout=30,
                )
                result.update(split_yf_download(data, batch))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        if last_error:
            log(f"{interval} batch {batch_no}/{len(batches)} failed: {last_error}")
        if batch_no % 5 == 0 or batch_no == len(batches):
            log(f"{interval}: {batch_no}/{len(batches)} batches, {len(result)}/{len(tickers)} tickers")
    return result


def local_dates(frame: pd.DataFrame) -> pd.Series:
    idx = pd.to_datetime(frame.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(JST).tz_localize(None)
    return pd.Series(idx.date, index=frame.index)


def build_top1000_universe(output_path: Path) -> pd.DataFrame:
    master = load_jpx_master()
    daily = download_many(master["ticker"].tolist(), period="10d", interval="1d")
    today = datetime.now(JST).date()
    rows = []
    for row in master.itertuples(index=False):
        frame = daily.get(row.ticker)
        if frame is None or frame.empty or "Volume" not in frame.columns:
            continue
        dates = local_dates(frame)
        mask = dates < today
        if not mask.any():
            continue
        prior = frame.loc[mask.values]
        prior_dates = dates.loc[mask.values]
        volume = pd.to_numeric(prior["Volume"], errors="coerce").iloc[-1]
        if pd.notna(volume):
            rows.append((row.code, row.name, row.market, row.ticker, prior_dates.iloc[-1], int(volume)))
    if not rows:
        raise RuntimeError("前営業日の出来高データを取得できませんでした。")
    as_of = max(row[4] for row in rows)
    universe = pd.DataFrame(rows, columns=["code", "name", "market", "ticker", "date", "volume"])
    universe = universe[universe["date"] == as_of].copy()
    universe = universe.sort_values(["volume", "code"], ascending=[False, True]).head(1000).reset_index(drop=True)
    universe.insert(0, "rank", np.arange(1, len(universe) + 1))
    universe.insert(0, "as_of", as_of.isoformat())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(output_path, index=False)
    log(f"Universe {as_of}: {len(universe)}銘柄")
    return universe


def history_before(frame, cutoff_date):
    if frame is None or frame.empty:
        return pd.DataFrame()
    dates = local_dates(frame)
    return frame.loc[(dates < cutoff_date).values].copy()


def numeric_series(frame, column):
    if frame is None or frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def ma_current_and_previous(completed_closes, current_close, length):
    completed = pd.to_numeric(completed_closes, errors="coerce").dropna()
    if len(completed) < length:
        return None, None
    current_values = pd.concat([completed, pd.Series([current_close])], ignore_index=True).iloc[-length:]
    return float(current_values.mean()), float(completed.iloc[-length:].mean())


def classify_state(ma, previous_ma, candle_low, candle_high):
    if ma is None or previous_ma is None:
        return -1
    direction = 2 if ma > previous_ma else 0 if ma < previous_ma else 1
    position = 2 if candle_low > ma else 0 if candle_high < ma else 1
    return direction * 3 + position


def partial_intraday_bar(frame, target_date, session):
    if frame is None or frame.empty:
        return None
    work = frame.copy()
    idx = pd.to_datetime(work.index)
    idx = idx.tz_localize(JST) if getattr(idx, "tz", None) is None else idx.tz_convert(JST)
    work.index = idx

    # Yahoo 5分足は通常「足の開始時刻」。前引けは11:25足、大引けは15:25足を最終足として要求。
    cutoff = dtime(11, 30) if session == "noon" else dtime(15, 30)
    minimum_time = dtime(11, 25) if session == "noon" else dtime(15, 25)
    work = work[(work.index.date == target_date) & (work.index.time <= cutoff)]
    if work.empty:
        return None

    last_bar_time = work.index[-1].time()
    intraday_fresh = last_bar_time >= minimum_time

    for column in ["Open", "High", "Low", "Close", "Volume"]:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    close_series = work["Close"].dropna()
    open_series = work["Open"].dropna()
    if close_series.empty or open_series.empty:
        return None

    return {
        "open": float(open_series.iloc[0]),
        "high": float(work["High"].max()),
        "low": float(work["Low"].min()),
        "close": float(close_series.iloc[-1]),
        "volume": int(work["Volume"].fillna(0).sum()),
        "last_bar": work.index[-1].isoformat(),
        "intraday_fresh": intraday_fresh,
        "expected_minimum_time": minimum_time.strftime("%H:%M"),
    }


def partial_period_ohlc(completed_daily, current_day, start_date, today):
    dates = local_dates(completed_daily)
    prior_part = completed_daily.loc[((dates >= start_date) & (dates < today)).values]
    highs = pd.to_numeric(prior_part["High"], errors="coerce").dropna().tolist() + [current_day["high"]]
    lows = pd.to_numeric(prior_part["Low"], errors="coerce").dropna().tolist() + [current_day["low"]]
    return {"high": max(highs), "low": min(lows), "close": current_day["close"]}


def analyze_symbol(row, intraday_frame, daily_frame, weekly_frame, monthly_frame, session, today):
    current_day = partial_intraday_bar(intraday_frame, today, session)
    if current_day is None:
        return None, "intraday_missing"

    completed_daily = history_before(daily_frame, today)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    completed_weekly = history_before(weekly_frame, week_start)
    completed_monthly = history_before(monthly_frame, month_start)
    if completed_daily.empty:
        return None, "daily_history_missing"
    if completed_weekly.empty:
        return None, "weekly_history_missing"
    if completed_monthly.empty:
        return None, "monthly_history_missing"

    current_week = partial_period_ohlc(completed_daily, current_day, week_start, today)
    current_month = partial_period_ohlc(completed_daily, current_day, month_start, today)

    context = {
        "D": (numeric_series(completed_daily, "Close"), current_day["close"], current_day["low"], current_day["high"]),
        "W": (numeric_series(completed_weekly, "Close"), current_week["close"], current_week["low"], current_week["high"]),
        "M": (numeric_series(completed_monthly, "Close"), current_month["close"], current_month["low"], current_month["high"]),
    }

    states, score, fail_mask, status_code = {}, 0, 0, 0
    for index, (key, label, length, timeframe) in enumerate(SPECS):
        completed_closes, current_close, candle_low, candle_high = context[timeframe]
        ma, previous_ma = ma_current_and_previous(completed_closes, current_close, length)
        state = classify_state(ma, previous_ma, candle_low, candle_high)
        if state < 0:
            return None, f"ma_history_insufficient:{key}"
        passed = state in (7, 8)
        score += int(passed)
        if not passed:
            fail_mask |= 1 << index
        status_code += state * (9 ** index)
        states[key] = {
            "label": label, "state": state, "state_label": STATE_LABELS[state], "pass": passed,
            "ma": round(ma, 4), "ma_prev": round(previous_ma, 4),
            "gap_pct": round((current_close - ma) / ma * 100, 4),
        }

    all_ma_above = all(x["state"] in (2, 5, 8) for x in states.values())
    non_upward_ma = [states[key]["label"] for key, _, _, _ in SPECS if states[key]["state"] in (2, 5)]

    item = {
        "code": str(row.code), "name": row.name, "market": row.market, "ticker": row.ticker,
        "universe_rank": int(row.rank), "previous_day_volume": int(row.volume),
        "price": round(current_day["close"], 4), "intraday_volume": current_day["volume"],
        "last_bar": current_day["last_bar"],
        "intraday_fresh": current_day["intraday_fresh"],
        "expected_minimum_time": current_day["expected_minimum_time"],
        "candle": {
            "day": {"open": round(current_day["open"], 4), "high": round(current_day["high"], 4),
                    "low": round(current_day["low"], 4), "close": round(current_day["close"], 4)},
            "week": {"high": round(current_week["high"], 4), "low": round(current_week["low"], 4),
                     "close": round(current_week["close"], 4)},
            "month": {"high": round(current_month["high"], 4), "low": round(current_month["low"], 4),
                      "close": round(current_month["close"], 4)},
        },
        "score": score, "tier": 3 if score == 9 else 2 if score == 8 else 1 if score == 7 else 0,
        "all_ma_above": all_ma_above, "non_upward_ma": non_upward_ma,
        "status_code": status_code, "fail_mask": fail_mask, "states": states,
        "failures": [f"{states[key]['label']} {states[key]['state_label']}"
                     for key, _, _, _ in SPECS if not states[key]["pass"]],
        "important_touches": [states[key]["label"] for key in IMPORTANT_TOUCHES if states[key]["state"] == 7],
    }
    return item, None


def state_changes(noon_item, close_item):
    changes = []
    for key, label, _, _ in SPECS:
        a = noon_item["states"][key]
        b = close_item["states"][key]
        if a["state"] != b["state"]:
            changes.append({
                "key": key, "label": label,
                "noon_state": a["state"], "noon_state_label": a["state_label"],
                "close_state": b["state"], "close_state_label": b["state_label"],
                "noon_ma": a["ma"], "close_ma": b["ma"],
                "noon_gap_pct": a["gap_pct"], "close_gap_pct": b["gap_pct"],
            })
    return changes


def scan(session: str, universe_path: Path, results_dir: Path) -> None:
    universe = build_top1000_universe(universe_path)
    tickers = universe["ticker"].tolist()
    today = datetime.now(JST).date()

    intraday = download_many(tickers, period="1d", interval="5m")
    daily = download_many(tickers, period="6mo", interval="1d")
    weekly = download_many(tickers, period="2y", interval="1wk")
    monthly = download_many(tickers, period="10y", interval="1mo")

    all_results, analysis_failures = [], []
    for row in universe.itertuples(index=False):
        item, reason = analyze_symbol(
            row, intraday.get(row.ticker), daily.get(row.ticker),
            weekly.get(row.ticker), monthly.get(row.ticker), session, today
        )
        if item is None:
            analysis_failures.append({
                "code": str(row.code), "name": row.name, "ticker": row.ticker,
                "universe_rank": int(row.rank), "reason": reason,
            })
        else:
            all_results.append(item)

    candidates = [x for x in all_results if x["score"] >= 7]
    all_ma_above_candidates = [x for x in all_results if x["all_ma_above"]]
    candidates.sort(key=lambda x: (-x["score"], x["universe_rank"], x["code"]))
    all_ma_above_candidates.sort(key=lambda x: (-x["score"], x["universe_rank"], x["code"]))
    all_results.sort(key=lambda x: x["universe_rank"])

    current_day_intraday_count = 0
    for frame in intraday.values():
        if frame is None or frame.empty:
            continue
        idx = pd.to_datetime(frame.index)
        idx = idx.tz_localize(JST) if getattr(idx, "tz", None) is None else idx.tz_convert(JST)
        if any(idx.date == today):
            current_day_intraday_count += 1

    score_distribution = {str(i): 0 for i in range(10)}
    last_bar_distribution = {}
    invalid_ohlc = []
    for x in all_results:
        score_distribution[str(x["score"])] += 1
        bar = datetime.fromisoformat(x["last_bar"]).strftime("%H:%M")
        last_bar_distribution[bar] = last_bar_distribution.get(bar, 0) + 1
        for tf in ("day", "week", "month"):
            c = x["candle"][tf]
            if c["low"] > c["high"]:
                invalid_ohlc.append({"code": x["code"], "timeframe": tf, "low": c["low"], "high": c["high"]})

    expected_last_bar = "11:25" if session == "noon" else "15:25"
    stale_last_bar = []
    for x in all_results:
        if not x["intraday_fresh"]:
            stale_last_bar.append({
                "code": x["code"],
                "name": x["name"],
                "last_bar": x["last_bar"],
                "expected_minimum_time": x["expected_minimum_time"],
            })

    dropped_from_noon = []
    impossible_low_increases = []
    noon_comparison = None

    if session == "close":
        noon_path = results_dir / "latest_noon.json"
        if noon_path.exists():
            try:
                noon_doc = json.loads(noon_path.read_text(encoding="utf-8"))
                if noon_doc.get("target_date") == today.isoformat() and noon_doc.get("session") == "noon":
                    close_map = {x["code"]: x for x in all_results}
                    noon_candidates = noon_doc.get("candidates", [])
                    for n in noon_candidates:
                        c = close_map.get(n["code"])
                        if c is None:
                            dropped_from_noon.append({
                                "code": n["code"], "name": n["name"], "noon_score": n["score"],
                                "close_score": None, "reason": "close_analysis_unavailable",
                            })
                        elif c["score"] < 7:
                            dropped_from_noon.append({
                                "code": n["code"], "name": n["name"],
                                "noon_score": n["score"], "close_score": c["score"],
                                "noon_price": n["price"], "close_price": c["price"],
                                "close_last_bar": c["last_bar"],
                                "close_failures": c["failures"],
                                "changed_states": state_changes(n, c),
                            })

                    # schema v3以降のnoon診断情報があれば、Lowの単調性も検査。
                    noon_symbols = {
                        x["code"]: x for x in noon_doc.get("diagnostics", {}).get("symbols", [])
                    }
                    for code, n in noon_symbols.items():
                        c = close_map.get(code)
                        if c is None:
                            continue
                        for tf in ("day", "week", "month"):
                            nlow = n.get("candle", {}).get(tf, {}).get("low")
                            clow = c["candle"][tf]["low"]
                            # 大引けまで期間を延ばしてLowが上昇することはない。
                            if nlow is not None and clow > nlow + 1e-9:
                                impossible_low_increases.append({
                                    "code": code, "name": c["name"], "timeframe": tf,
                                    "noon_low": nlow, "close_low": clow,
                                })

                    noon_comparison = {
                        "source_generated_at_jst": noon_doc.get("generated_at_jst"),
                        "noon_candidate_count": len(noon_candidates),
                        "retained_score_7_plus": sum(
                            1 for n in noon_candidates
                            if close_map.get(n["code"]) is not None and close_map[n["code"]]["score"] >= 7
                        ),
                        "dropped_below_7": sum(x.get("close_score") is not None for x in dropped_from_noon),
                        "close_analysis_unavailable": sum(x.get("close_score") is None for x in dropped_from_noon),
                    }
                else:
                    noon_comparison = {"status": "not_comparable", "reason": "date_or_session_mismatch"}
            except Exception as exc:
                noon_comparison = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        else:
            noon_comparison = {"status": "not_available"}

    quality_issues = []
    if len(all_results) < len(universe):
        quality_issues.append(f"analysis_missing={len(universe)-len(all_results)}")
    if stale_last_bar:
        quality_issues.append(f"stale_last_bar={len(stale_last_bar)}")
    if invalid_ohlc:
        quality_issues.append(f"invalid_ohlc={len(invalid_ohlc)}")
    if impossible_low_increases:
        quality_issues.append(f"impossible_noon_to_close_low_increase={len(impossible_low_increases)}")

    # diagnostics.symbols は全解析銘柄。候補外（Score 0～6）も保存する。
    diagnostic_symbols = [{
        "code": x["code"], "name": x["name"], "ticker": x["ticker"],
        "universe_rank": x["universe_rank"], "price": x["price"],
        "intraday_volume": x["intraday_volume"], "last_bar": x["last_bar"],
        "intraday_fresh": x["intraday_fresh"],
        "expected_minimum_time": x["expected_minimum_time"],
        "score": x["score"], "tier": x["tier"], "all_ma_above": x["all_ma_above"],
        "candle": x["candle"], "failures": x["failures"], "states": x["states"],
    } for x in all_results]

    document = {
        "schema_version": 4,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "target_date": today.isoformat(),
        "session": session,
        "session_label": "前引け確定版" if session == "noon" else "大引け確定版",
        "market_status": "open_data_available" if current_day_intraday_count > 0 else "closed_or_data_unavailable",
        "result_reliability": "diagnostic_warning" if quality_issues else "normal",
        "data_source": "Yahoo Finance via yfinance (unofficial/free; delay or missing data may occur)",
        "universe_definition": "東証プライム・スタンダード・グロースの内国普通株から、前営業日出来高上位1000銘柄",
        "universe_as_of": str(universe["as_of"].iloc[0]),
        "universe_count": len(universe),
        "counts": {
            "9_of_9": sum(x["score"] == 9 for x in candidates),
            "8_of_9": sum(x["score"] == 8 for x in candidates),
            "7_of_9": sum(x["score"] == 7 for x in candidates),
            "all_ma_above": len(all_ma_above_candidates),
        },
        "coverage": {
            "current_day_intraday": current_day_intraday_count,
            "intraday_downloaded": len(intraday),
            "daily_downloaded": len(daily),
            "weekly_downloaded": len(weekly),
            "monthly_downloaded": len(monthly),
            "analyzed": len(all_results),
            "analysis_failed": len(analysis_failures),
        },
        "data_quality": {
            "quality_status": "warning" if quality_issues else "ok",
            "issues": quality_issues,
            "expected_last_bar": expected_last_bar,
            "stale_last_bar_count": len(stale_last_bar),
            "invalid_ohlc_count": len(invalid_ohlc),
            "impossible_noon_to_close_low_increase_count": len(impossible_low_increases),
        },
        "diagnostics": {
            "score_distribution": score_distribution,
            "last_bar_distribution": dict(sorted(last_bar_distribution.items())),
            "analysis_failures": analysis_failures,
            "stale_last_bar_symbols": stale_last_bar,
            "invalid_ohlc": invalid_ohlc,
            "impossible_noon_to_close_low_increases": impossible_low_increases,
            "noon_comparison": noon_comparison,
            "dropped_from_noon": dropped_from_noon,
            "symbols": diagnostic_symbols,
        },
        "candidates": candidates,
        "all_ma_above_candidates": all_ma_above_candidates,
    }

    results_dir.mkdir(parents=True, exist_ok=True)
    latest_path = results_dir / f"latest_{session}.json"
    latest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_dir = results_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    archive_path = archive_dir / f"{today}_{session}.json"
    archive_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    log(
        f"{session}: 9/9={document['counts']['9_of_9']} "
        f"8/9={document['counts']['8_of_9']} 7/9={document['counts']['7_of_9']} "
        f"all-above={document['counts']['all_ma_above']} "
        f"analyzed={len(all_results)}/{len(universe)} "
        f"quality={document['data_quality']['quality_status']}"
    )


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    universe_parser = subparsers.add_parser("update-universe")
    universe_parser.add_argument("--output", default="data/universe_top1000.csv")
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--session", choices=["noon", "close"], required=True)
    scan_parser.add_argument("--universe", default="data/universe_top1000.csv")
    scan_parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()
    if args.command == "update-universe":
        build_top1000_universe(Path(args.output))
    else:
        scan(args.session, Path(args.universe), Path(args.results_dir))


if __name__ == "__main__":
    main()
