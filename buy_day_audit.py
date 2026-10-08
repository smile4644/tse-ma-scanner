"""Validate prior-close MA picks against NEXT CLOSE, avoiding look-ahead bias.

Example:
python buy_day_audit.py --prior results/archive/2026-10-07_close.json \
  --following results/archive/2026-10-08_close.json \
  --benchmark-prior 4154.11 --benchmark-following 4091.46 \
  --output-json results/audit_2026-10-08.json \
  --output-csv results/audit_2026-10-08_rows.csv

No current-day new pick is ever included in the prior-day cohort.
The scanner uses UNADJUSTED closes; adjust for splits/dividends when
extending a multi-day total-return backtest.
"""
import argparse
import csv
import json
import statistics
from datetime import date
from pathlib import Path

GROUP_NAMES = (
    '①9/9', '②9/9目前', '③8/9', '④7/9', '⑤全MA上', '⑥全MA目前A', '⑥全MA目前B'
)


def cohorts(document):
    pool = document['candidates']
    return {
        '①9/9': [v for v in pool if v.get('score') == 9],
        '②9/9目前': document['near_9_of_9_candidates'],
        '③8/9': [v for v in pool if v.get('score') == 8],
        '④7/9': [v for v in pool if v.get('score') == 7],
        '⑤全MA上': document['all_ma_above_candidates'],
        '⑥全MA目前A': document['near_all_ma_above_price_candidates'],
        '⑥全MA目前B': document['near_all_ma_above_touch_candidates'],
    }


def _bar_matches_target_date(last_bar, target_date):
    """Accept YYYY-MM-DD or an ISO datetime for the target Japanese trading date."""
    if not isinstance(last_bar, str):
        return False
    if len(last_bar) > 10 and last_bar[10] not in ('T', ' '):
        return False
    try:
        return date.fromisoformat(last_bar[:10]) == date.fromisoformat(target_date)
    except ValueError:
        return False


def validate(prior, following, benchmark_prior, benchmark_following):
    for doc in (prior, following):
        if doc.get('write_status') != 'accepted' or doc.get('safety_gate_errors'):
            raise ValueError('Refuse unaccepted or unsafe scanner JSON')
        if doc.get('session') != 'close' or doc.get('schema_version', 0) < 19:
            raise ValueError('Require close scanner JSON schema 19 or newer')
    if date.fromisoformat(prior['target_date']) >= date.fromisoformat(following['target_date']):
        raise ValueError('Dates not increasing')
    if prior.get('price_series_mode') != following.get('price_series_mode'):
        raise ValueError('Price data basis mismatch')
    if benchmark_prior <= 0 or benchmark_following <= 0:
        raise ValueError('Benchmark prices must be positive')
    benchmark_return = 100 * (benchmark_following / benchmark_prior - 1)
    diagnostics = {x['code']: x for x in following['diagnostics']['symbols']}
    rows, summary = [], {}
    for group, picks in cohorts(prior).items():
        seen = set()
        known, missed = [], []
        for item in picks:
            code = item['code']
            if code in seen:
                raise ValueError(f'Duplicate in {group}: {code}')
            seen.add(code)
            nxt = diagnostics.get(code)
            old, new = item.get('price'), nxt.get('price') if nxt else None
            reason = ('missing_today_universe' if nxt is None else
                      'quarantined_today' if nxt.get('quarantined') else
                      'date_mismatch' if not _bar_matches_target_date(nxt.get('last_bar'), following['target_date']) else
                      'price_invalid' if not isinstance(old, (int, float)) or not isinstance(new, (int, float))
                       or old <= 0 or new <= 0 else None)
            entry = {'cohort': group, 'code': code, 'name': item.get('name'),
                     'prior_close': old, 'following_close': new,
                     'day_return_pct': None, 'excess_return_pct': None,
                     'beat_benchmark': None, 'status': reason or 'matched'}
            if reason is None:
                ret = 100 * (new / old - 1)
                entry['day_return_pct'] = round(ret, 4)
                entry['excess_return_pct'] = round(ret - benchmark_return, 4)
                entry['beat_benchmark'] = ret > benchmark_return
                entry['intraday_low_return_pct'] = (round(100*(nxt['day_low']/old-1),4)
                    if isinstance(nxt.get('day_low'),(int,float)) else None)
                known.append(entry)
            else:
                missed.append({'code':code,'reason':reason})
            rows.append(entry)
        rets = [x['day_return_pct'] for x in known]
        count = len(known)
        summary[group] = {
            'selected': len(picks), 'matched': count, 'missing': missed,
            'mean_pct': round(statistics.mean(rets), 4) if rets else None,
            'median_pct': round(statistics.median(rets), 4) if rets else None,
            'benchmark_beat_count': sum(x['beat_benchmark'] for x in known),
            'benchmark_beat_ratio': round(sum(x['beat_benchmark'] for x in known)/count, 4) if count else None,
            'up_or_flat_count': sum(v >= 0 for v in rets),
            'below_minus_2_count': sum(v < -2 for v in rets),
            'worst_pct': min(rets) if rets else None,
            'best_pct': max(rets) if rets else None,
        }
    return {
        'dates': [prior['target_date'], following['target_date']],
        'benchmark_return_pct': round(benchmark_return,4),
        'methodology': 'Prior close cohort only; following close returns; scanner price series unadjusted; no selection look-ahead.',
        'summary': summary, 'rows': rows
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prior', required=True, type=Path)
    p.add_argument('--following', required=True, type=Path)
    p.add_argument('--benchmark-prior', type=float, required=True)
    p.add_argument('--benchmark-following', type=float, required=True)
    p.add_argument('--output-json', type=Path)
    p.add_argument('--output-csv', type=Path)
    args = p.parse_args()
    old = json.loads(args.prior.read_text(encoding='utf-8'))
    new = json.loads(args.following.read_text(encoding='utf-8'))
    report = validate(old, new, args.benchmark_prior, args.benchmark_following)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True,exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True,exist_ok=True)
        with args.output_csv.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f, fieldnames=('cohort','code','name','prior_close','following_close','day_return_pct','excess_return_pct','beat_benchmark','status','intraday_low_return_pct'))
            writer.writeheader()
            for row in report['rows']:
                writer.writerow(row)
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}, ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()

