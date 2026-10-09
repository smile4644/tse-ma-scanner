# Noon-to-close low inversion: investigation and safe remediation

## Incident
2026-10-09 close: 125 symbols were quarantined because confirmed daily Low exceeded the 10:30 5-minute aggregated Low. The two sources are not equivalent, so the discrepancy must be investigated before restoring any symbol.

## Verified implementation
- `download_many` calls `yf.download(... auto_adjust=False, repair=True, prepost=False)`.
- `intraday_bar` aggregates same-date 5-minute Low up to the session cutoff.
- `current_daily_bar` reads same-date daily OHLC.
- `score_one` uses 5-minute intraday for noon, confirmed daily for close.
- close compares day/week/month Low from noon diagnostic symbols against close symbols, and quarantines each affected symbol.
- No change to MA/EPS screening or quarantine policy is proposed without evidence.

## Required reproducible source audit
For **each** inverted symbol, capture:
1. ticker, JPX code, target date, session, provider request parameters, fetch UTC/JST timestamp, yfinance version.
2. unmodified 5-minute rows (timestamp with JST offset, Open/High/Low/Close/Volume), especially the row with minimum Low and last row at/before 10:30.
3. unmodified daily OHLCV for the same trading date, fetched after official close.
4. raw-vs-repaired (`repair=False` and `repair=True`) 5-minute and daily OHLCV, from separate calls, with `auto_adjust=False` in all four cases.
5. whether noon 5-minute Low < daily Low, and the difference, before and after repair.
6. evidence of split/corporate-action, delayed bar corrections, timezone mismatch, stale cached responses, or provider disagreement. Do not assume any one is the cause.

## Fail-closed checks
- Daily and noon source data must have identical ticker/date/currency and documented adjustment basis.
- Do not alter confirmed daily close OHLC to match 5-minute data.
- Do not silently clamp noon Low to daily Low, change comparison tolerance, or remove quarantine.
- If raw/repaired source mismatch is unresolved, retain quarantine and emit per-symbol evidence in diagnostic JSON.
- If the mismatch is proven to be caused by erroneous noon data, fix only the identified ingestion/repair path; re-run both sessions using comparable data and archive the provenance.

## Regression tests
1. Valid: daily Low <= noon Low: no quarantine.
2. Invalid: daily Low > noon Low: quarantine, with code, name, both lows, difference, provenance.
3. Source metadata missing/incompatible: mark comparison unverifiable, do not declare clean.
4. 5-minute timezone boundary: only same target date through 10:30 JST.
5. 5-minute Low corrupted by repair, while raw bars agree with daily: record raw/repaired delta, keep quarantine until approved fix.
6. 5-minute and daily both have repaired values but different adjustment bases: no auto-correction.
7. Regression: buy/short ①–⑥ candidates, EPS filters, schema 19 and confirmed_daily_close behavior unchanged by audit-only changes.

## Acceptance criteria
- 125/125 incident symbols have reproducible evidence and reason category (or explicit unresolved).
- No unverified symbol is restored to official results.
- New scan emits raw/repaired comparison metrics and provenance before a remediation is merged.
- Existing tests pass; official accepted JSON is produced only under the existing safety gates.
