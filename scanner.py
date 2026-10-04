from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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
USER_AGENT = "tse-ma-scanner/1.7 (+github)"
SCHEMA_VERSION = 13
PRICE_SERIES_MODE = "normal_close_auto_adjust_false"

# ---------- safety thresholds ----------
MIN_MASTER_COUNT = 3000
MIN_MASTER_DAILY_DOWNLOAD_RATIO = 0.85
MAX_UNIVERSE_AGE_DAYS = 7

# 流動性ユニバース。固定件数ではなく絶対的な売買代金基準で選ぶ。
LIQUIDITY_LOOKBACK_DAYS = 20
MIN_LIQUIDITY_OBSERVATIONS = 15
MIN_MEDIAN_20D_TRADING_VALUE = 100_000_000.0
MIN_PREVIOUS_DAY_TRADING_VALUE = 50_000_000.0
MIN_LIQUID_UNIVERSE_COUNT = 700

# universeが可変件数になるため、品質ゲートは比率で判定する。
MIN_INTRADAY_DOWNLOAD_RATIO = 0.90
MIN_FRESH_INTRADAY_RATIO = 0.85
MIN_DAILY_10Y_DOWNLOAD_RATIO = 0.95
MIN_SCORED_RATIO = 0.80

# ---------- output screening ----------
MAX_CANDIDATE_PRICE = 3500.0
FUNDAMENTAL_CACHE_FILENAME = "fundamental_eps_cache.json"
MIN_FUNDAMENTAL_AVAILABLE_RATIO = 0.50
TDNET_DOCUMENT_LIMIT = 12
TDNET_CACHE_DIR = ".cache/tdnet"

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

# ---------- MA proximity / early-stage screening ----------
# 「初動」は9本すべてから近いことを要求せず、短中期MAの密集度と、
# 重要MA（日75・週26・月12）への近接を重視する。
EARLY_STAGE_KEYS = ("d5", "d25", "d75", "w13", "w26", "m12")
EARLY_STAGE_IMPORTANT_KEYS = ("d75", "w26", "m12")
EARLY_STAGE_SCORE_MIN = 7
EARLY_STAGE_NEAR_2_PCT = 2.0
EARLY_STAGE_NEAR_5_PCT = 5.0
EARLY_STAGE_MIN_NEAR_5_COUNT = 3
EARLY_STAGE_MAX_CORE_GAP_PCT = 10.0


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


def noon_comparison_compatible(noon_doc: dict, effective_date: date) -> bool:
    """前引け・大引け比較は同一schema・同一価格系列だけ許可する。"""
    try:
        source_schema = int(noon_doc.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        noon_doc.get("target_date") == effective_date.isoformat()
        and noon_doc.get("session") == "noon"
        and source_schema == SCHEMA_VERSION
        and noon_doc.get("price_series_mode") == PRICE_SERIES_MODE
    )


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
    auto_adjust: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    価格系列を一括取得。

    MA判定は auto_adjust=False のYahoo Close/OHLCを使う。
    Yahooの通常Closeは株式分割を反映する一方、配当によるAdj Close補正を
    混ぜないため、証券会社チャートの移動平均線に近い基準になる。
    """
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
                    auto_adjust=auto_adjust,
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


def build_liquidity_universe(
    output_path: Path,
    target_date: date | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    東証内国普通株から継続的に売買代金が確保されている銘柄を抽出する。

    条件:
      1. 過去20営業日の売買代金中央値 >= 1億円
      2. 前営業日の売買代金 >= 5,000万円
      3. 20営業日のうち最低15営業日分の有効データ

    売買代金 = Yahoo Close × Volume。
    配当によるAdj Close補正を混ぜないため、universe構築も
    auto_adjust=False の通常Closeを使う。
    """
    master = load_jpx_master()
    master_tickers = master["ticker"].tolist()
    daily = download_many(
        master_tickers,
        period="3mo",
        interval="1d",
        auto_adjust=False,
    )
    cutoff_date = target_date or datetime.now(JST).date()

    minimum_master_downloads = math.ceil(
        len(master_tickers) * MIN_MASTER_DAILY_DOWNLOAD_RATIO
    )
    if len(daily) < minimum_master_downloads:
        fail(
            "Universe source download coverage too low: "
            f"{len(daily)}/{len(master_tickers)} "
            f"(required >= {minimum_master_downloads})"
        )

    latest_dates = []
    prepared: dict[str, pd.DataFrame] = {}
    for row in master.itertuples(index=False):
        frame = daily.get(row.ticker)
        if frame is None or frame.empty:
            continue
        if "Close" not in frame.columns or "Volume" not in frame.columns:
            continue
        dates = local_dates(frame)
        mask = dates < cutoff_date
        if not mask.any():
            continue
        prior = frame.loc[mask.values].copy()
        prior["_local_date"] = dates.loc[mask.values].to_numpy()
        prepared[row.ticker] = prior
        latest_dates.append(prior["_local_date"].iloc[-1])

    if not latest_dates:
        fail("No previous-trading-day data were created for liquidity universe.")

    latest_as_of = max(latest_dates)
    age_days = (cutoff_date - latest_as_of).days
    if age_days < 0 or age_days > MAX_UNIVERSE_AGE_DAYS:
        fail(
            "Universe as_of is stale or invalid: "
            f"target_date={cutoff_date} as_of={latest_as_of} age_days={age_days}"
        )

    rows = []
    source_rows_on_latest_date = 0

    for row in master.itertuples(index=False):
        prior = prepared.get(row.ticker)
        if prior is None or prior.empty:
            continue
        prior = prior[prior["_local_date"] <= latest_as_of].copy()
        if prior.empty or prior["_local_date"].iloc[-1] != latest_as_of:
            continue
        source_rows_on_latest_date += 1

        close = pd.to_numeric(prior["Close"], errors="coerce")
        volume = pd.to_numeric(prior["Volume"], errors="coerce")
        trading_value = (close * volume).replace([np.inf, -np.inf], np.nan)

        valid = pd.DataFrame({
            "date": prior["_local_date"],
            "close": close,
            "volume": volume,
            "trading_value": trading_value,
        }).dropna(subset=["close", "volume", "trading_value"])
        valid = valid[
            (valid["close"] > 0)
            & (valid["volume"] >= 0)
            & (valid["trading_value"] >= 0)
        ]
        if valid.empty:
            continue

        lookback = valid.tail(LIQUIDITY_LOOKBACK_DAYS)
        observations = len(lookback)
        if observations < MIN_LIQUIDITY_OBSERVATIONS:
            continue

        previous = lookback.iloc[-1]
        previous_day_trading_value = float(previous["trading_value"])
        median_20d_trading_value = float(lookback["trading_value"].median())
        mean_20d_trading_value = float(lookback["trading_value"].mean())

        if median_20d_trading_value < MIN_MEDIAN_20D_TRADING_VALUE:
            continue
        if previous_day_trading_value < MIN_PREVIOUS_DAY_TRADING_VALUE:
            continue

        rows.append((
            row.code,
            row.name,
            row.market,
            row.ticker,
            latest_as_of,
            int(previous["volume"]),
            float(previous["close"]),
            int(round(previous_day_trading_value)),
            int(round(median_20d_trading_value)),
            int(round(mean_20d_trading_value)),
            observations,
        ))

    if source_rows_on_latest_date < MIN_LIQUID_UNIVERSE_COUNT:
        fail(
            "Latest trading date source coverage too low: "
            f"as_of={latest_as_of} rows={source_rows_on_latest_date}"
        )

    universe = pd.DataFrame(
        rows,
        columns=[
            "code", "name", "market", "ticker", "date", "volume",
            "previous_day_close", "previous_day_trading_value",
            "median_20d_trading_value", "mean_20d_trading_value",
            "liquidity_observations",
        ],
    )
    universe = universe.sort_values(
        ["median_20d_trading_value", "previous_day_trading_value", "code"],
        ascending=[False, False, True],
    ).reset_index(drop=True)

    if len(universe) < MIN_LIQUID_UNIVERSE_COUNT:
        fail(
            "Liquidity universe unexpectedly small: "
            f"{len(universe)} < {MIN_LIQUID_UNIVERSE_COUNT}"
        )
    if universe["ticker"].duplicated().any():
        fail("Universe contains duplicate tickers.")

    universe.insert(0, "rank", np.arange(1, len(universe) + 1))
    universe.insert(0, "as_of", latest_as_of.isoformat())
    atomic_write_csv(output_path, universe)

    meta = {
        "jpx_master_count": len(master),
        "master_daily_downloaded": len(daily),
        "master_daily_required_min": minimum_master_downloads,
        "latest_as_of": latest_as_of.isoformat(),
        "latest_as_of_source_rows": source_rows_on_latest_date,
        "universe_count": len(universe),
        "universe_age_days": age_days,
        "liquidity_lookback_days": LIQUIDITY_LOOKBACK_DAYS,
        "minimum_liquidity_observations": MIN_LIQUIDITY_OBSERVATIONS,
        "minimum_median_20d_trading_value": int(MIN_MEDIAN_20D_TRADING_VALUE),
        "minimum_previous_day_trading_value": int(MIN_PREVIOUS_DAY_TRADING_VALUE),
        "ranking_metric": "median_20d_trading_value_desc",
    }
    log(
        f"Liquidity universe {latest_as_of}: {len(universe)}銘柄 "
        f"(20d median >= {MIN_MEDIAN_20D_TRADING_VALUE:,.0f}円, "
        f"previous day >= {MIN_PREVIOUS_DAY_TRADING_VALUE:,.0f}円)"
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
        "ma_proximity_2pct_count": 0,
        "ma_proximity_5pct_count": 0,
        "nearest_ma_key": None,
        "nearest_ma_label": None,
        "nearest_ma_gap_pct": None,
        "d25_gap_pct": None,
        "w13_gap_pct": None,
        "early_stage_candidate": False,
        "early_stage_rank": None,
        "early_stage_progressed": False,
        "early_stage_important_near_2pct": [],
        "early_stage_touch_ma": [],
        "early_stage_fresh_breakout_ma": [],
    }


def analyze_symbol(
    row,
    intraday_frame,
    daily_frame,
    session: str,
    target_date: date,
    historical_close: bool = False,
):
    intra = intraday_bar(intraday_frame, target_date, session)
    daily_today = current_daily_bar(daily_frame, target_date)

    # 通常実行のnoon/closeは従来どおり5分足を使う。
    # 過去日のclose再計算だけは、その日の確定日足を判定用OHLCにする。
    if historical_close and daily_today is not None:
        current_day = {
            **daily_today,
            "source": "daily_1d_confirmed",
            "last_bar": target_date.isoformat(),
            "intraday_fresh": True,
            "expected_minimum_time": None,
            "intraday_close": None,
        }
    elif intra is not None:
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
    else:
        return base_unscored(row, "current_day_price_missing", session)

    completed_daily = history_before(daily_frame, target_date)
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
            "intraday_close": (
                round(current_day["intraday_close"], 4)
                if current_day["intraday_close"] is not None else None
            ),
            "intraday_volume": intra["volume"] if intra is not None else None,
            "daily_volume": current_day["volume"],
            "daily_reference": daily_today,
            "intraday_fresh": current_day["intraday_fresh"],
        })
        return item

    week_start = target_date - timedelta(days=target_date.weekday())
    month_start = target_date.replace(day=1)
    completed_before_week = history_before(daily_frame, week_start)
    completed_before_month = history_before(daily_frame, month_start)
    completed_weekly_closes = completed_period_closes(completed_before_week, "W")
    completed_monthly_closes = completed_period_closes(completed_before_month, "M")

    current_week = partial_period_ohlc(
        completed_daily,
        current_day,
        week_start,
        target_date,
    )
    current_month = partial_period_ohlc(
        completed_daily,
        current_day,
        month_start,
        target_date,
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
        "intraday_close": (
            round(current_day["intraday_close"], 4)
            if current_day["intraday_close"] is not None else None
        ),
        "intraday_volume": intra["volume"] if intra is not None else None,
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

    proximity = calculate_early_stage_metrics(
        states=states,
        score=score,
        full_ma_set=full_ma_set,
    )

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
        **proximity,
    }



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


def calculate_early_stage_metrics(
    *,
    states: dict,
    score: int | None,
    full_ma_set: bool,
) -> dict:
    """
    TDK/SWCC型の「MA近接型初動」を定量化する。

    判定対象は日5・25・75、週13・26、月12の6本。
      - 6本中3本以上が現在値から±5%以内
      - 日75・週26・月12のいずれかが±2%以内
      - 日25・週13の乖離がともに±10%以内
      - 9本完備かつScore 7/9以上
    を満たす銘柄を初動候補とする。

    Rank A:
      - 6本中2本以上が±2%以内、または
      - 6本中4本以上が±5%以内かつ重要MAで接触/小幅上抜け
    Rank B:
      - 上記の初動条件は満たすがRank A未満
    """
    focus = {
        key: states[key]
        for key in EARLY_STAGE_KEYS
        if key in states and safe_float(states[key].get("gap_pct")) is not None
    }
    if not focus:
        return {
            "ma_proximity_2pct_count": 0,
            "ma_proximity_5pct_count": 0,
            "nearest_ma_key": None,
            "nearest_ma_label": None,
            "nearest_ma_gap_pct": None,
            "d25_gap_pct": None,
            "w13_gap_pct": None,
            "early_stage_candidate": False,
            "early_stage_rank": None,
            "early_stage_progressed": False,
            "early_stage_important_near_2pct": [],
            "early_stage_touch_ma": [],
            "early_stage_fresh_breakout_ma": [],
        }

    within_2 = [
        key for key, state in focus.items()
        if abs(float(state["gap_pct"])) <= EARLY_STAGE_NEAR_2_PCT
    ]
    within_5 = [
        key for key, state in focus.items()
        if abs(float(state["gap_pct"])) <= EARLY_STAGE_NEAR_5_PCT
    ]
    important_near_2 = [
        key for key in EARLY_STAGE_IMPORTANT_KEYS
        if key in focus
        and abs(float(focus[key]["gap_pct"])) <= EARLY_STAGE_NEAR_2_PCT
    ]
    touch_ma = [
        key for key, state in focus.items()
        if state.get("state") in (1, 4, 7)
    ]
    fresh_breakout_ma = [
        key for key, state in focus.items()
        if state.get("state") == 8
        and 0.0 <= float(state["gap_pct"]) <= EARLY_STAGE_NEAR_2_PCT
    ]

    nearest_key = min(
        focus,
        key=lambda key: abs(float(focus[key]["gap_pct"])),
    )
    nearest = focus[nearest_key]

    d25_gap = safe_float((focus.get("d25") or {}).get("gap_pct"))
    w13_gap = safe_float((focus.get("w13") or {}).get("gap_pct"))
    core_gap_ok = bool(
        d25_gap is not None
        and w13_gap is not None
        and abs(d25_gap) <= EARLY_STAGE_MAX_CORE_GAP_PCT
        and abs(w13_gap) <= EARLY_STAGE_MAX_CORE_GAP_PCT
    )
    progressed = bool(full_ma_set and not core_gap_ok)

    eligible = bool(
        full_ma_set
        and score is not None
        and score >= EARLY_STAGE_SCORE_MIN
        and len(within_5) >= EARLY_STAGE_MIN_NEAR_5_COUNT
        and len(important_near_2) >= 1
        and core_gap_ok
    )
    important_trigger = any(
        key in EARLY_STAGE_IMPORTANT_KEYS
        for key in (touch_ma + fresh_breakout_ma)
    )
    rank = None
    if eligible:
        rank = (
            "A"
            if (
                len(within_2) >= 2
                or (len(within_5) >= 4 and important_trigger)
            )
            else "B"
        )

    return {
        "ma_proximity_2pct_count": len(within_2),
        "ma_proximity_5pct_count": len(within_5),
        "nearest_ma_key": nearest_key,
        "nearest_ma_label": nearest.get("label"),
        "nearest_ma_gap_pct": round(float(nearest["gap_pct"]), 4),
        "d25_gap_pct": round(d25_gap, 4) if d25_gap is not None else None,
        "w13_gap_pct": round(w13_gap, 4) if w13_gap is not None else None,
        "early_stage_candidate": eligible,
        "early_stage_rank": rank,
        "early_stage_progressed": progressed,
        "early_stage_important_near_2pct": [
            focus[key]["label"] for key in important_near_2
        ],
        "early_stage_touch_ma": [
            focus[key]["label"] for key in touch_ma
        ],
        "early_stage_fresh_breakout_ma": [
            focus[key]["label"] for key in fresh_breakout_ma
        ],
    }


def near_9_of_9_candidate(item: dict) -> dict | None:
    """Return a near-9/9 annotation when +1% can make every MA pass."""
    if item.get("available_ma_count") != len(SPECS) or item.get("score") == 9:
        return None
    price = safe_float(item.get("price"))
    if price is None or price <= 0:
        return None
    candle = item.get("candle") or {}
    timeframe_to_candle = {"D": "day", "W": "week", "M": "month"}

    def passes(rise_pct: float) -> bool:
        delta = price * rise_pct / 100.0
        for key, _label, length, timeframe in SPECS:
            state = item["states"][key]
            candle_key = timeframe_to_candle[timeframe]
            tf_candle = candle.get(candle_key)
            if not tf_candle or "low" not in tf_candle or "high" not in tf_candle:
                return False
            simulated_ma = state["ma"] + delta / length
            simulated_high = max(float(tf_candle["high"]), price + delta)
            simulated_low = float(tf_candle["low"])
            if classify_state(
                simulated_ma,
                state["ma_prev"],
                simulated_low,
                simulated_high,
            ) not in (7, 8):
                return False
        return True

    if not passes(1.0):
        return None
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if passes(mid):
            hi = mid
        else:
            lo = mid
    result = dict(item)
    result["near_9_of_9_required_rise_pct"] = round(hi, 6)
    result["near_9_of_9_target_price"] = round(price * (1 + hi / 100.0), 6)
    result["near_9_of_9_from_score"] = item.get("score")
    return result


def near_all_ma_above_candidate(item: dict) -> dict | None:
    """Return the price-level leading indicator for all nine MAs."""
    if item.get("available_ma_count") != len(SPECS) or item.get("all_ma_above"):
        return None
    price = safe_float(item.get("price"))
    if price is None or price <= 0:
        return None
    max_ma = max(item["states"][key]["ma"] for key in MA_KEYS)
    required = max(0.0, (max_ma - price) / price * 100.0)
    if required > 1.0:
        return None
    result = dict(item)
    result["near_all_ma_above_required_rise_pct"] = round(required, 6)
    result["near_all_ma_above_target_price"] = round(max(price, max_ma), 6)
    result["near_all_ma_above_blocking_ma"] = [
        f"{item['states'][key]['label']} {item['states'][key]['state_label']}"
        for key in MA_KEYS
        if item["states"][key]["state"] not in (2, 5, 8)
    ]
    return result


def duration_days(extracted_value) -> int | None:
    if extracted_value is None or extracted_value.item is None:
        return None
    period = extracted_value.item.period
    start_date = getattr(period, "start_date", None)
    end_date = getattr(period, "end_date", None)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days + 1


def extracted_number(extracted_value) -> float | None:
    return safe_float(
        extracted_value.value if extracted_value is not None else None
    )


def filing_date(filing) -> date:
    return pd.to_datetime(filing.pubdate).date()


def fetch_tdnet_earnings(
    tickers: list[str],
    target_date: date,
) -> dict[str, dict]:
    """
    TDnet XBRLから今期会社予想EPSと直近通期実績EPSを取得する。

    四半期短信のcurrent EPSは累計四半期値なので比較に使わず、
    期間300日以上の最新実績だけを前期通期EPSとして採用する。
    """
    if not tickers:
        return {}

    try:
        import tdnet
        from tdnet import CK, extract_values

        tdnet.configure(
            cache_dir=TDNET_CACHE_DIR,
            timeout=60.0,
            max_retries=4,
            rate_limit=0.5,
        )
    except Exception as exc:
        return {
            ticker: {
                "status": "error",
                "improving": None,
                "reason": f"TDnetImportError: {type(exc).__name__}: {exc}",
            }
            for ticker in tickers
        }

    result: dict[str, dict] = {}
    for position, ticker in enumerate(tickers, start=1):
        code = ticker.removesuffix(".T")
        try:
            filings = tdnet.documents(
                code=code,
                has_xbrl=True,
                limit=TDNET_DOCUMENT_LIMIT,
            )
            filings = [
                filing for filing in filings
                if filing_date(filing) <= target_date
            ]
            filings.sort(key=filing_date, reverse=True)

            forecast_eps = None
            forecast_end = None
            forecast_filing = None
            annual_actuals = []

            for filing in filings:
                statements = filing.xbrl()
                if forecast_eps is None:
                    forecast_value = extract_values(
                        statements,
                        [CK.FORECAST_EPS],
                    ).get(CK.FORECAST_EPS)
                    value = extracted_number(forecast_value)
                    period = (
                        forecast_value.item.period
                        if forecast_value is not None else None
                    )
                    period_end = getattr(period, "end_date", None)
                    if value is not None and period_end is not None:
                        forecast_eps = value
                        forecast_end = period_end
                        forecast_filing = filing

                actual_value = extract_values(
                    statements,
                    [CK.EPS],
                    period="current",
                    consolidated=True,
                ).get(CK.EPS)
                if actual_value is None:
                    actual_value = extract_values(
                        statements,
                        [CK.EPS],
                        period="current",
                        consolidated=False,
                    ).get(CK.EPS)
                actual_eps = extracted_number(actual_value)
                actual_period = (
                    actual_value.item.period
                    if actual_value is not None else None
                )
                actual_end = getattr(actual_period, "end_date", None)
                if (
                    actual_eps is not None
                    and actual_end is not None
                    and (duration_days(actual_value) or 0) >= 300
                ):
                    annual_actuals.append((actual_end, actual_eps, filing))
                    if forecast_end is not None and actual_end < forecast_end:
                        break

            eligible_actuals = [
                item for item in annual_actuals
                if forecast_end is None or item[0] < forecast_end
            ]
            eligible_actuals.sort(key=lambda item: item[0], reverse=True)
            prior_actual = eligible_actuals[0] if eligible_actuals else None

            if forecast_eps is None or prior_actual is None:
                result[ticker] = {
                    "status": "unavailable",
                    "improving": None,
                    "reason": (
                        "forecast_eps_missing"
                        if forecast_eps is None
                        else "prior_full_year_eps_missing"
                    ),
                    "current_year_eps_estimate": forecast_eps,
                    "prior_year_eps": (
                        prior_actual[1] if prior_actual is not None else None
                    ),
                }
            else:
                prior_end, prior_eps, prior_filing = prior_actual
                result[ticker] = {
                    "status": "ok",
                    "improving": forecast_eps > prior_eps,
                    "reason": None,
                    "current_year_eps_estimate": round(forecast_eps, 6),
                    "prior_year_eps": round(prior_eps, 6),
                    "earnings_growth": (
                        round((forecast_eps - prior_eps) / abs(prior_eps), 6)
                        if prior_eps != 0 else None
                    ),
                    "forecast_period_end": forecast_end.isoformat(),
                    "prior_period_end": prior_end.isoformat(),
                    "forecast_filing_date": (
                        filing_date(forecast_filing).isoformat()
                    ),
                    "prior_filing_date": filing_date(prior_filing).isoformat(),
                    "source": "TDnet XBRL",
                }
        except Exception as exc:
            result[ticker] = {
                "status": "error",
                "improving": None,
                "reason": f"{type(exc).__name__}: {exc}",
            }

        if position % 50 == 0 or position == len(tickers):
            available = sum(x.get("status") == "ok" for x in result.values())
            log(
                f"fundamentals: {position}/{len(tickers)}, "
                f"available={available}"
            )
    return result


def load_fundamental_cache(path: Path, target_date: date) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema_version") != 4:
            return {}
        if doc.get("target_date") != target_date.isoformat():
            return {}
        entries = doc.get("entries", {})
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}


def fetch_fundamental_screening(
    symbols: list[dict],
    cache_path: Path,
    target_date: date,
) -> tuple[dict[str, dict], dict]:
    """
    価格条件を満たしたMA候補のみをTDnet XBRLから取得する。
    同一日のnoon/close間では成功データと欠損データをキャッシュする。
    transient errorは次回再試行できるようキャッシュしない。
    """
    by_ticker = {x["ticker"]: x for x in symbols}
    cache = load_fundamental_cache(cache_path, target_date)

    result: dict[str, dict] = {}
    missing_tickers = []
    for ticker in by_ticker:
        cached = cache.get(ticker)
        if isinstance(cached, dict) and cached.get("status") in {"ok", "unavailable"}:
            result[ticker] = cached
        else:
            missing_tickers.append(ticker)

    fetched = fetch_tdnet_earnings(missing_tickers, target_date)
    result.update(fetched)

    # errorは保存せず、次回実行時に再取得する。
    cache_entries = {
        ticker: value
        for ticker, value in result.items()
        if value.get("status") in {"ok", "unavailable"}
    }
    cache_doc = {
        "schema_version": 4,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "target_date": target_date.isoformat(),
        "definition": (
            "TDnet XBRL: current-year company forecast EPS "
            "> latest prior full-year actual EPS"
        ),
        "entries": cache_entries,
    }
    atomic_write_json(cache_path, cache_doc)

    available = sum(x.get("status") == "ok" for x in result.values())
    requested = len(by_ticker)
    summary = {
        "requested": requested,
        "cache_hits": requested - len(missing_tickers),
        "fetched": len(missing_tickers),
        "available": available,
        "available_ratio": (
            round(available / requested, 4)
            if requested
            else 1.0
        ),
        "improving": sum(
            x.get("status") == "ok" and x.get("improving") is True
            for x in result.values()
        ),
        "not_improving": sum(
            x.get("status") == "ok" and x.get("improving") is False
            for x in result.values()
        ),
        "unavailable": sum(
            x.get("status") == "unavailable"
            for x in result.values()
        ),
        "errors": sum(
            x.get("status") == "error"
            for x in result.values()
        ),
        "error_reasons": dict(sorted(Counter(
            x.get("reason") or "unknown"
            for x in result.values()
            if x.get("status") == "error"
        ).items())),
        "unavailable_or_error": sum(
            x.get("status") != "ok"
            for x in result.values()
        ),
    }
    return result, summary


def attach_screening_fields(item: dict, fundamental: dict | None) -> dict:
    item["screening"] = {
        "price_limit": MAX_CANDIDATE_PRICE,
        "price_pass": (
            item.get("price") is not None
            and item["price"] <= MAX_CANDIDATE_PRICE
        ),
        "earnings_rule": (
            "current-year company forecast EPS > latest prior full-year "
            "actual EPS (TDnet XBRL)"
        ),
        "earnings_status": (
            fundamental.get("status") if fundamental else "not_checked"
        ),
        "earnings_improving": (
            fundamental.get("improving") if fundamental else None
        ),
        "current_year_eps_estimate": (
            fundamental.get("current_year_eps_estimate")
            if fundamental else None
        ),
        "prior_year_eps": (
            fundamental.get("prior_year_eps")
            if fundamental else None
        ),
        "forecast_period_end": (
            fundamental.get("forecast_period_end")
            if fundamental else None
        ),
        "prior_period_end": (
            fundamental.get("prior_period_end") if fundamental else None
        ),
        "earnings_growth": (
            fundamental.get("earnings_growth")
            if fundamental else None
        ),
        "reason": (
            fundamental.get("reason")
            if fundamental else None
        ),
    }
    return item


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
    historical_close: bool = False,
) -> list[str]:
    """latest/archiveを書いてよいか判定。Low逆転は隔離対象で、全体停止しない。"""
    errors: list[str] = []
    universe_count = len(universe)
    if universe_count < MIN_LIQUID_UNIVERSE_COUNT:
        errors.append(
            f"universe_count={universe_count} < {MIN_LIQUID_UNIVERSE_COUNT}"
        )
    if diagnostic_count != universe_count:
        errors.append(
            f"diagnostic_count={diagnostic_count} != universe_count={universe_count}"
        )

    min_intraday = math.ceil(universe_count * MIN_INTRADAY_DOWNLOAD_RATIO)
    min_fresh = math.ceil(universe_count * MIN_FRESH_INTRADAY_RATIO)
    min_daily = math.ceil(universe_count * MIN_DAILY_10Y_DOWNLOAD_RATIO)
    min_scored = math.ceil(universe_count * MIN_SCORED_RATIO)

    if historical_close:
        if current_day_intraday_count < min_intraday:
            errors.append(
                f"target_daily_bar={current_day_intraday_count} < {min_intraday}"
            )
    else:
        if intraday_downloaded < min_intraday:
            errors.append(f"intraday_downloaded={intraday_downloaded} < {min_intraday}")
        if current_day_intraday_count < min_intraday:
            errors.append(f"current_day_intraday={current_day_intraday_count} < {min_intraday}")
        if fresh_intraday_count < min_fresh:
            errors.append(f"fresh_intraday={fresh_intraday_count} < {min_fresh}")
    if daily_downloaded < min_daily:
        errors.append(f"daily_10y_downloaded={daily_downloaded} < {min_daily}")
    if scored_count < min_scored:
        errors.append(f"scored={scored_count} < {min_scored}")
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
        "ma_proximity_2pct_count": x.get("ma_proximity_2pct_count"),
        "ma_proximity_5pct_count": x.get("ma_proximity_5pct_count"),
        "nearest_ma_key": x.get("nearest_ma_key"),
        "nearest_ma_label": x.get("nearest_ma_label"),
        "nearest_ma_gap_pct": x.get("nearest_ma_gap_pct"),
        "d25_gap_pct": x.get("d25_gap_pct"),
        "w13_gap_pct": x.get("w13_gap_pct"),
        "early_stage_candidate": x.get("early_stage_candidate"),
        "early_stage_rank": x.get("early_stage_rank"),
        "early_stage_progressed": x.get("early_stage_progressed"),
        "early_stage_important_near_2pct": x.get("early_stage_important_near_2pct", []),
        "early_stage_touch_ma": x.get("early_stage_touch_ma", []),
        "early_stage_fresh_breakout_ma": x.get("early_stage_fresh_breakout_ma", []),
    }


def validate_target_date(effective_date: date, run_date: date) -> None:
    """Reject future dates; historical noon and close are reproducible."""
    if effective_date > run_date:
        fail(f"target_date must not be in the future: {effective_date}")


def intraday_period_for(session: str, effective_date: date, run_date: date) -> str:
    """Use enough lookback to retrieve a recent historical noon session."""
    if session == "noon" and effective_date < run_date:
        return "5d"
    return "1d"


def scan(
    session: str,
    universe_path: Path,
    results_dir: Path,
    target_date: date | None = None,
) -> None:
    run_date = datetime.now(JST).date()
    effective_date = target_date or run_date
    if effective_date > run_date:
        fail(f"target_date must not be in the future: {effective_date}")
    historical_close = effective_date < run_date and session == "close"
    if effective_date < run_date and session != "close":
        fail("Past target_date is supported only for the close session.")

    universe, universe_meta = build_liquidity_universe(
        universe_path,
        effective_date,
    )
    tickers = universe["ticker"].tolist()

    intraday = (
        {}
        if historical_close
        else download_many(
            tickers,
            period="1d",
            interval="5m",
            auto_adjust=False,
        )
    )
    daily = download_many(
        tickers,
        period="10y",
        interval="1d",
        auto_adjust=False,
    )

    all_symbols = []
    for row in universe.itertuples(index=False):
        all_symbols.append(
            analyze_symbol(
                row,
                intraday.get(row.ticker),
                daily.get(row.ticker),
                session,
                effective_date,
                historical_close,
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

    # MA条件だけの生候補。監査用に件数を保持し、最終出力には
    # 価格3,500円以下 + 今期EPS予想改善の共通フィルタを適用する。
    raw_candidates = [x for x in fully_scored if x["score"] >= 7]
    raw_all_ma_above_candidates = [
        x for x in fully_scored if x.get("all_ma_above")
    ]
    raw_near_9_of_9_candidates = [
        candidate
        for x in fully_scored
        if (candidate := near_9_of_9_candidate(x)) is not None
    ]
    raw_near_all_ma_above_candidates = [
        candidate
        for x in fully_scored
        if (candidate := near_all_ma_above_candidate(x)) is not None
    ]
    raw_early_stage_candidates = [
        x for x in fully_scored
        if x.get("early_stage_candidate")
    ]
    partial_ma_candidates = list(partial_scored)

    # まず価格条件で絞り、業績取得件数を抑える。
    screening_pool_map = {}
    all_raw_candidate_groups = (
        raw_candidates
        + raw_all_ma_above_candidates
        + raw_near_9_of_9_candidates
        + raw_near_all_ma_above_candidates
        + raw_early_stage_candidates
    )
    for x in all_raw_candidate_groups:
        if x.get("price") is not None and x["price"] <= MAX_CANDIDATE_PRICE:
            screening_pool_map[x["ticker"]] = x
    screening_pool = list(screening_pool_map.values())

    fundamental_cache_path = results_dir / FUNDAMENTAL_CACHE_FILENAME
    fundamental_map, fundamental_summary = fetch_fundamental_screening(
        screening_pool,
        fundamental_cache_path,
        effective_date,
    )

    for x in all_raw_candidate_groups:
        attach_screening_fields(
            x,
            fundamental_map.get(x["ticker"]),
        )

    def final_screen_pass(x: dict) -> bool:
        if x.get("price") is None or x["price"] > MAX_CANDIDATE_PRICE:
            return False
        f = fundamental_map.get(x["ticker"])
        return bool(
            f
            and f.get("status") == "ok"
            and f.get("improving") is True
        )

    candidates = [
        x for x in raw_candidates
        if final_screen_pass(x)
    ]
    all_ma_above_candidates = [
        x for x in raw_all_ma_above_candidates
        if final_screen_pass(x)
    ]
    near_9_of_9_candidates = [
        x for x in raw_near_9_of_9_candidates if final_screen_pass(x)
    ]
    near_all_ma_above_candidates = [
        x for x in raw_near_all_ma_above_candidates if final_screen_pass(x)
    ]
    early_stage_candidates = [
        x for x in raw_early_stage_candidates if final_screen_pass(x)
    ]

    candidates.sort(
        key=lambda x: (-x["score"], x["universe_rank"], x["code"])
    )
    all_ma_above_candidates.sort(
        key=lambda x: (-x["score"], x["universe_rank"], x["code"])
    )
    near_9_of_9_candidates.sort(
        key=lambda x: (x["near_9_of_9_required_rise_pct"], x["universe_rank"], x["code"])
    )
    near_all_ma_above_candidates.sort(
        key=lambda x: (x["near_all_ma_above_required_rise_pct"], x["universe_rank"], x["code"])
    )
    early_stage_candidates.sort(
        key=lambda x: (
            0 if x.get("early_stage_rank") == "A" else 1,
            -x.get("ma_proximity_5pct_count", 0),
            -x.get("ma_proximity_2pct_count", 0),
            (
                abs(x["nearest_ma_gap_pct"])
                if x.get("nearest_ma_gap_pct") is not None
                else 999.0
            ),
            x["universe_rank"],
            x["code"],
        )
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
        bar = (
            "daily"
            if historical_close
            else datetime.fromisoformat(x["last_bar"]).strftime("%H:%M")
        )
        last_bar_distribution[bar] = last_bar_distribution.get(bar, 0) + 1
        if not x.get("intraday_fresh", False):
            stale_last_bar.append({
                "code": x["code"],
                "name": x["name"],
                "last_bar": x["last_bar"],
                "expected_minimum_time": x["expected_minimum_time"],
            })

    current_day_intraday_count = len(universe) - len(intraday_missing_codes)
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
                same_date = noon_doc.get("target_date") == effective_date.isoformat()
                correct_session = noon_doc.get("session") == "noon"
                source_schema = int(noon_doc.get("schema_version", 0) or 0)
                source_price_series_mode = noon_doc.get("price_series_mode")
                comparison_compatible = noon_comparison_compatible(
                    noon_doc, effective_date
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
                        "reason": "date_session_schema_or_price_mode_mismatch",
                        "source_schema_version": source_schema,
                        "expected_schema_version": SCHEMA_VERSION,
                        "source_price_series_mode": source_price_series_mode,
                        "expected_price_series_mode": PRICE_SERIES_MODE,
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
        near_9_of_9_candidates = [
            x for x in near_9_of_9_candidates if x["code"] not in quarantine_codes
        ]
        near_all_ma_above_candidates = [
            x for x in near_all_ma_above_candidates
            if x["code"] not in quarantine_codes
        ]
        early_stage_candidates = [
            x for x in early_stage_candidates
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
    if (
        fundamental_summary.get("requested", 0) > 0
        and fundamental_summary.get("available_ratio", 0.0)
        < MIN_FUNDAMENTAL_AVAILABLE_RATIO
    ):
        quality_issues.append(
            "fundamental_coverage_low="
            f"{fundamental_summary.get('available_ratio', 0.0):.1%}"
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
        "near_9_of_9": len(near_9_of_9_candidates),
        "near_all_ma_above": len(near_all_ma_above_candidates),
        "early_stage": len(early_stage_candidates),
        "partial_ma": len(partial_ma_candidates),
        "partial_available_ma_above": sum(
            bool(x.get("available_ma_above"))
            for x in partial_ma_candidates
        ),
        "quarantined": len(quarantine_codes),
    }

    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "target_date": effective_date.isoformat(),
        "session": session,
        "session_label": "前引け確定版" if session == "noon" else "大引け確定版",
        "market_status": (
            "confirmed_daily_data_available"
            if historical_close and current_day_intraday_count > 0
            else "open_data_available"
            if current_day_intraday_count > 0
            else "closed_or_data_unavailable"
        ),
        "result_reliability": "diagnostic_warning" if quality_issues else "normal",
        "price_series_mode": PRICE_SERIES_MODE,
        "data_source": (
            "Yahoo Finance via yfinance; auto_adjust=False, repair=True. "
            "通常Close/OHLC（株式分割反映・配当Adj Close補正なし）をMAに使用。 "
            + (
                "過去日closeの判定用当日OHLCは確定日足を使用。"
                if historical_close
                else "判定用当日OHLCはnoon/closeとも5分足から生成。"
            )
            + "日・週・月MAは同一の調整済み日足から生成。"
            + ("" if historical_close else "当日1日足はclose時の照合専用。")
        ),
        "universe_definition": (
            "東証プライム・スタンダード・グロースの内国普通株から、"
            "過去20営業日の売買代金中央値1億円以上、"
            "前営業日の売買代金5000万円以上、"
            "かつ20営業日中15営業日以上の有効データがある銘柄"
        ),
        "universe_as_of": str(universe["as_of"].iloc[0]),
        "universe_count": len(universe),
        "universe_build": universe_meta,
        "ma_order": MA_KEYS,
        "ma_labels": MA_LABELS,
        "liquidity_definition": {
            "lookback_days": LIQUIDITY_LOOKBACK_DAYS,
            "minimum_observations": MIN_LIQUIDITY_OBSERVATIONS,
            "minimum_median_20d_trading_value": int(
                MIN_MEDIAN_20D_TRADING_VALUE
            ),
            "minimum_previous_day_trading_value": int(
                MIN_PREVIOUS_DAY_TRADING_VALUE
            ),
            "ranking": "median_20d_trading_value_desc",
            "fixed_universe_count": None,
        },
        "screening_definition": {
            "price_max": MAX_CANDIDATE_PRICE,
            "price_inclusive": True,
            "earnings_improvement": (
                "TDnet XBRL current-year company forecast EPS > latest "
                "prior full-year actual EPS"
            ),
            "earnings_unavailable_policy": "exclude",
            "near_9_of_9": "現在価格から+1%以内の仮想価格で9本すべてがstate 7/8になる最小上昇率",
            "near_all_ma_above": "正式なall_ma_aboveではなく、現在価格から+1%以内で9本の現在MA価格水準を上回れるかを見る価格水準ベース先行指標",
            "early_stage": (
                "9本完備かつ7/9以上。日5・25・75、週13・26、月12の"
                "6本中3本以上が±5%以内、日75・週26・月12の"
                "いずれかが±2%以内、日25・週13はともに±10%以内。"
                "A/Bランクで初動強度を表示"
            ),
        },
        "screening_summary": {
            "raw_9_of_9": sum(x["score"] == 9 for x in raw_candidates),
            "raw_8_of_9": sum(x["score"] == 8 for x in raw_candidates),
            "raw_7_of_9": sum(x["score"] == 7 for x in raw_candidates),
            "raw_all_ma_above": len(raw_all_ma_above_candidates),
            "raw_near_9_of_9": len(raw_near_9_of_9_candidates),
            "raw_near_all_ma_above": len(raw_near_all_ma_above_candidates),
            "raw_early_stage": len(raw_early_stage_candidates),
            "price_eligible_unique_symbols": len(screening_pool),
            "fundamental": fundamental_summary,
        },
        "counts": counts,
        "coverage": {
            "price_source_mode": (
                "confirmed_daily" if historical_close else "intraday_5m"
            ),
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
            "expected_last_bar": (
                None
                if historical_close
                else "11:25" if session == "noon" else "15:20"
            ),
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
            "screening_excluded": [
                {
                    "code": x["code"],
                    "name": x["name"],
                    "price": x.get("price"),
                    "score": x.get("score"),
                    "price_pass": (
                        x.get("price") is not None
                        and x["price"] <= MAX_CANDIDATE_PRICE
                    ),
                    "earnings_status": (
                        fundamental_map.get(x["ticker"], {}).get("status")
                        if x.get("price") is not None
                        and x["price"] <= MAX_CANDIDATE_PRICE
                        else "not_checked"
                    ),
                    "earnings_improving": (
                        fundamental_map.get(x["ticker"], {}).get("improving")
                        if x.get("price") is not None
                        and x["price"] <= MAX_CANDIDATE_PRICE
                        else None
                    ),
                }
                for x in raw_candidates
                if not final_screen_pass(x)
            ],
            "near_screening_excluded": {
                "near_9_of_9": [
                    {
                        "code": x["code"], "name": x["name"], "score": x.get("score"),
                        "price": x.get("price"),
                        "required_rise_pct": x.get("near_9_of_9_required_rise_pct"),
                        "price_pass": x.get("price") is not None and x["price"] <= MAX_CANDIDATE_PRICE,
                        "earnings_status": fundamental_map.get(x["ticker"], {}).get("status"),
                        "earnings_improving": fundamental_map.get(x["ticker"], {}).get("improving"),
                        "quarantined": x["code"] in quarantine_codes,
                    }
                    for x in raw_near_9_of_9_candidates
                    if not final_screen_pass(x) or x["code"] in quarantine_codes
                ],
                "near_all_ma_above": [
                    {
                        "code": x["code"], "name": x["name"], "score": x.get("score"),
                        "price": x.get("price"),
                        "required_rise_pct": x.get("near_all_ma_above_required_rise_pct"),
                        "price_pass": x.get("price") is not None and x["price"] <= MAX_CANDIDATE_PRICE,
                        "earnings_status": fundamental_map.get(x["ticker"], {}).get("status"),
                        "earnings_improving": fundamental_map.get(x["ticker"], {}).get("improving"),
                        "quarantined": x["code"] in quarantine_codes,
                    }
                    for x in raw_near_all_ma_above_candidates
                    if not final_screen_pass(x) or x["code"] in quarantine_codes
                ],
            },
            "symbols": diagnostic_symbols,
        },
        # ①〜③。9/9,8/9,7/9の全件を保持。
        "candidates": candidates,
        # ④。全時間軸MA上（方向不問）の全件を保持。
        "all_ma_above_candidates": all_ma_above_candidates,
        "near_9_of_9_candidates": near_9_of_9_candidates,
        "near_all_ma_above_candidates": near_all_ma_above_candidates,
        # TDK/SWCC型。複数MAの近接を利用した「初動」候補。
        "early_stage_candidates": early_stage_candidates,
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
        historical_close=historical_close,
    )
    if (
        fundamental_summary.get("requested", 0) > 0
        and fundamental_summary.get("available_ratio", 0.0)
        < MIN_FUNDAMENTAL_AVAILABLE_RATIO
    ):
        gate_errors.append(
            "fundamental_available_ratio="
            f"{fundamental_summary.get('available_ratio', 0.0):.4f} "
            f"< {MIN_FUNDAMENTAL_AVAILABLE_RATIO:.2f}"
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
        diagnostics_dir / f"{effective_date}_{session}_attempt.json",
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
    archive_path = archive_dir / f"{effective_date}_{session}.json"

    atomic_write_json(latest_path, document)
    atomic_write_json(archive_path, document)

    log(
        f"{session}: "
        f"9/9={counts['9_of_9']} "
        f"8/9={counts['8_of_9']} "
        f"7/9={counts['7_of_9']} "
        f"all-above={counts['all_ma_above']} "
        f"early-stage={counts['early_stage']} "
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
        help="Legacy filename; contents are the dynamic liquidity universe.",
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
        help="Legacy filename; contents are the dynamic liquidity universe.",
    )
    scan_parser.add_argument(
        "--results-dir",
        default="results",
    )
    scan_parser.add_argument(
        "--target-date",
        type=date.fromisoformat,
        default=None,
        help="Optional JST target date (YYYY-MM-DD). Past dates support close only.",
    )

    args = parser.parse_args()
    if args.command == "update-universe":
        universe, meta = build_liquidity_universe(Path(args.output))
        log(
            "Universe update completed: "
            f"{len(universe)} rows, as_of={meta['latest_as_of']}"
        )
    else:
        scan(
            args.session,
            Path(args.universe),
            Path(args.results_dir),
            args.target_date,
        )


if __name__ == "__main__":
    main()
