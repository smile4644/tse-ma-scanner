from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import scipy  # yfinance repair=True が利用する依存関係
import yfinance as yf

JST = ZoneInfo("Asia/Tokyo")
JPX_MASTER_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
USER_AGENT = "tse-ma-scanner/1.4 (+github)"

# ---------- safety thresholds ----------
EXPECTED_UNIVERSE_COUNT = 1000
MIN_MASTER_COUNT = 3000
MIN_MASTER_DAILY_DOWNLOAD_RATIO = 0.85
MAX_UNIVERSE_AGE_DAYS = 7

MIN_INTRADAY_CURRENT_COUNT = 900
MIN_FRESH_INTRADAY_COUNT = 850
MIN_DAILY_10Y_DOWNLOAD_COUNT = 950
# partial_scored を含む「1本以上のMAを判定できた銘柄」の最低数。
MIN_SCORED_COUNT = 800

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
MA_KEYS = [x[0] for x in SPECS]
MA_LABELS = [x[1] for x in SPECS]

STATE_LABELS = {
    0: "↓下", 1: "↓接触", 2: "↓上",
    3: "→下", 4: "→接触", 5: "→上",
    6: "↑下", 7: "↑接触", 8: "↑上",
}
IMPORTANT_TOUCHES = {"d25", "d75", "w13", "w26", "m12"}


def log(message: str) -> None:
    print(f"[{datetime.now(JST).isoformat(timespec='seconds')}] {message}", flush=True)


def fail(message: str) -> None:
    log(f"FATAL: {message}")
    raise RuntimeError(message)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def atomic_write_json(path: Path, document: dict) -> None:
    """0 byte・壊れたJSONで既存結果を置換しない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())

    # 置換前に必ず再読込して構文を検証する。
    with tmp.open("r", encoding="utf-8") as f:
        json.load(f)

    if tmp.stat().st_size <= 2:
        tmp.unlink(missing_ok=True)
        fail(f"JSON atomic validation failed: {path}")

    tmp.replace(path)


def atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.to_csv(tmp, index=False)
    check = pd.read_csv(tmp, dtype={"code": str, "ticker": str})
    if len(check) != len(frame):
        tmp.unlink(missing_ok=True)
        fail(f"CSV atomic validation failed: expected={len(frame)} actual={len(check)}")
    tmp.replace(path)


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
    response = requests.get(
        JPX_MASTER_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
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
    df = df.drop_duplicates("ticker").reset_index(drop=True)

    if len(df) < MIN_MASTER_COUNT:
        fail(f"JPX master too small: {len(df)} < {MIN_MASTER_COUNT}")

    return df


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


def download_many(
    tickers: list[str],
    period: str,
    interval: str,
    batch_size: int = 100,
) -> dict[str, pd.DataFrame]:
    """全価格系列を auto_adjust=True, repair=True で統一。"""
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
                    auto_adjust=True,
                    repair=True,
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
            log(
                f"{interval} batch {batch_no}/{len(batches)} failed: "
                f"{type(last_error).__name__}: {last_error}"
            )
        if batch_no % 5 == 0 or batch_no == len(batches):
            log(
                f"{interval}: {batch_no}/{len(batches)} batches, "
                f"{len(result)}/{len(tickers)} tickers"
            )
    return result


def local_dates(frame: pd.DataFrame) -> pd.Series:
    idx = pd.to_datetime(frame.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(JST).tz_localize(None)
    return pd.Series(idx.date, index=frame.index)


def build_top1000_universe(output_path: Path) -> tuple[pd.DataFrame, dict]:
    """東証内国普通株の前営業日出来高上位1000銘柄。"""
    master = load_jpx_master()
    master_tickers = master["ticker"].tolist()
    daily = download_many(master_tickers, period="10d", interval="1d")
    today = datetime.now(JST).date()

    minimum_master_downloads = math.ceil(
        len(master_tickers) * MIN_MASTER_DAILY_DOWNLOAD_RATIO
    )
    if len(daily) < minimum_master_downloads:
        fail(
            "Universe source download coverage too low: "
            f"{len(daily)}/{len(master_tickers)} "
            f"(required >= {minimum_master_downloads})"
        )

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
            rows.append((
                row.code,
                row.name,
                row.market,
                row.ticker,
                prior_dates.iloc[-1],
                int(volume),
            ))

    if not rows:
        fail("No previous-trading-day volume rows were created.")

    latest_as_of = max(row[4] for row in rows)
    latest_rows = [row for row in rows if row[4] == latest_as_of]
    if len(latest_rows) < EXPECTED_UNIVERSE_COUNT:
        fail(
            "Latest trading date does not have enough stocks: "
            f"as_of={latest_as_of} rows={len(latest_rows)} < {EXPECTED_UNIVERSE_COUNT}"
        )

    age_days = (today - latest_as_of).days
    if age_days < 0 or age_days > MAX_UNIVERSE_AGE_DAYS:
        fail(
            "Universe as_of is stale or invalid: "
            f"today={today} as_of={latest_as_of} age_days={age_days}"
        )

    universe = pd.DataFrame(
        latest_rows,
        columns=["code", "name", "market", "ticker", "date", "volume"],
    )
    universe = (
        universe
        .sort_values(["volume", "code"], ascending=[False, True])
        .head(EXPECTED_UNIVERSE_COUNT)
        .reset_index(drop=True)
    )

    if len(universe) != EXPECTED_UNIVERSE_COUNT:
        fail(f"Universe count invalid: {len(universe)} != {EXPECTED_UNIVERSE_COUNT}")
    if universe["ticker"].duplicated().any():
        fail("Universe contains duplicate tickers.")
    if universe["volume"].isna().any():
        fail("Universe contains missing volume.")

    universe.insert(0, "rank", np.arange(1, EXPECTED_UNIVERSE_COUNT + 1))
    universe.insert(0, "as_of", latest_as_of.isoformat())
    atomic_write_csv(output_path, universe)

    meta = {
        "jpx_master_count": len(master),
        "master_daily_downloaded": len(daily),
        "master_daily_required_min": minimum_master_downloads,
        "latest_as_of": latest_as_of.isoformat(),
        "latest_as_of_rows": len(latest_rows),
        "universe_count": len(universe),
        "universe_age_days": age_days,
    }
    log(
        f"Universe {latest_as_of}: {len(universe)}銘柄 "
        f"(source={len(daily)}/{len(master_tickers)})"
    )
    return universe, meta


def history_before(frame: pd.DataFrame | None, cutoff_date: date) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    dates = local_dates(frame)
    return frame.loc[(dates < cutoff_date).values].copy()


def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def completed_period_closes(daily_frame: pd.DataFrame, period: str) -> pd.Series:
    if daily_frame is None or daily_frame.empty:
        return pd.Series(dtype=float)
    closes = pd.to_numeric(daily_frame["Close"], errors="coerce")
    idx = pd.to_datetime(daily_frame.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(JST).tz_localize(None)
    temp = pd.DataFrame({"close": closes.to_numpy()}, index=idx).dropna()
    if temp.empty:
        return pd.Series(dtype=float)
    if period == "W":
        group_key = temp.index.to_period("W-FRI")
    elif period == "M":
        group_key = temp.index.to_period("M")
    else:
        raise ValueError(period)
    return temp.groupby(group_key)["close"].last()


def ma_current_and_previous(
    completed_closes: pd.Series,
    current_close: float,
    length: int,
):
    completed = pd.to_numeric(completed_closes, errors="coerce").dropna()
    # 方向判定には前回MAも必要なので completed が length 本必要。
    if len(completed) < length:
        return None, None
    current_values = pd.concat(
        [completed.reset_index(drop=True), pd.Series([current_close])],
        ignore_index=True,
    ).iloc[-length:]
    return (
        float(current_values.mean()),
        float(completed.iloc[-length:].mean()),
    )


def classify_state(ma, previous_ma, candle_low: float, candle_high: float) -> int:
    if ma is None or previous_ma is None:
        return -1
    direction = 2 if ma > previous_ma else 0 if ma < previous_ma else 1
    position = 2 if candle_low > ma else 0 if candle_high < ma else 1
    return direction * 3 + position


def intraday_bar(
    frame: pd.DataFrame | None,
    target_date: date,
    session: str,
):
    if frame is None or frame.empty:
        return None

    work = frame.copy()
    idx = pd.to_datetime(work.index)
    idx = (
        idx.tz_localize(JST)
        if getattr(idx, "tz", None) is None
        else idx.tz_convert(JST)
    )
    work.index = idx

    cutoff = dtime(11, 30) if session == "noon" else dtime(15, 30)
    expected_minimum = dtime(11, 25) if session == "noon" else dtime(15, 20)
    work = work[
        (work.index.date == target_date)
        & (work.index.time <= cutoff)
    ]
    if work.empty:
        return None

    for column in ["Open", "High", "Low", "Close", "Volume"]:
        if column in work.columns:
            work[column] = pd.to_numeric(work[column], errors="coerce")

    close_series = work["Close"].dropna()
    open_series = work["Open"].dropna()
    high_series = work["High"].dropna()
    low_series = work["Low"].dropna()
    if close_series.empty or open_series.empty or high_series.empty or low_series.empty:
        return None

    last_bar_time = work.index[-1].time()
    return {
        "open": float(open_series.iloc[0]),
        "high": float(high_series.max()),
        "low": float(low_series.min()),
        "close": float(close_series.iloc[-1]),
        "volume": int(work["Volume"].fillna(0).sum()) if "Volume" in work.columns else 0,
        "last_bar": work.index[-1].isoformat(),
        "fresh": last_bar_time >= expected_minimum,
        "expected_minimum_time": expected_minimum.strftime("%H:%M"),
    }


def current_daily_bar(frame: pd.DataFrame | None, target_date: date):
    """当日1日足。判定には使わずclose時の照合用。"""
    if frame is None or frame.empty:
        return None
    dates = local_dates(frame)
    mask = dates == target_date
    if not mask.any():
        return None
    row = frame.loc[mask.values].iloc[-1]
    values = {}
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in frame.columns:
            values[col] = pd.to_numeric(pd.Series([row[col]]), errors="coerce").iloc[0]
    if any(pd.isna(values.get(col)) for col in ("Open", "High", "Low", "Close")):
        return None
    return {
        "open": float(values["Open"]),
        "high": float(values["High"]),
        "low": float(values["Low"]),
        "close": float(values["Close"]),
        "volume": int(values.get("Volume", 0)) if pd.notna(values.get("Volume", 0)) else 0,
    }


def partial_period_ohlc(
    completed_daily: pd.DataFrame,
    current_day: dict,
    start_date: date,
    today: date,
):
    dates = local_dates(completed_daily)
    prior_part = completed_daily.loc[
        ((dates >= start_date) & (dates < today)).values
    ]
    highs = (
        pd.to_numeric(prior_part["High"], errors="coerce").dropna().tolist()
        + [current_day["high"]]
    )
    lows = (
        pd.to_numeric(prior_part["Low"], errors="coerce").dropna().tolist()
        + [current_day["low"]]
    )
    return {
        "high": max(highs),
        "low": min(lows),
        "close": current_day["close"],
    }


def base_unscored(row, reason: str, session: str, last_bar=None):
    return {
        "code": str(row.code),
        "name": row.name,
        "market": row.market,
        "ticker": row.ticker,
        "universe_rank": int(row.rank),
        "previous_day_volume": int(row.volume),
        "price": None,
        "price_source": None,
        "intraday_close": None,
        "intraday_volume": None,
        "daily_volume": None,
        "daily_reference": None,
        "last_bar": last_bar,
        "intraday_fresh": False,
        "expected_minimum_time": "11:25" if session == "noon" else "15:20",
        "score": None,
        "score_denominator": 0,
        "score_label": None,
        "available_ma_count": 0,
        "missing_ma_count": len(SPECS),
        "missing_ma": MA_LABELS.copy(),
        "analysis_status": "unscored",
        "analysis_failure_reason": reason,
        "tier": 0,
        "all_ma_above": False,
        "available_ma_above": False,
        "non_upward_ma": [],
        "candle": None,
        "states": {},
        "failures": [],
        "important_touches": [],
    }


def analyze_symbol(
    row,
    intraday_frame,
    daily_frame,
    session: str,
    today: date,
):
    intra = intraday_bar(intraday_frame, today, session)
    daily_today = current_daily_bar(daily_frame, today)

    # noon / close の判定用当日OHLCは必ず同じ5分足系列から作る。
    # 当日1日足は close 時の照合用だけに保持する。
    if intra is None:
        return base_unscored(row, "current_day_price_missing", session)

    current_day = {
        "open": intra["open"],
        "high": intra["high"],
        "low": intra["low"],
        "close": intra["close"],
        "volume": intra["volume"],
        "source": "intraday_5m",
        "last_bar": intra["last_bar"],
        "intraday_fresh": intra["fresh"],
        "expected_minimum_time": intra["expected_minimum_time"],
        "intraday_close": intra["close"],
    }

    completed_daily = history_before(daily_frame, today)
    if completed_daily.empty:
        item = base_unscored(
            row,
            "daily_history_missing",
            session,
            last_bar=current_day["last_bar"],
        )
        item.update({
            "price": round(current_day["close"], 4),
            "price_source": current_day["source"],
            "intraday_close": round(current_day["intraday_close"], 4),
            "intraday_volume": intra["volume"],
            "daily_volume": current_day["volume"],
            "daily_reference": daily_today,
            "intraday_fresh": current_day["intraday_fresh"],
        })
        return item

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    completed_before_week = history_before(daily_frame, week_start)
    completed_before_month = history_before(daily_frame, month_start)
    completed_weekly_closes = completed_period_closes(completed_before_week, "W")
    completed_monthly_closes = completed_period_closes(completed_before_month, "M")

    current_week = partial_period_ohlc(
        completed_daily,
        current_day,
        week_start,
        today,
    )
    current_month = partial_period_ohlc(
        completed_daily,
        current_day,
        month_start,
        today,
    )

    base = {
        "code": str(row.code),
        "name": row.name,
        "market": row.market,
        "ticker": row.ticker,
        "universe_rank": int(row.rank),
        "previous_day_volume": int(row.volume),
        "price": round(current_day["close"], 4),
        "price_source": current_day["source"],
        "intraday_close": round(current_day["intraday_close"], 4),
        "intraday_volume": intra["volume"],
        "daily_volume": current_day["volume"],
        "daily_reference": (
            {
                "open": round(daily_today["open"], 4),
                "high": round(daily_today["high"], 4),
                "low": round(daily_today["low"], 4),
                "close": round(daily_today["close"], 4),
                "volume": daily_today["volume"],
            }
            if daily_today is not None
            else None
        ),
        "last_bar": current_day["last_bar"],
        "intraday_fresh": current_day["intraday_fresh"],
        "expected_minimum_time": current_day["expected_minimum_time"],
        "candle": {
            "day": {
                "open": round(current_day["open"], 4),
                "high": round(current_day["high"], 4),
                "low": round(current_day["low"], 4),
                "close": round(current_day["close"], 4),
            },
            "week": {
                "high": round(current_week["high"], 4),
                "low": round(current_week["low"], 4),
                "close": round(current_week["close"], 4),
            },
            "month": {
                "high": round(current_month["high"], 4),
                "low": round(current_month["low"], 4),
                "close": round(current_month["close"], 4),
            },
        },
    }

    context = {
        "D": (
            numeric_series(completed_daily, "Close"),
            current_day["close"],
            current_day["low"],
            current_day["high"],
        ),
        "W": (
            completed_weekly_closes,
            current_week["close"],
            current_week["low"],
            current_week["high"],
        ),
        "M": (
            completed_monthly_closes,
            current_month["close"],
            current_month["low"],
            current_month["high"],
        ),
    }

    states = {}
    score = 0
    fail_mask = 0
    status_code = 0

    for index, (key, label, length, timeframe) in enumerate(SPECS):
        completed_closes, current_close, candle_low, candle_high = context[timeframe]
        ma, previous_ma = ma_current_and_previous(
            completed_closes,
            current_close,
            length,
        )
        state = classify_state(ma, previous_ma, candle_low, candle_high)

        # 上場後の期間不足は銘柄全体を未採点にしない。
        # 存在するMAだけを判定し、分母を available_ma_count にする。
        if state < 0:
            continue

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

    if not states:
        item = base_unscored(
            row,
            "ma_history_insufficient:all",
            session,
            last_bar=current_day["last_bar"],
        )
        item.update(base)
        return item

    available_ma_count = len(states)
    missing_ma = [label for key, label, _, _ in SPECS if key not in states]
    full_ma_set = available_ma_count == len(SPECS)

    # ④「全時間軸MA上」は9本完備銘柄だけ。
    all_ma_above = (
        full_ma_set
        and all(states[key]["state"] in (2, 5, 8) for key in MA_KEYS)
    )
    # 履歴不足銘柄向け。存在するMAがすべてローソク足より下ならTrue。
    available_ma_above = all(
        x["state"] in (2, 5, 8)
        for x in states.values()
    )
    non_upward_ma = [
        states[key]["label"]
        for key in MA_KEYS
        if key in states and states[key]["state"] in (2, 5)
    ]

    return {
        **base,
        "score": score,
        "score_denominator": available_ma_count,
        "score_label": f"{score}/{available_ma_count}",
        "available_ma_count": available_ma_count,
        "missing_ma_count": len(missing_ma),
        "missing_ma": missing_ma,
        "analysis_status": "scored" if full_ma_set else "partial_scored",
        "analysis_failure_reason": None,
        "tier": (
            3 if full_ma_set and score == 9
            else 2 if full_ma_set and score == 8
            else 1 if full_ma_set and score == 7
            else 0
        ),
        "all_ma_above": all_ma_above,
        "available_ma_above": available_ma_above,
        "non_upward_ma": non_upward_ma,
        "status_code": status_code,
        "fail_mask": fail_mask,
        "states": states,
        "failures": [
            f"{states[key]['label']} {states[key]['state_label']}"
            for key in MA_KEYS
            if key in states and not states[key]["pass"]
        ],
        "important_touches": [
            states[key]["label"]
            for key in MA_KEYS
            if key in IMPORTANT_TOUCHES
            and key in states
            and states[key]["state"] == 7
        ],
    }


def state_changes(noon_item: dict, close_item: dict) -> list[dict]:
    changes = []
    if close_item.get("score") is None:
        return changes
    for key, label, _, _ in SPECS:
        if key not in noon_item.get("states", {}) or key not in close_item.get("states", {}):
            continue
        a = noon_item["states"][key]
        b = close_item["states"][key]
        if a["state"] != b["state"]:
            changes.append({
                "key": key,
                "label": label,
                "noon_state": a["state"],
                "noon_state_label": a["state_label"],
                "close_state": b["state"],
                "close_state_label": b["state_label"],
                "noon_ma": a["ma"],
                "close_ma": b["ma"],
                "noon_gap_pct": a["gap_pct"],
                "close_gap_pct": b["gap_pct"],
            })
    return changes


def validate_scan_before_commit(
    *,
    universe: pd.DataFrame,
    intraday_downloaded: int,
    daily_downloaded: int,
    current_day_intraday_count: int,
    fresh_intraday_count: int,
    scored_count: int,
    diagnostic_count: int,
    invalid_ohlc_count: int,
) -> list[str]:
    """latest/archiveを書いてよいか判定。Low逆転は隔離対象で、全体停止しない。"""
    errors: list[str] = []
    if len(universe) != EXPECTED_UNIVERSE_COUNT:
        errors.append(f"universe_count={len(universe)} != {EXPECTED_UNIVERSE_COUNT}")
    if diagnostic_count != EXPECTED_UNIVERSE_COUNT:
        errors.append(
            f"diagnostic_count={diagnostic_count} != {EXPECTED_UNIVERSE_COUNT}"
        )
    if intraday_downloaded < MIN_INTRADAY_CURRENT_COUNT:
        errors.append(
            f"intraday_downloaded={intraday_downloaded} < {MIN_INTRADAY_CURRENT_COUNT}"
        )
    if current_day_intraday_count < MIN_INTRADAY_CURRENT_COUNT:
        errors.append(
            f"current_day_intraday={current_day_intraday_count} < {MIN_INTRADAY_CURRENT_COUNT}"
        )
    if fresh_intraday_count < MIN_FRESH_INTRADAY_COUNT:
        errors.append(
            f"fresh_intraday={fresh_intraday_count} < {MIN_FRESH_INTRADAY_COUNT}"
        )
    if daily_downloaded < MIN_DAILY_10Y_DOWNLOAD_COUNT:
        errors.append(
            f"daily_10y_downloaded={daily_downloaded} < {MIN_DAILY_10Y_DOWNLOAD_COUNT}"
        )
    if scored_count < MIN_SCORED_COUNT:
        errors.append(f"scored={scored_count} < {MIN_SCORED_COUNT}")
    if invalid_ohlc_count > 0:
        errors.append(f"invalid_ohlc={invalid_ohlc_count}")
    return errors


def compact_diagnostic_symbol(x: dict, quarantined: bool) -> dict:
    candle = x.get("candle") or {}
    states = x.get("states", {})
    return {
        "code": x["code"],
        "name": x["name"],
        "ticker": x["ticker"],
        "universe_rank": x["universe_rank"],
        "price": x.get("price"),
        "price_source": x.get("price_source"),
        "intraday_close": x.get("intraday_close"),
        "intraday_volume": x.get("intraday_volume"),
        "daily_volume": x.get("daily_volume"),
        "last_bar": x.get("last_bar"),
        "intraday_fresh": x.get("intraday_fresh"),
        "expected_minimum_time": x.get("expected_minimum_time"),
        "score": x.get("score"),
        "score_denominator": x.get("score_denominator"),
        "score_label": x.get("score_label"),
        "available_ma_count": x.get("available_ma_count"),
        "missing_ma": x.get("missing_ma", []),
        "analysis_status": x.get("analysis_status"),
        "analysis_failure_reason": x.get("analysis_failure_reason"),
        "tier": x.get("tier"),
        "all_ma_above": x.get("all_ma_above"),
        "available_ma_above": x.get("available_ma_above"),
        "quarantined": quarantined,
        "day_low": (candle.get("day") or {}).get("low"),
        "day_high": (candle.get("day") or {}).get("high"),
        "week_low": (candle.get("week") or {}).get("low"),
        "week_high": (candle.get("week") or {}).get("high"),
        "month_low": (candle.get("month") or {}).get("low"),
        "month_high": (candle.get("month") or {}).get("high"),
        "state_vector": [states.get(k, {}).get("state") for k in MA_KEYS],
        "ma_vector": [states.get(k, {}).get("ma") for k in MA_KEYS],
        "ma_prev_vector": [states.get(k, {}).get("ma_prev") for k in MA_KEYS],
        "gap_pct_vector": [states.get(k, {}).get("gap_pct") for k in MA_KEYS],
        "failures": x.get("failures", []),
    }


def scan(session: str, universe_path: Path, results_dir: Path) -> None:
    universe, universe_meta = build_top1000_universe(universe_path)
    tickers = universe["ticker"].tolist()
    today = datetime.now(JST).date()

    intraday = download_many(tickers, period="1d", interval="5m")
    daily = download_many(tickers, period="10y", interval="1d")

    all_symbols = []
    for row in universe.itertuples(index=False):
        all_symbols.append(
            analyze_symbol(
                row,
                intraday.get(row.ticker),
                daily.get(row.ticker),
                session,
                today,
            )
        )
    all_symbols.sort(key=lambda x: x["universe_rank"])

    scored = [x for x in all_symbols if x.get("score") is not None]
    fully_scored = [
        x for x in scored
        if x.get("available_ma_count") == len(SPECS)
    ]
    partial_scored = [
        x for x in scored
        if 0 < x.get("available_ma_count", 0) < len(SPECS)
    ]
    unscored = [x for x in all_symbols if x.get("score") is None]

    # ①〜③は9本完備銘柄だけ。勝手な抜粋はせず配列に全件格納。
    candidates = [x for x in fully_scored if x["score"] >= 7]
    # ④も9本完備かつ全9MAよりローソク足Lowが上の銘柄を全件。
    all_ma_above_candidates = [
        x for x in fully_scored if x.get("all_ma_above")
    ]
    # 履歴不足は別枠。存在するMAをすべて判定した結果を全件格納。
    partial_ma_candidates = list(partial_scored)

    candidates.sort(
        key=lambda x: (-x["score"], x["universe_rank"], x["code"])
    )
    all_ma_above_candidates.sort(
        key=lambda x: (-x["score"], x["universe_rank"], x["code"])
    )
    partial_ma_candidates.sort(
        key=lambda x: (
            -(x["score"] / max(x["available_ma_count"], 1)),
            -x["available_ma_count"],
            x["universe_rank"],
            x["code"],
        )
    )

    score_distribution = {str(i): 0 for i in range(10)}
    score_distribution["NA"] = len(unscored)
    for x in scored:
        score_distribution[str(x["score"])] += 1

    score_fraction_distribution = dict(sorted(Counter(
        x.get("score_label") or "NA" for x in all_symbols
    ).items()))
    available_ma_distribution = dict(sorted(Counter(
        str(x.get("available_ma_count", 0)) for x in all_symbols
    ).items(), key=lambda kv: int(kv[0])))

    last_bar_distribution: dict[str, int] = {}
    intraday_missing_codes = []
    stale_last_bar = []
    for x in all_symbols:
        if x.get("last_bar") is None:
            intraday_missing_codes.append({"code": x["code"], "name": x["name"]})
            last_bar_distribution["missing"] = last_bar_distribution.get("missing", 0) + 1
            continue
        bar = datetime.fromisoformat(x["last_bar"]).strftime("%H:%M")
        last_bar_distribution[bar] = last_bar_distribution.get(bar, 0) + 1
        if not x.get("intraday_fresh", False):
            stale_last_bar.append({
                "code": x["code"],
                "name": x["name"],
                "last_bar": x["last_bar"],
                "expected_minimum_time": x["expected_minimum_time"],
            })

    current_day_intraday_count = EXPECTED_UNIVERSE_COUNT - len(intraday_missing_codes)
    fresh_intraday_count = current_day_intraday_count - len(stale_last_bar)

    # 1日足は判定には使わず、5分足終値との照合だけに使う。
    close_daily_reference_count = 0
    close_source_mismatch = []
    if session == "close":
        for x in all_symbols:
            ref = x.get("daily_reference")
            if ref is None:
                continue
            close_daily_reference_count += 1
            if x.get("intraday_close") is None or ref.get("close") in (None, 0):
                continue
            diff_pct = (
                (ref["close"] - x["intraday_close"])
                / ref["close"]
                * 100
            )
            if abs(diff_pct) >= 1.0:
                close_source_mismatch.append({
                    "code": x["code"],
                    "name": x["name"],
                    "daily_close": ref["close"],
                    "intraday_close": x["intraday_close"],
                    "diff_pct": round(diff_pct, 4),
                })

    invalid_ohlc = []
    for x in all_symbols:
        candle = x.get("candle")
        if not candle:
            continue
        for tf in ("day", "week", "month"):
            c = candle.get(tf)
            if c is not None and c["low"] > c["high"]:
                invalid_ohlc.append({
                    "code": x["code"],
                    "name": x["name"],
                    "timeframe": tf,
                    "low": c["low"],
                    "high": c["high"],
                })

    dropped_from_noon = []
    impossible_low_increases = []
    noon_comparison = None

    if session == "close":
        noon_path = results_dir / "latest_noon.json"
        if noon_path.exists():
            try:
                noon_doc = json.loads(noon_path.read_text(encoding="utf-8"))
                same_date = noon_doc.get("target_date") == today.isoformat()
                correct_session = noon_doc.get("session") == "noon"
                source_schema = int(noon_doc.get("schema_version", 0) or 0)
                comparison_compatible = (
                    same_date and correct_session and source_schema >= 5
                )

                if comparison_compatible:
                    close_map = {x["code"]: x for x in all_symbols}
                    noon_candidates = noon_doc.get("candidates", [])
                    noon_symbols = {
                        x["code"]: x
                        for x in noon_doc.get("diagnostics", {}).get("symbols", [])
                    }

                    # v6はcandle、v7 compact diagnosticsはday_low/week_low/month_low。
                    for code, n in noon_symbols.items():
                        c = close_map.get(code)
                        if c is None or not c.get("candle"):
                            continue
                        for tf in ("day", "week", "month"):
                            if n.get("candle"):
                                nlow = (n.get("candle", {}).get(tf) or {}).get("low")
                            else:
                                nlow = n.get(f"{tf}_low")
                            clow = (c.get("candle", {}).get(tf) or {}).get("low")
                            if nlow is None or clow is None:
                                continue
                            if clow > nlow + 1e-9:
                                impossible_low_increases.append({
                                    "code": code,
                                    "name": c["name"],
                                    "timeframe": tf,
                                    "noon_low": nlow,
                                    "close_low": clow,
                                    "noon_last_bar": n.get("last_bar"),
                                    "close_last_bar": c.get("last_bar"),
                                    "close_price_source": c.get("price_source"),
                                })

                    quarantine_codes_pre = {
                        x["code"] for x in impossible_low_increases
                    }

                    for n in noon_candidates:
                        c = close_map.get(n["code"])
                        if c is None:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n.get("score"),
                                "close_score": None,
                                "reason": "close_analysis_unavailable",
                            })
                            continue
                        if n["code"] in quarantine_codes_pre:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n.get("score"),
                                "close_score": c.get("score"),
                                "reason": "quarantined_noon_to_close_low_increase",
                                "close_last_bar": c.get("last_bar"),
                            })
                            continue
                        if c.get("score") is None:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n.get("score"),
                                "close_score": None,
                                "reason": c.get("analysis_failure_reason"),
                            })
                        elif c.get("available_ma_count") != 9 or c["score"] < 7:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n.get("score"),
                                "close_score": c.get("score"),
                                "close_score_label": c.get("score_label"),
                                "noon_price": n.get("price"),
                                "close_price": c.get("price"),
                                "close_price_source": c.get("price_source"),
                                "close_last_bar": c.get("last_bar"),
                                "close_failures": c.get("failures", []),
                                "changed_states": state_changes(n, c),
                            })

                    retained = 0
                    for n in noon_candidates:
                        c = close_map.get(n["code"])
                        if (
                            c is not None
                            and n["code"] not in quarantine_codes_pre
                            and c.get("available_ma_count") == 9
                            and c.get("score") is not None
                            and c["score"] >= 7
                        ):
                            retained += 1

                    noon_comparison = {
                        "status": "comparable",
                        "source_schema_version": source_schema,
                        "source_generated_at_jst": noon_doc.get("generated_at_jst"),
                        "noon_candidate_count": len(noon_candidates),
                        "retained_score_7_plus": retained,
                        "dropped_or_quarantined": len(dropped_from_noon),
                    }
                else:
                    noon_comparison = {
                        "status": "not_comparable",
                        "reason": "date_session_or_schema_mismatch",
                        "source_schema_version": source_schema,
                        "source_target_date": noon_doc.get("target_date"),
                        "source_session": noon_doc.get("session"),
                    }
            except Exception as exc:
                noon_comparison = {
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        else:
            noon_comparison = {"status": "not_available"}

    # Low逆転は銘柄単位で隔離。全体のclose結果は残す。
    quarantine_codes = {x["code"] for x in impossible_low_increases}
    quarantined_symbols = [
        x for x in all_symbols if x["code"] in quarantine_codes
    ]
    if quarantine_codes:
        candidates = [
            x for x in candidates if x["code"] not in quarantine_codes
        ]
        all_ma_above_candidates = [
            x for x in all_ma_above_candidates
            if x["code"] not in quarantine_codes
        ]
        partial_ma_candidates = [
            x for x in partial_ma_candidates
            if x["code"] not in quarantine_codes
        ]

    quality_issues = []
    if unscored:
        quality_issues.append(f"unscored={len(unscored)}")
    if stale_last_bar:
        quality_issues.append(f"stale_last_bar={len(stale_last_bar)}")
    if invalid_ohlc:
        quality_issues.append(f"invalid_ohlc={len(invalid_ohlc)}")
    if quarantine_codes:
        quality_issues.append(
            f"quarantined_noon_to_close_low_increase={len(quarantine_codes)}"
        )
    if close_source_mismatch:
        quality_issues.append(
            f"daily_vs_intraday_close_diff_ge_1pct={len(close_source_mismatch)}"
        )

    analysis_failures = [
        {
            "code": x["code"],
            "name": x["name"],
            "ticker": x["ticker"],
            "universe_rank": x["universe_rank"],
            "reason": x.get("analysis_failure_reason"),
        }
        for x in unscored
    ]

    diagnostic_symbols = [
        compact_diagnostic_symbol(x, x["code"] in quarantine_codes)
        for x in all_symbols
    ]

    counts = {
        "9_of_9": sum(x["score"] == 9 for x in candidates),
        "8_of_9": sum(x["score"] == 8 for x in candidates),
        "7_of_9": sum(x["score"] == 7 for x in candidates),
        "all_ma_above": len(all_ma_above_candidates),
        "partial_ma": len(partial_ma_candidates),
        "partial_available_ma_above": sum(
            bool(x.get("available_ma_above"))
            for x in partial_ma_candidates
        ),
        "quarantined": len(quarantine_codes),
    }

    document = {
        "schema_version": 7,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "target_date": today.isoformat(),
        "session": session,
        "session_label": "前引け確定版" if session == "noon" else "大引け確定版",
        "market_status": (
            "open_data_available"
            if current_day_intraday_count > 0
            else "closed_or_data_unavailable"
        ),
        "result_reliability": "diagnostic_warning" if quality_issues else "normal",
        "data_source": (
            "Yahoo Finance via yfinance; auto_adjust=True, repair=True. "
            "判定用当日OHLCはnoon/closeとも5分足から生成。"
            "日・週・月MAは同一の調整済み日足から生成。"
            "当日1日足はclose時の照合専用。"
        ),
        "universe_definition": (
            "東証プライム・スタンダード・グロースの"
            "内国普通株から、前営業日出来高上位1000銘柄"
        ),
        "universe_as_of": str(universe["as_of"].iloc[0]),
        "universe_count": len(universe),
        "universe_build": universe_meta,
        "ma_order": MA_KEYS,
        "ma_labels": MA_LABELS,
        "counts": counts,
        "coverage": {
            "current_day_intraday": current_day_intraday_count,
            "fresh_intraday": fresh_intraday_count,
            "intraday_downloaded": len(intraday),
            "daily_10y_downloaded": len(daily),
            "diagnostic_symbols": len(diagnostic_symbols),
            "scored": len(scored),
            "fully_scored_9_ma": len(fully_scored),
            "partial_scored": len(partial_scored),
            "unscored": len(unscored),
            "close_daily_reference": (
                close_daily_reference_count if session == "close" else None
            ),
        },
        "data_quality": {
            "quality_status": "warning" if quality_issues else "ok",
            "issues": quality_issues,
            "expected_last_bar": "11:25" if session == "noon" else "15:20",
            "stale_last_bar_count": len(stale_last_bar),
            "intraday_missing_count": len(intraday_missing_codes),
            "invalid_ohlc_count": len(invalid_ohlc),
            "impossible_noon_to_close_low_increase_count": len(impossible_low_increases),
            "quarantined_symbol_count": len(quarantine_codes),
            "daily_vs_intraday_close_diff_ge_1pct_count": len(close_source_mismatch),
        },
        "diagnostics": {
            "score_distribution": score_distribution,
            "score_fraction_distribution": score_fraction_distribution,
            "available_ma_distribution": available_ma_distribution,
            "last_bar_distribution": dict(sorted(last_bar_distribution.items())),
            "analysis_failures": analysis_failures,
            "intraday_missing_symbols": intraday_missing_codes,
            "stale_last_bar_symbols": stale_last_bar,
            "invalid_ohlc": invalid_ohlc,
            "daily_vs_intraday_close_diff_ge_1pct": close_source_mismatch,
            "impossible_noon_to_close_low_increases": impossible_low_increases,
            "quarantined_symbols": [
                {
                    "code": x["code"],
                    "name": x["name"],
                    "reason": "noon_to_close_low_increase",
                    "last_bar": x.get("last_bar"),
                    "candle": x.get("candle"),
                }
                for x in quarantined_symbols
            ],
            "noon_comparison": noon_comparison,
            "dropped_from_noon": dropped_from_noon,
            "symbols": diagnostic_symbols,
        },
        # ①〜③。9/9,8/9,7/9の全件を保持。
        "candidates": candidates,
        # ④。全時間軸MA上（方向不問）の全件を保持。
        "all_ma_above_candidates": all_ma_above_candidates,
        # 上場後の履歴不足銘柄。存在MAでx/y判定した全件。
        "partial_ma_candidates": partial_ma_candidates,
    }

    gate_errors = validate_scan_before_commit(
        universe=universe,
        intraday_downloaded=len(intraday),
        daily_downloaded=len(daily),
        current_day_intraday_count=current_day_intraday_count,
        fresh_intraday_count=fresh_intraday_count,
        scored_count=len(scored),
        diagnostic_count=len(diagnostic_symbols),
        invalid_ohlc_count=len(invalid_ohlc),
    )

    # 成否にかかわらず「今回の試行」をresults/diagnosticsへ保存する。
    # scannerが0終了するため、現行workflowのgit add results/で診断もコミットされる。
    diagnostics_dir = results_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    attempt_document = {
        **document,
        "write_status": "blocked" if gate_errors else "accepted",
        "safety_gate_errors": gate_errors,
    }
    atomic_write_json(
        diagnostics_dir / f"latest_{session}_attempt.json",
        attempt_document,
    )
    atomic_write_json(
        diagnostics_dir / f"{today}_{session}_attempt.json",
        attempt_document,
    )

    if gate_errors:
        log(
            "Safety gate blocked latest/archive overwrite; diagnostics saved: "
            + "; ".join(gate_errors)
        )
        return

    log(
        "Safety gate passed: "
        f"session={session} universe={len(universe)} "
        f"intraday={current_day_intraday_count} fresh={fresh_intraday_count} "
        f"daily10y={len(daily)} scored={len(scored)}"
    )

    results_dir.mkdir(parents=True, exist_ok=True)
    latest_path = results_dir / f"latest_{session}.json"
    archive_dir = results_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{today}_{session}.json"

    atomic_write_json(latest_path, document)
    atomic_write_json(archive_path, document)

    log(
        f"{session}: "
        f"9/9={counts['9_of_9']} "
        f"8/9={counts['8_of_9']} "
        f"7/9={counts['7_of_9']} "
        f"all-above={counts['all_ma_above']} "
        f"partial={counts['partial_ma']} "
        f"quarantined={counts['quarantined']} "
        f"scored={len(scored)}/{len(universe)} "
        f"quality={document['data_quality']['quality_status']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    universe_parser = subparsers.add_parser("update-universe")
    universe_parser.add_argument(
        "--output",
        default="data/universe_top1000.csv",
    )

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument(
        "--session",
        choices=["noon", "close"],
        required=True,
    )
    scan_parser.add_argument(
        "--universe",
        default="data/universe_top1000.csv",
    )
    scan_parser.add_argument(
        "--results-dir",
        default="results",
    )

    args = parser.parse_args()
    if args.command == "update-universe":
        universe, meta = build_top1000_universe(Path(args.output))
        log(
            "Universe update completed: "
            f"{len(universe)} rows, as_of={meta['latest_as_of']}"
        )
    else:
        scan(
            args.session,
            Path(args.universe),
            Path(args.results_dir),
        )


if __name__ == "__main__":
    main()
