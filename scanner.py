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
USER_AGENT = "tse-ma-scanner/1.2 (+github)"

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


def download_many(
    tickers: list[str],
    period: str,
    interval: str,
    batch_size: int = 100,
) -> dict[str, pd.DataFrame]:
    """
    価格は auto_adjust=True で統一する。
    株式分割・配当等を跨いでも、過去OHLCと現在値の価格尺度を合わせる。
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


def completed_period_closes(daily_frame: pd.DataFrame, period: str) -> pd.Series:
    """
    週足・月足MA用の確定終値を、同一の調整済み日足からローカル生成する。
    period: "W" or "M"
    """
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


def ma_current_and_previous(completed_closes: pd.Series, current_close: float, length: int):
    completed = pd.to_numeric(completed_closes, errors="coerce").dropna()
    if len(completed) < length:
        return None, None
    current_values = pd.concat(
        [completed.reset_index(drop=True), pd.Series([current_close])],
        ignore_index=True,
    ).iloc[-length:]
    return float(current_values.mean()), float(completed.iloc[-length:].mean())


def classify_state(ma, previous_ma, candle_low: float, candle_high: float) -> int:
    if ma is None or previous_ma is None:
        return -1
    direction = 2 if ma > previous_ma else 0 if ma < previous_ma else 1
    position = 2 if candle_low > ma else 0 if candle_high < ma else 1
    return direction * 3 + position


def intraday_bar(frame: pd.DataFrame | None, target_date, session: str):
    if frame is None or frame.empty:
        return None

    work = frame.copy()
    idx = pd.to_datetime(work.index)
    idx = idx.tz_localize(JST) if getattr(idx, "tz", None) is None else idx.tz_convert(JST)
    work.index = idx

    # 東証ザラバは15:25まで、15:25～15:30はクロージング・オークション。
    # Yahoo 5分足では大多数の銘柄の最終ザラバ足が15:20開始なので、
    # closeの「5分足鮮度」は15:20以上を正常とする。
    cutoff = dtime(11, 30) if session == "noon" else dtime(15, 30)
    expected_minimum = dtime(11, 25) if session == "noon" else dtime(15, 20)

    work = work[(work.index.date == target_date) & (work.index.time <= cutoff)]
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


def current_daily_bar(frame: pd.DataFrame | None, target_date):
    """大引け後にYahooの当日確定日足があれば、そのOHLCを取得する。"""
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


def partial_period_ohlc(completed_daily: pd.DataFrame, current_day: dict, start_date, today):
    dates = local_dates(completed_daily)
    prior_part = completed_daily.loc[((dates >= start_date) & (dates < today)).values]
    highs = pd.to_numeric(prior_part["High"], errors="coerce").dropna().tolist() + [current_day["high"]]
    lows = pd.to_numeric(prior_part["Low"], errors="coerce").dropna().tolist() + [current_day["low"]]
    return {"high": max(highs), "low": min(lows), "close": current_day["close"]}


def analyze_symbol(row, intraday_frame, daily_frame, session: str, today):
    intra = intraday_bar(intraday_frame, today, session)
    daily_today = current_daily_bar(daily_frame, today)

    # 前引けは5分足を使用。大引けは確定日足を最優先し、
    # 未更新なら5分足へフォールバックする。
    if session == "close" and daily_today is not None:
        current_day = {
            **daily_today,
            "source": "daily_1d",
            "last_bar": intra["last_bar"] if intra else None,
            "intraday_fresh": intra["fresh"] if intra else False,
            "expected_minimum_time": intra["expected_minimum_time"] if intra else "15:20",
            "intraday_close": intra["close"] if intra else None,
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
        return {
            "code": str(row.code),
            "name": row.name,
            "market": row.market,
            "ticker": row.ticker,
            "universe_rank": int(row.rank),
            "previous_day_volume": int(row.volume),
            "score": None,
            "analysis_status": "unscored",
            "analysis_failure_reason": "current_day_price_missing",
            "last_bar": None,
            "intraday_fresh": False,
            "expected_minimum_time": "11:25" if session == "noon" else "15:20",
            "price_source": None,
            "candle": None,
            "states": {},
        }

    completed_daily = history_before(daily_frame, today)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    if completed_daily.empty:
        failure = "daily_history_missing"
        completed_weekly_closes = pd.Series(dtype=float)
        completed_monthly_closes = pd.Series(dtype=float)
        current_week = None
        current_month = None
    else:
        completed_before_week = history_before(daily_frame, week_start)
        completed_before_month = history_before(daily_frame, month_start)
        completed_weekly_closes = completed_period_closes(completed_before_week, "W")
        completed_monthly_closes = completed_period_closes(completed_before_month, "M")
        current_week = partial_period_ohlc(completed_daily, current_day, week_start, today)
        current_month = partial_period_ohlc(completed_daily, current_day, month_start, today)
        failure = None

    base = {
        "code": str(row.code),
        "name": row.name,
        "market": row.market,
        "ticker": row.ticker,
        "universe_rank": int(row.rank),
        "previous_day_volume": int(row.volume),
        "price": round(current_day["close"], 4),
        "intraday_volume": intra["volume"] if intra else None,
        "daily_volume": current_day["volume"],
        "last_bar": current_day["last_bar"],
        "intraday_fresh": current_day["intraday_fresh"],
        "expected_minimum_time": current_day["expected_minimum_time"],
        "price_source": current_day["source"],
        "intraday_close": round(current_day["intraday_close"], 4) if current_day["intraday_close"] is not None else None,
        "candle": {
            "day": {
                "open": round(current_day["open"], 4),
                "high": round(current_day["high"], 4),
                "low": round(current_day["low"], 4),
                "close": round(current_day["close"], 4),
            },
            "week": (
                {
                    "high": round(current_week["high"], 4),
                    "low": round(current_week["low"], 4),
                    "close": round(current_week["close"], 4),
                } if current_week is not None else None
            ),
            "month": (
                {
                    "high": round(current_month["high"], 4),
                    "low": round(current_month["low"], 4),
                    "close": round(current_month["close"], 4),
                } if current_month is not None else None
            ),
        },
    }

    if failure is not None:
        return {
            **base,
            "score": None,
            "analysis_status": "unscored",
            "analysis_failure_reason": failure,
            "states": {},
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
    failure = None

    for index, (key, label, length, timeframe) in enumerate(SPECS):
        completed_closes, current_close, candle_low, candle_high = context[timeframe]
        ma, previous_ma = ma_current_and_previous(completed_closes, current_close, length)
        state = classify_state(ma, previous_ma, candle_low, candle_high)
        if state < 0:
            failure = f"ma_history_insufficient:{key}"
            break

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

    if failure is not None:
        return {
            **base,
            "score": None,
            "analysis_status": "unscored",
            "analysis_failure_reason": failure,
            "states": states,
        }

    all_ma_above = all(x["state"] in (2, 5, 8) for x in states.values())
    non_upward_ma = [
        states[key]["label"]
        for key, _, _, _ in SPECS
        if states[key]["state"] in (2, 5)
    ]

    return {
        **base,
        "score": score,
        "analysis_status": "scored",
        "analysis_failure_reason": None,
        "tier": 3 if score == 9 else 2 if score == 8 else 1 if score == 7 else 0,
        "all_ma_above": all_ma_above,
        "non_upward_ma": non_upward_ma,
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


def state_changes(noon_item, close_item):
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


def scan(session: str, universe_path: Path, results_dir: Path) -> None:
    universe = build_top1000_universe(universe_path)
    tickers = universe["ticker"].tolist()
    today = datetime.now(JST).date()

    # 5分足は当日判定用。日・週・月の全MAは、同一の調整済み10年日足から作る。
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

    scored = [x for x in all_symbols if x.get("score") is not None]
    unscored = [x for x in all_symbols if x.get("score") is None]
    candidates = [x for x in scored if x["score"] >= 7]
    all_ma_above_candidates = [x for x in scored if x.get("all_ma_above")]

    candidates.sort(key=lambda x: (-x["score"], x["universe_rank"], x["code"]))
    all_ma_above_candidates.sort(key=lambda x: (-x["score"], x["universe_rank"], x["code"]))
    all_symbols.sort(key=lambda x: x["universe_rank"])

    score_distribution = {str(i): 0 for i in range(10)}
    score_distribution["NA"] = len(unscored)
    for x in scored:
        score_distribution[str(x["score"])] += 1

    # 最終5分足時刻はScore算出可否と無関係に全ユニバースで集計。
    last_bar_distribution = {}
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

    current_day_intraday_count = len(universe) - len(intraday_missing_codes)
    close_daily_source_count = sum(x.get("price_source") == "daily_1d" for x in all_symbols)
    close_intraday_fallback_count = sum(
        session == "close" and x.get("price_source") == "intraday_5m"
        for x in all_symbols
    )

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
                if noon_doc.get("target_date") == today.isoformat() and noon_doc.get("session") == "noon":
                    close_map = {x["code"]: x for x in all_symbols}
                    noon_candidates = noon_doc.get("candidates", [])

                    for n in noon_candidates:
                        c = close_map.get(n["code"])
                        if c is None or c.get("score") is None:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n["score"],
                                "close_score": None,
                                "reason": c.get("analysis_failure_reason") if c else "close_analysis_unavailable",
                            })
                        elif c["score"] < 7:
                            dropped_from_noon.append({
                                "code": n["code"],
                                "name": n["name"],
                                "noon_score": n["score"],
                                "close_score": c["score"],
                                "noon_price": n["price"],
                                "close_price": c["price"],
                                "close_price_source": c.get("price_source"),
                                "close_last_bar": c.get("last_bar"),
                                "close_failures": c.get("failures", []),
                                "changed_states": state_changes(n, c),
                            })

                    noon_symbols = {
                        x["code"]: x
                        for x in noon_doc.get("diagnostics", {}).get("symbols", [])
                    }
                    for code, n in noon_symbols.items():
                        c = close_map.get(code)
                        if c is None or not c.get("candle"):
                            continue
                        for tf in ("day", "week", "month"):
                            n_candle = n.get("candle", {}).get(tf)
                            c_candle = c.get("candle", {}).get(tf)
                            if not n_candle or not c_candle:
                                continue
                            nlow = n_candle.get("low")
                            clow = c_candle.get("low")
                            if nlow is not None and clow is not None and clow > nlow + 1e-9:
                                impossible_low_increases.append({
                                    "code": code,
                                    "name": c["name"],
                                    "timeframe": tf,
                                    "noon_low": nlow,
                                    "close_low": clow,
                                })

                    noon_comparison = {
                        "source_schema_version": noon_doc.get("schema_version"),
                        "source_generated_at_jst": noon_doc.get("generated_at_jst"),
                        "noon_candidate_count": len(noon_candidates),
                        "retained_score_7_plus": sum(
                            1 for n in noon_candidates
                            if close_map.get(n["code"], {}).get("score") is not None
                            and close_map[n["code"]]["score"] >= 7
                        ),
                        "dropped_below_7": sum(
                            x.get("close_score") is not None for x in dropped_from_noon
                        ),
                        "close_unscored": sum(
                            x.get("close_score") is None for x in dropped_from_noon
                        ),
                    }
                else:
                    noon_comparison = {
                        "status": "not_comparable",
                        "reason": "date_or_session_mismatch",
                    }
            except Exception as exc:
                noon_comparison = {
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        else:
            noon_comparison = {"status": "not_available"}

    quality_issues = []
    if unscored:
        quality_issues.append(f"unscored={len(unscored)}")
    if stale_last_bar:
        quality_issues.append(f"stale_last_bar={len(stale_last_bar)}")
    if invalid_ohlc:
        quality_issues.append(f"invalid_ohlc={len(invalid_ohlc)}")
    if impossible_low_increases:
        quality_issues.append(
            f"impossible_noon_to_close_low_increase={len(impossible_low_increases)}"
        )
    if session == "close" and close_intraday_fallback_count:
        quality_issues.append(
            f"close_daily_bar_unavailable_fallback={close_intraday_fallback_count}"
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

    # diagnostics.symbols は全1000銘柄を必ず格納。
    diagnostic_symbols = []
    for x in all_symbols:
        diagnostic_symbols.append({
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
            "analysis_status": x.get("analysis_status"),
            "analysis_failure_reason": x.get("analysis_failure_reason"),
            "tier": x.get("tier"),
            "all_ma_above": x.get("all_ma_above"),
            "candle": x.get("candle"),
            "failures": x.get("failures", []),
            "states": x.get("states", {}),
        })

    document = {
        "schema_version": 5,
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
            "Yahoo Finance via yfinance; adjusted OHLC. "
            "日・週・月MAは同一の調整済み日足から生成。"
        ),
        "universe_definition": (
            "東証プライム・スタンダード・グロースの内国普通株から、"
            "前営業日出来高上位1000銘柄"
        ),
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
            "daily_10y_downloaded": len(daily),
            "diagnostic_symbols": len(diagnostic_symbols),
            "scored": len(scored),
            "unscored": len(unscored),
            "close_daily_source": close_daily_source_count if session == "close" else None,
            "close_intraday_fallback": close_intraday_fallback_count if session == "close" else None,
        },
        "data_quality": {
            "quality_status": "warning" if quality_issues else "ok",
            "issues": quality_issues,
            "expected_last_bar": "11:25" if session == "noon" else "15:20",
            "stale_last_bar_count": len(stale_last_bar),
            "intraday_missing_count": len(intraday_missing_codes),
            "invalid_ohlc_count": len(invalid_ohlc),
            "impossible_noon_to_close_low_increase_count": len(impossible_low_increases),
        },
        "diagnostics": {
            "score_distribution": score_distribution,
            "last_bar_distribution": dict(sorted(last_bar_distribution.items())),
            "analysis_failures": analysis_failures,
            "intraday_missing_symbols": intraday_missing_codes,
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
    latest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    archive_dir = results_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    archive_path = archive_dir / f"{today}_{session}.json"
    archive_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(
        f"{session}: "
        f"9/9={document['counts']['9_of_9']} "
        f"8/9={document['counts']['8_of_9']} "
        f"7/9={document['counts']['7_of_9']} "
        f"all-above={document['counts']['all_ma_above']} "
        f"scored={len(scored)}/{len(universe)} "
        f"quality={document['data_quality']['quality_status']}"
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
