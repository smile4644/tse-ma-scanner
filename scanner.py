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
USER_AGENT = "tse-ma-scanner/1.0 (+https://github.com/smile4644/tse-ma-scanner)"

SPECS = [
    ("d5", "日5", 5, "D"),
    ("d25", "日25", 25, "D"),
    ("d75", "日75", 75, "D"),
    ("w13", "週13", 13, "W"),
    ("w26", "週26", 26, "W"),
    ("w52", "週52", 52, "W"),
    ("m12", "月12", 12, "M"),
    ("m24", "月24", 24, "M"),
    ("m60", "月60", 60, "M"),
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
    eligible = {
        "プライム（内国株式）",
        "スタンダード（内国株式）",
        "グロース（内国株式）",
    }
    df = df[df["market"].isin(eligible) & df["code"].notna()].copy()
    df["ticker"] = df["code"] + ".T"
    return df.drop_duplicates("ticker").reset_index(drop=True)


def chunked(items: list[str], size: int = 100):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def split_yf_download(data: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
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
                frame = None
            if frame is not None:
                frame = frame.dropna(how="all")
                if not frame.empty:
                    result[ticker] = frame
    elif len(tickers) == 1:
        frame = data.dropna(how="all")
        if not frame.empty:
            result[tickers[0]] = frame
    return result


def download_many(tickers: list[str], period: str, interval: str, batch_size: int = 100) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    batches = list(chunked(tickers, batch_size))

    for batch_no, batch in enumerate(batches, start=1):
        last_error = None
        for attempt in range(3):
            try:
                data = yf.download(
                    batch,
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    prepost=False,
                    threads=True,
                    progress=False,
                    timeout=30,
                )
                result.update(split_yf_download(data, batch))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))

        if last_error is not None:
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


def history_before(frame: pd.DataFrame | None, cutoff_date) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    dates = local_dates(frame)
    return frame.loc[(dates < cutoff_date).values].copy()


def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def ma_current_and_previous(completed_closes: pd.Series, current_close: float, length: int):
    completed = pd.to_numeric(completed_closes, errors="coerce").dropna()
    if len(completed) < length:
        return None, None
    current_values = pd.concat([completed, pd.Series([current_close])], ignore_index=True).iloc[-length:]
    return float(current_values.mean()), float(completed.iloc[-length:].mean())


def classify_state(ma, previous_ma, candle_low: float, candle_high: float) -> int:
    if ma is None or previous_ma is None:
        return -1
    direction = 2 if ma > previous_ma else 0 if ma < previous_ma else 1
    position = 2 if candle_low > ma else 0 if candle_high < ma else 1
    return direction * 3 + position


def partial_intraday_bar(frame: pd.DataFrame | None, target_date, session: str):
    if frame is None or frame.empty:
        return None

    work = frame.copy()
    idx = pd.to_datetime(work.index)
    idx = idx.tz_localize(JST) if getattr(idx, "tz", None) is None else idx.tz_convert(JST)
    work.index = idx

    end_time = dtime(11, 30) if session == "noon" else dtime(15, 30)
    minimum_time = dtime(11, 25) if session == "noon" else dtime(15, 25)
    work = work[(work.index.date == target_date) & (work.index.time <= end_time)]

    if work.empty or work.index[-1].time() < minimum_time:
        return None

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
    }


def partial_period_ohlc(completed_daily: pd.DataFrame, current_day: dict, start_date, today):
    dates = local_dates(completed_daily)
    prior_part = completed_daily.loc[((dates >= start_date) & (dates < today)).values]
    highs = pd.to_numeric(prior_part["High"], errors="coerce").dropna().tolist() + [current_day["high"]]
    lows = pd.to_numeric(prior_part["Low"], errors="coerce").dropna().tolist() + [current_day["low"]]
    return {"high": max(highs), "low": min(lows), "close": current_day["close"]}


def analyze_symbol(row, intraday_frame, daily_frame, weekly_frame, monthly_frame, session: str, today):
    current_day = partial_intraday_bar(intraday_frame, today, session)
    if current_day is None:
        return None

    completed_daily = history_before(daily_frame, today)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    completed_weekly = history_before(weekly_frame, week_start)
    completed_monthly = history_before(monthly_frame, month_start)

    if completed_daily.empty or completed_weekly.empty or completed_monthly.empty:
        return None

    current_week = partial_period_ohlc(completed_daily, current_day, week_start, today)
    current_month = partial_period_ohlc(completed_daily, current_day, month_start, today)

    context = {
        "D": (numeric_series(completed_daily, "Close"), current_day["close"], current_day["low"], current_day["high"]),
        "W": (numeric_series(completed_weekly, "Close"), current_week["close"], current_week["low"], current_week["high"]),
        "M": (numeric_series(completed_monthly, "Close"), current_month["close"], current_month["low"], current_month["high"]),
    }

    states = {}
    score = 0
    fail_mask = 0
    status_code = 0

    for index, (key, label, length, timeframe) in enumerate(SPECS):
        completed_closes, current_close, candle_low, candle_high = context[timeframe]
        ma, previous_ma = ma_current_and_previous(completed_closes, current_close, length)
        state = classify_state(ma, previous_ma, candle_low, candle_high)
        if state < 0:
            return None

        passed = state in (7, 8)
        score += int(passed)
        if not passed:
            fail_mask |= 1 << index
        status_code += state * (9 ** index)

        states[key] = {
            "label": label,
            "state": state,
            "state_label": STATE_LABELS[state],
            "pass": passed,
            "ma": round(ma, 4),
            "ma_prev": round(previous_ma, 4),
            "gap_pct": round((current_close - ma) / ma * 100, 4),
        }

    if score < 7:
        return None

    return {
        "code": str(row.code),
        "name": row.name,
        "market": row.market,
        "ticker": row.ticker,
        "universe_rank": int(row.rank),
        "previous_day_volume": int(row.volume),
        "price": round(current_day["close"], 4),
        "intraday_volume": current_day["volume"],
        "last_bar": current_day["last_bar"],
        "score": score,
        "tier": 3 if score == 9 else 2 if score == 8 else 1,
        "status_code": status_code,
        "fail_mask": fail_mask,
        "states": states,
        "failures": [
            f"{states[key]['label']} {states[key]['state_label']}"
            for key, _, _, _ in SPECS
            if not states[key]["pass"]
        ],
        "important_touches": [
            states[key]["label"]
            for key in IMPORTANT_TOUCHES
            if states[key]["state"] == 7
        ],
    }


def scan(session: str, universe_path: Path, results_dir: Path) -> None:
    universe = build_top1000_universe(universe_path)
    tickers = universe["ticker"].tolist()
    today = datetime.now(JST).date()

    intraday = download_many(tickers, period="1d", interval="5m")
    daily = download_many(tickers, period="6mo", interval="1d")
    weekly = download_many(tickers, period="2y", interval="1wk")
    monthly = download_many(tickers, period="10y", interval="1mo")

    candidates = []
    for row in universe.itertuples(index=False):
        item = analyze_symbol(
            row,
            intraday.get(row.ticker),
            daily.get(row.ticker),
            weekly.get(row.ticker),
            monthly.get(row.ticker),
            session,
            today,
        )
        if item is not None:
            candidates.append(item)

    candidates.sort(key=lambda item: (-item["score"], item["universe_rank"], item["code"]))

    current_day_intraday_count = 0
    for frame in intraday.values():
        if frame is None or frame.empty:
            continue
        idx = pd.to_datetime(frame.index)
        idx = idx.tz_localize(JST) if getattr(idx, "tz", None) is None else idx.tz_convert(JST)
        if any(idx.date == today):
            current_day_intraday_count += 1

    document = {
        "schema_version": 1,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "target_date": today.isoformat(),
        "session": session,
        "session_label": "前引け確定版" if session == "noon" else "大引け確定版",
        "market_status": "open_data_available" if current_day_intraday_count > 0 else "closed_or_data_unavailable",
        "data_source": "Yahoo Finance via yfinance (unofficial/free; delay or missing data may occur)",
        "universe_definition": "東証プライム・スタンダード・グロースの内国普通株から、前営業日出来高上位1000銘柄",
        "universe_as_of": str(universe["as_of"].iloc[0]),
        "universe_count": len(universe),
        "counts": {
            "9_of_9": sum(item["score"] == 9 for item in candidates),
            "8_of_9": sum(item["score"] == 8 for item in candidates),
            "7_of_9": sum(item["score"] == 7 for item in candidates),
        },
        "coverage": {
            "current_day_intraday": current_day_intraday_count,
            "intraday_downloaded": len(intraday),
            "daily_downloaded": len(daily),
            "weekly_downloaded": len(weekly),
            "monthly_downloaded": len(monthly),
        },
        "candidates": candidates,
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
        f"8/9={document['counts']['8_of_9']} "
        f"7/9={document['counts']['7_of_9']}"
    )


def main() -> None:
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
