"""Fail-closed execution-safety audit for six short-scanner research groups.

Optional same-day checks: data/short_trade_checks_YYYY-MM-DD.csv
One row per ticker, with independent date-stamped broker, JSF and earnings evidence.
Missing evidence NEVER makes a candidate order-ready.
"""
import argparse
import csv
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GROUPS = (
    'short_9_of_9_candidates', 'near_short_9_of_9_candidates',
    'short_8_of_9_candidates', 'short_7_of_9_candidates',
    'all_ma_below_candidates', 'near_all_ma_below_candidates',
)
JST = ZoneInfo('Asia/Tokyo')
REQUIRED_COLUMNS = (
    'code', 'sbi_system_sellable', 'sbi_checked_date', 'sbi_source',
    'jsf_asof_date', 'jsf_source', 'jsf_restriction',
    'jsf_reverse_fee_yen', 'jsf_stock_shortage',
    'earnings_checked_date', 'earnings_source', 'next_earnings_date',
)
EXCLUDE_FLAGS = {
    'sbi_sell_not_allowed', 'jsf_restricted', 'jsf_positive_reverse_fee',
    'jsf_stock_shortage', 'earnings_within_7_days',
}


def _on_date(value, expected=None):
    try:
        result = date.fromisoformat(str(value).strip())
        return result if expected is None or result == expected else None
    except (TypeError, ValueError):
        return None


def _yes_no(value):
    s = str(value or '').strip().lower()
    return {'yes': True, 'no': False, 'true': True, 'false': False,
            '1': True, '0': False}.get(s)


def read_checks(path):
    if not path.exists():
        return {}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(k not in reader.fieldnames for k in REQUIRED_COLUMNS):
            raise ValueError('Invalid short risk-check CSV column names')
        checks = {}
        for row in reader:
            code = str(row.get('code') or '').strip()
            if not code or code in checks:
                raise ValueError(f'Empty or duplicate risk-check code: {code!r}')
            checks[code] = row
        return checks


def evaluate(row, target):
    """Return verdict with explicit failures; historical zero fee is not a guarantee."""
    flags = []
    if not row:
        return {'status': 'verification_required', 'flags': ['all_checks_missing'],
                'order_ready': False}

    broker_date = _on_date(row.get('sbi_checked_date'), target)
    if not broker_date or not row.get('sbi_source', '').strip():
        flags.append('sbi_check_missing_or_stale')
    sellable = _yes_no(row.get('sbi_system_sellable'))
    if broker_date and sellable is False:
        flags.append('sbi_sell_not_allowed')
    elif sellable is not True:
        flags.append('sbi_sellability_unconfirmed')

    jsf_date = _on_date(row.get('jsf_asof_date'))
    if not jsf_date or not (target - timedelta(days=4) <= jsf_date <= target) or not row.get('jsf_source', '').strip():
        flags.append('jsf_check_missing_or_stale')
    restriction = _yes_no(row.get('jsf_restriction'))
    shortage = _yes_no(row.get('jsf_stock_shortage'))
    if restriction is True:
        flags.append('jsf_restricted')
    elif restriction is None:
        flags.append('jsf_restriction_unconfirmed')
    if shortage is True:
        flags.append('jsf_stock_shortage')
    elif shortage is None:
        flags.append('jsf_shortage_unconfirmed')
    try:
        fee = float(str(row.get('jsf_reverse_fee_yen', '')).strip())
        if not math.isfinite(fee) or fee < 0:
            raise ValueError('Invalid fee')
        if fee > 0:
            flags.append('jsf_positive_reverse_fee')
    except (ValueError, TypeError):
        flags.append('jsf_reverse_fee_unconfirmed')

    earnings_checked = _on_date(row.get('earnings_checked_date'), target)
    if not earnings_checked or not row.get('earnings_source', '').strip():
        flags.append('earnings_check_missing_or_stale')
    earnings_date = _on_date(row.get('next_earnings_date'))
    if earnings_date is None:
        flags.append('next_earnings_date_unconfirmed')
    elif earnings_date <= target + timedelta(days=7):
        flags.append('earnings_within_7_days')

    if any(flag in EXCLUDE_FLAGS for flag in flags):
        status = 'excluded'
    elif flags:
        status = 'verification_required'
    else:
        status = 'eligible'
    return {'status': status, 'flags': flags, 'order_ready': status == 'eligible'}


def apply_gate(doc, checks):
    target = date.fromisoformat(doc['target_date'])
    missing = [g for g in GROUPS if not isinstance(doc.get(g), list)]
    if missing:
        raise ValueError(f'Short groups absent: {missing}')
    seen = {}
    eligible = {g: [] for g in GROUPS}
    for group in GROUPS:
        for candidate in doc[group]:
            code = str(candidate.get('code', '')).strip()
            if not code:
                raise ValueError('Candidate without ticker code')
            verdict = seen.setdefault(code, evaluate(checks.get(code), target))
            candidate['trade_safety'] = dict(verdict)
            if verdict['order_ready']:
                eligible[group].append(code)
    tallies = {s: sum(v['status'] == s for v in seen.values())
               for s in ('eligible', 'excluded', 'verification_required')}
    doc['trade_safety_gate'] = {
        'version': 1, 'checked_at_jst': datetime.now(JST).isoformat(timespec='seconds'),
        'target_date': target.isoformat(), 'unique_candidates': len(seen),
        'counts': tallies, 'order_ready_codes_by_group': eligible,
        'risk_file_rows': len(checks),
        'policy': ('All six groups remain research output. Only order_ready=true may be '
                   'considered for an order after a fresh SBI order-screen check. '
                   'Historical JSF reverse fee of zero does not rule out future reverse fees.'),
    }
    return doc


def atomic_write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.short_audit_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(results_dir, session, target, checks_path=None):
    latest = results_dir / f'latest_short_{session}.json'
    archive = results_dir / 'short_archive' / f'{target}_{session}.json'
    if not archive.exists() or not latest.exists():
        raise FileNotFoundError('Missing short results')
    doc = json.loads(archive.read_text(encoding='utf-8'))
    cur = json.loads(latest.read_text(encoding='utf-8'))
    if doc['target_date'] != target.isoformat() or doc['session'] != session:
        raise ValueError('Short archive date/session mismatch')
    if (cur.get('target_date'), cur.get('source_generated_at_jst')) != (
        doc.get('target_date'), doc.get('source_generated_at_jst')):
        raise ValueError('Latest short result is not the same source run; refuse overwrite')
    path = checks_path or Path('data') / f'short_trade_checks_{target}.csv'
    checks = read_checks(path)
    apply_gate(doc, checks)
    atomic_write(archive, doc)
    atomic_write(latest, doc)
    return doc['trade_safety_gate']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=('noon', 'close'), required=True)
    parser.add_argument('--target-date', type=date.fromisoformat, required=True)
    parser.add_argument('--results-dir', type=Path, default=Path('results'))
    parser.add_argument('--checks-path', type=Path, default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.results_dir, args.session, args.target_date,
                         args.checks_path), ensure_ascii=False))
