"""Fail-closed execution-safety audit for short-scanner research groups.

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
EXTRA_GROUPS = (
    'near_all_ma_below_price_candidates',
    'near_all_ma_below_touch_candidates',
)
JST = ZoneInfo('Asia/Tokyo')
REQUIRED_COLUMNS = (
    'code', 'sbi_system_sellable', 'sbi_checked_date', 'sbi_source',
    'jsf_asof_date', 'jsf_source', 'jsf_restriction',
    'jsf_reverse_fee_yen', 'jsf_stock_shortage',
    'earnings_checked_date', 'earnings_source', 'next_earnings_date',
    'jpx_checked_date', 'jpx_source', 'jpx_lending_eligible',
    'jsf_last5_reverse_fees_yen', 'jsf_last5_shortage',
)
EXCLUDE_FLAGS = {
    'sbi_sell_not_allowed', 'jsf_restricted', 'jsf_positive_reverse_fee',
    'jsf_stock_shortage', 'earnings_within_7_days',
    'jpx_not_lendable', 'jsf_recent_fee_high_risk',
    'jsf_recent_shortage_high_risk',
    'sbi_public_loan_caution', 'sbi_public_sell_suspended',
    'jsf_public_not_loanable',
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


def _recent_jsf_history(row, flags):
    """Check five confirmed trading sessions, newest first. Never infer zero for missing data."""
    raw_fees = str(row.get('jsf_last5_reverse_fees_yen') or '').strip()
    fee_parts = [v.strip() for v in raw_fees.split(';')]
    if len(fee_parts) != 5:
        flags.append('jsf_recent_fee_history_unconfirmed')
    else:
        try:
            fees = [float(v) for v in fee_parts]
            if any(not math.isfinite(v) or v < 0 for v in fees):
                raise ValueError('invalid fee')
            # 2 of last 5 charged, or a significant fee on any day.
            if sum(v > 0 for v in fees) >= 2 or max(fees) >= 0.10:
                flags.append('jsf_recent_fee_high_risk')
        except (ValueError, TypeError):
            flags.append('jsf_recent_fee_history_unconfirmed')
    raw_short = str(row.get('jsf_last5_shortage') or '').strip()
    short_parts = [v.strip() for v in raw_short.split(';')]
    shortages = [_yes_no(v) for v in short_parts]
    if len(shortages) != 5 or any(v is None for v in shortages):
        flags.append('jsf_recent_shortage_history_unconfirmed')
    elif sum(shortages) >= 2:
        flags.append('jsf_recent_shortage_high_risk')


def evaluate(row, target):
    """Return verdict with explicit failures; historical zero fee is not a guarantee."""
    flags = []
    if not row:
        return {'status': 'verification_required', 'flags': ['all_checks_missing'],
                'order_ready': False}

    # A public SBI lending warning blocks new-short approval independent of
    # broker order-screen availability. A published "none" does NOT establish
    # actual orderability: the fresh broker-specific check is still mandatory.
    alert = str(row.get('sbi_public_alert_status') or '').strip().lower()
    alert_date = _on_date(row.get('sbi_public_alert_checked_date'))
    alert_source = str(row.get('sbi_public_alert_source') or '').strip()
    if alert in ('loan_caution', 'new_sell_suspended'):
        # Keep negative evidence until a *fresh* official clear notice replaces it.
        if alert_date and alert_date <= target and alert_source:
            flags.append('sbi_public_loan_caution' if alert == 'loan_caution'
                         else 'sbi_public_sell_suspended')
        else:
            flags.append('sbi_public_alert_unconfirmed')
    elif alert != 'none' or alert_date != target or not alert_source:
        flags.append('sbi_public_alert_unconfirmed')

    # An independent JSF classification may veto a sale; it never grants
    # affirmative SBI sellability or replaces the exchange confirmation.
    if str(row.get('jsf_public_lending_status') or '').strip().lower() == 'not_loanable':
        flags.append('jsf_public_not_loanable')

    # JPX loanable status differs from generic margin-buy eligibility.
    jpx_date = _on_date(row.get('jpx_checked_date'), target)
    if not jpx_date or not row.get('jpx_source', '').strip():
        flags.append('jpx_lending_check_missing_or_stale')
    lendable = _yes_no(row.get('jpx_lending_eligible'))
    if lendable is False and jpx_date:
        flags.append('jpx_not_lendable')
    elif lendable is not True:
        flags.append('jpx_lending_unconfirmed')

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

    _recent_jsf_history(row, flags)

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
    groups = GROUPS + tuple(g for g in EXTRA_GROUPS if isinstance(doc.get(g), list))
    eligible = {g: [] for g in groups}
    for group in groups:
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
        'version': 3, 'checked_at_jst': datetime.now(JST).isoformat(timespec='seconds'),
        'target_date': target.isoformat(), 'unique_candidates': len(seen),
        'counts': tallies, 'order_ready_codes_by_group': eligible,
        'risk_file_rows': len(checks),
        'policy': ('All six groups remain research output. Only order_ready=true may be '
                   'considered for an order after a fresh SBI order-screen check. '
                   'Five-session history and dated public SBI alerts are required. '
                   'Even a zero reported fee does not guarantee future reverse fees.'),
    }
    return doc


def export_research_queue(doc, checks, path):
    """Write one row per ticker (not per overlapping group) for evidence review."""
    groups = GROUPS + tuple(g for g in EXTRA_GROUPS if isinstance(doc.get(g), list))
    candidates = {}
    for group in groups:
        for item in doc[group]:
            code = str(item['code'])
            obj = candidates.setdefault(code, {'code': code,
                                                'name': item.get('name', ''),
                                                'price': item.get('price', ''),
                                                'groups': []})
            obj['groups'].append(group)
    names = ('code', 'name', 'price', 'groups', 'verdict', 'flags',
             *REQUIRED_COLUMNS[1:],
             'sbi_public_alert_status', 'sbi_public_alert_checked_date',
             'sbi_public_alert_source')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        for code, item in sorted(candidates.items()):
            evidence = checks.get(code, {})
            verdict = evaluate(evidence, date.fromisoformat(doc['target_date']))
            row = {key: evidence.get(key, '') for key in names}
            row.update(code=code, name=item['name'], price=item['price'],
                       groups=';'.join(item['groups']), verdict=verdict['status'],
                       flags=';'.join(verdict['flags']))
            writer.writerow(row)
    return len(candidates)


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
    export_research_queue(doc, checks,
        results_dir / 'short_research' / f'{target}_{session}_review.csv')
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

