"""Collect official JSF *public* short-sale risk evidence (never broker approval).

Usage: python short_evidence_collect.py --session close --target-date 2026-10-09

Outputs a review report and a dated, merged risk-check CSV. No login or
broker credentials are read; SBI order-screen approval must be entered by a
human. Unknown, stale, missing, or malformed source data never means safe.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = 'https://www.taisyaku.jp/data/'
SOURCES = {'meigara': 'meigara.csv', 'shina': 'shina.csv',
           'zandaka': 'zandaka.csv', 'seigen': 'seigenichiran.csv'}
JST = ZoneInfo('Asia/Tokyo')
REPORT_COLS = ('code', 'name', 'price', 'jsf_classification',
               'jsf_rate_asof_date', 'jsf_reverse_fee_yen',
               'jsf_stock_shortage', 'jsf_restriction',
               'jsf_evidence', 'broker_status', 'earnings_status', 'final_status')


def download_csv(url: str) -> list[dict]:
    if not url.startswith(BASE):
        raise ValueError('Untrusted host')
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (risk-audit)'} )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(6_000_001)
        if len(body) > 6_000_000:
            raise ValueError('Oversized source')
    for encoding in ('utf-8-sig', 'cp932', 'shift_jis'):
        try:
            text = body.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError('Unsupported CSV encoding')
    data = list(csv.DictReader(io.StringIO(text)))
    if not data or not data[0]:
        raise ValueError(f'No rows in {url}')
    return data


def norm(key: str) -> str:
    return re.sub(r'[\s\u3000（）()・_\-:/]+', '', str(key).replace('\ufeff', '')).lower()


def field(row: dict, *names: str) -> str:
    lookup = {norm(k): str(v or '').strip() for k, v in row.items() if k is not None}
    for name in names:
        if norm(name) in lookup:
            return lookup[norm(name)]
    return ''


def code_of(row: dict) -> str:
    value = field(row, '銘柄コード', 'コード', '証券コード', 'コード番号')
    value = value.strip().upper().replace('.T', '')
    return value if re.fullmatch(r'[0-9]{4,5}|[0-9]{3}[A-Z]', value) else ''


def parse_date(value: str) -> str:
    s = str(value or '').strip()
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ''


def num(value: str):
    s = str(value or '').strip().replace(',', '').replace('−', '-').replace('△', '-')
    if not s or '*' in s or s in ('-', '－', '---', '満額'):
        return None
    try:
        result = float(s)
        return result if result == result and abs(result) != float('inf') else None
    except ValueError:
        return None


def find_columns(rows: list[dict], must_have: tuple[tuple[str,...],...], label: str):
    headers = {norm(s) for s in rows[0]}
    if any(not ({norm(x) for x in aliases} & headers) for aliases in must_have):
        raise ValueError(f'{label} schema unknown; refusing inference: {sorted(rows[0])}')


def official_snapshot(tables: dict[str, list[dict]], captured_date: date) -> dict:
    """Parse fail-closed: identifiable date/schema required before any evidence."""
    names = {k: v for k, v in SOURCES.items()}
    result = {'schema': 1, 'captured_at_jst': datetime.now(JST).isoformat(timespec='seconds'),
              'source_urls': {k: BASE + v for k,v in names.items()},
              'source_status': {}, 'rows': {}}
    source_schema = {
      'meigara': (('コード','銘柄コード'), ('貸借区分','区分','貸借取引対象区分')),
      'shina': (('コード','銘柄コード'), ('貸借申込日','申込日'), ('当日品貸料率','当日品貸料率円','品貸料率','品貸料率円')),
      'zandaka': (('コード','銘柄コード'), ('申込日','貸借申込日'), ('融資残高株数','融資残高','融資残高株数株口'), ('貸株残高株数','貸株残高','貸株残高株数株口')),
      'seigen': (('コード','銘柄コード'),),
    }
    for kind, rows in tables.items():
        try:
            find_columns(rows, source_schema[kind], kind)
            parsed = {}
            for row in rows:
                code = code_of(row)
                if code and code not in parsed:
                    parsed[code] = row
            if not parsed:
                raise ValueError('No valid security codes')
            result['rows'][kind] = parsed
            result['source_status'][kind] = 'ok'
        except (ValueError, KeyError) as ex:
            result['rows'][kind] = {}
            result['source_status'][kind] = f'invalid:{str(ex)[:220]}'
    return result


def collect(target_date: date) -> dict:
    tables, errors = {}, {}
    for kind, filename in SOURCES.items():
        try:
            tables[kind] = download_csv(BASE + filename)
        except Exception as ex:
            errors[kind] = f'{type(ex).__name__}:{str(ex)[:180]}'
    snap = official_snapshot(tables, target_date)
    for kind, message in errors.items():
        snap['source_status'][kind] = message
        snap['rows'][kind] = {}
    return snap


def pick_fee(row: dict) -> tuple[str, float | None, float | None]:
    asof = parse_date(field(row, '貸借申込日','申込日'))
    fee = num(field(row, '当日品貸料率','当日品貸料率円','品貸料率','品貸料率円'))
    shortage = num(field(row, '貸株超過株数','貸株超過株数株口','貸株超過'))
    return asof, fee, shortage


def add_official(checks: dict, code: str, source: dict, history: list[dict], target: date) -> tuple[dict,dict]:
    row = dict(checks)
    evidence = {'jsf_classification': 'unverified', 'jsf_date': '',
                'jsf_fee': None, 'jsf_stock_shortage': 'unverified',
                'jsf_restriction': 'unverified', 'checks': [], 'jsf_balance_asof_date': '', 'jsf_lending_excess_shares': None}
    state = source.get('source_status', {})
    sources = source.get('rows', {})
    if state.get('meigara') == 'ok':
        m = sources.get('meigara', {}).get(code)
        if m:
            cls = field(m,'貸借区分','区分','貸借取引対象区分')
            if cls in ('1', '貸借', '貸借銘柄'):
                evidence['jsf_classification'] = 'loanable'
                row['jsf_public_lending_status'] = 'loanable'
            elif cls in ('0','2','貸借融資','融資','非貸借','非制度信用'):
                evidence['jsf_classification'] = 'not_loanable'
                evidence['checks'].append('jsf_not_loanable')
                row['jsf_public_lending_status'] = 'not_loanable'
            else:
                evidence['checks'].append('jsf_classification_unconfirmed')
        else:
            evidence['checks'].append('jsf_symbol_absent')
    else:
        evidence['checks'].append('jsf_classification_source_unavailable')
    if state.get('seigen') == 'ok':
        restriction = sources.get('seigen', {}).get(code)
        if restriction:
            value = ' '.join(str(x or '') for x in restriction.values())
            if any(x in value for x in ('停止','注意','制限','規制','禁止')):
                evidence['jsf_restriction'] = 'yes'
                evidence['checks'].append('jsf_restricted')
                # Official negative overrides a contrary old manual row.
                row.update(jsf_restriction='yes', jsf_source=BASE+SOURCES['seigen'])
            else:
                evidence['checks'].append('jsf_restriction_details_ambiguous')
        else:
            evidence['jsf_restriction'] = 'no'
            # Not being listed does not prove the absence of all restrictions.
            # Therefore do not fill a positive 'no' from absence.
    else:
        evidence['checks'].append('jsf_restriction_source_unavailable')
    if state.get('zandaka') == 'ok':
        balance = sources.get('zandaka', {}).get(code)
        if balance:
            balance_date = parse_date(field(balance,'申込日','貸借申込日'))
            finance = num(field(balance,'融資残高株数','融資残高','融資残高株数株口'))
            shares = num(field(balance,'貸株残高株数','貸株残高','貸株残高株数株口'))
            evidence['jsf_balance_asof_date'] = balance_date
            if finance is not None and shares is not None:
                evidence['jsf_lending_excess_shares'] = max(shares-finance,0)
                if shares > finance and balance_date and \
                   target-timedelta(days=4) <= date.fromisoformat(balance_date) <= target:
                    evidence['checks'].append('jsf_lending_balance_deficit')
                    row['jsf_stock_shortage']='yes'
                    row['jsf_asof_date']=balance_date
                    row['jsf_source']=BASE+SOURCES['zandaka']
        else:
            evidence['checks'].append('jsf_balance_symbol_absent')
    else:
        evidence['checks'].append('jsf_balance_source_unavailable')
    if state.get('shina') == 'ok':
        f = sources.get('shina', {}).get(code)
        if f:
            d, fee, shortage = pick_fee(f)
            evidence['jsf_date'], evidence['jsf_fee'] = d, fee
            if shortage is not None:
                evidence['jsf_stock_shortage'] = 'yes' if shortage>0 else 'no'
            # Staleness is verified from inside the data, not from HTTP success.
            fresh = bool(d) and target - timedelta(days=4) <= date.fromisoformat(d) <= target
            if fresh:
                if fee is not None and fee > 0:
                    row['jsf_reverse_fee_yen'] = str(fee)
                    row['jsf_asof_date'] = d
                    row['jsf_source'] = BASE+SOURCES['shina']
                    evidence['checks'].append('jsf_positive_reverse_fee')
                elif fee == 0 and not row.get('jsf_reverse_fee_yen'):
                    row['jsf_reverse_fee_yen'] = '0'
                if shortage is not None:
                    if shortage>0:
                        row.update(jsf_stock_shortage='yes',jsf_asof_date=d,
                                   jsf_source=BASE+SOURCES['shina'])
                        evidence['checks'].append('jsf_stock_shortage')
                    elif not row.get('jsf_stock_shortage'):
                        row['jsf_stock_shortage'] = 'no'
                if not row.get('jsf_asof_date'):
                    row['jsf_asof_date'] = d
                if not row.get('jsf_source'):
                    row['jsf_source'] = BASE+SOURCES['shina']
            else:
                evidence['checks'].append('jsf_fee_stale')
        else:
            evidence['checks'].append('jsf_fee_symbol_absent')
    else:
        evidence['checks'].append('jsf_fee_source_unavailable')
    # Historical observations are counted by *loan application date*, not file
    # collection time, to prevent repeated downloads faking five-day coverage.
    obs = {}
    for historical in history + [source]:
        if historical.get('source_status',{}).get('shina')!='ok':
            continue
        f = historical.get('rows',{}).get('shina',{}).get(code)
        if not f:
            continue
        d, fee, shortage = pick_fee(f)
        if d and d <= target.isoformat() and fee is not None and shortage is not None:
            obs[d] = (fee, shortage > 0)
    last = sorted(obs,reverse=True)[:5]
    if (len(last)==5 and date.fromisoformat(last[0]) >= target-timedelta(days=4)
            and date.fromisoformat(last[-1]) >= target-timedelta(days=12)):
        row['jsf_last5_reverse_fees_yen'] = ';'.join(str(obs[x][0]) for x in last)
        row['jsf_last5_shortage'] = ';'.join('yes' if obs[x][1] else 'no' for x in last)
        if sum(obs[x][0]>0 for x in last)>=2 or sum(obs[x][1] for x in last)>=2:
            evidence['checks'].append('jsf_recent_shortage_or_fee_high_risk')
    else:
        evidence['checks'].append('jsf_five_distinct_dates_missing')
    # Classification is advisory unless another trustworthy dated source confirms.
    # A missing public classification still cannot silently become order-ready.
    # IMPORTANT: no JPX approval, SBI orderability, public SBI alert clearance,
    # nor issuer-confirmed earnings date is ever inferred from JSF data.
    return row,evidence


def candidate_codes(doc: dict) -> dict[str, dict]:
    keys = ('short_9_of_9_candidates','near_short_9_of_9_candidates',
            'short_8_of_9_candidates','short_7_of_9_candidates',
            'all_ma_below_candidates','near_all_ma_below_candidates',
            'near_all_ma_below_price_candidates','near_all_ma_below_touch_candidates')
    out={}
    for key in keys:
        for item in doc.get(key,[]):
            c=str(item['code'])
            out.setdefault(c, {'name':item.get('name',''), 'price':item.get('price')})
    return out


def csv_data(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding='utf-8-sig',newline='') as handle:
        rows = list(csv.DictReader(handle))
    result={}
    for row in rows:
        code=str(row.get('code') or '').strip()
        if not code or code in result:
            raise ValueError('Missing/duplicate code in manual checks')
        result[code]=row
    return result


def run(session: str, target: date, results_dir: Path=Path('results'),
        data_dir: Path=Path('data'), snapshot: dict | None=None):
    from short_trade_safety import REQUIRED_COLUMNS, evaluate
    src_path=results_dir/f'latest_short_{session}.json'
    doc=json.loads(src_path.read_text(encoding='utf-8'))
    if doc.get('target_date') != target.isoformat() or doc.get('session') != session:
        raise ValueError('Short scanner target/session mismatch')
    universe=candidate_codes(doc)
    # Only repeatable facts from the official JSF source are automatically set.
    snapshot = snapshot if snapshot is not None else collect(target)
    storage=results_dir/'short_evidence'
    storage.mkdir(parents=True,exist_ok=True)
    history=[]
    for p in sorted(storage.glob('*_snapshot.json')):
        try:
            old=json.loads(p.read_text(encoding='utf-8'))
            if old.get('schema') == 1 and p.name[:10] <= target.isoformat():
                history.append(old)
        except (ValueError,OSError):
            continue
    checks=csv_data(data_dir/f'short_trade_checks_{target}.csv')
    cols=['code',*REQUIRED_COLUMNS[1:], 'jsf_public_lending_status', 'sbi_public_alert_status',
          'sbi_public_alert_checked_date', 'sbi_public_alert_source']
    out=[]
    review=[]
    for code, item in sorted(universe.items()):
        verified, info=add_official(checks.get(code,{}),code,snapshot,history,target)
        verified['code']=code
        out.append({k: verified.get(k,'') for k in cols})
        gate=evaluate(verified,target)
        review.append({'code':code,'name':item['name'],'price':item['price'],
                       'jsf_classification':info['jsf_classification'],
                       'jsf_rate_asof_date':info['jsf_date'],
                       'jsf_reverse_fee_yen':info['jsf_fee'],
                       'jsf_stock_shortage':info['jsf_stock_shortage'],
                       'jsf_restriction':info['jsf_restriction'],
                       'jsf_evidence':';'.join(info['checks']),
                       'broker_status':'verified' if verified.get('sbi_system_sellable') == 'yes' and verified.get('sbi_checked_date') == target.isoformat() else 'verification_required',
                       'earnings_status':'verified' if verified.get('next_earnings_date') and verified.get('earnings_checked_date')==target.isoformat() else 'verification_required',
                       'final_status':gate['status']})
    output=storage/f'{target}_checks_{session}.csv'
    with output.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader();writer.writerows(out)
    report=storage/f'{target}_{session}_review.csv'
    with report.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=REPORT_COLS);writer.writeheader();writer.writerows(review)
    snap_path=storage/f'{target}_{session}_snapshot.json'
    # Persist only minimal observations for candidates, not the provider's
    # complete CSV data. Existing same-day snapshot is never overwritten.
    if not snap_path.exists():
        small = {'schema': 1, 'captured_at_jst': snapshot.get('captured_at_jst'),
                 'source_urls': snapshot.get('source_urls',{}),
                 'source_status': snapshot.get('source_status',{}),
                 'rows': {'shina': {}}}
        for code in universe:
            row=snapshot.get('rows',{}).get('shina',{}).get(code)
            if row:
                d, fee, shortage = pick_fee(row)
                small['rows']['shina'][code]={'申込日':d,
                       '品貸料率': '' if fee is None else str(fee),
                       '貸株超過株数': '' if shortage is None else str(shortage)}
        snap_path.write_text(json.dumps(small,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    stats={'target_date':target.isoformat(),'session':session,
           'candidates':len(out),'statuses':{k:sum(x['final_status']==k for x in review)
                    for k in ('eligible','excluded','verification_required')},
           'official_source_status':snapshot.get('source_status',{}),
           'generated_checks_path':str(output),'review_path':str(report),
           'source_urls':snapshot.get('source_urls',{})}
    (storage/f'{target}_{session}_status.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--session',required=True,choices=('noon','close'))
    parser.add_argument('--target-date',required=True,type=date.fromisoformat)
    parser.add_argument('--results-dir',type=Path,default=Path('results'))
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    a=parser.parse_args()
    print(json.dumps(run(a.session,a.target_date,a.results_dir,a.data_dir),ensure_ascii=False))