import csv
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
import short_evidence_collect as c

D=date(2026,10,9)

def rows(fee='0.00', shortage='0', asof='20261008', classification='1', balance_lend='50', restricted=False):
    x={
        'meigara':[{'コード':'8278','貸借区分':classification}],
        'shina':[{'コード':'8278','貸借申込日':asof,'当日品貸料率（円）':fee,'貸株超過株数':shortage}],
        'zandaka':[{'コード':'8278','申込日':asof,'融資残高株数': '100','貸株残高株数':balance_lend}],
        'seigen':[{'コード':'9999','制限措置':'申込停止'}] if not restricted else [{'コード':'8278','制限措置':'申込停止'}],
    }
    return c.official_snapshot(x,D)

class ParseTests(unittest.TestCase):
    def test_normal(self):
        x=rows()
        self.assertEqual(set(x['source_status'].values()),{'ok'})
        a,b=c.add_official({},'8278',x,[],D)
        self.assertEqual(b['jsf_classification'],'loanable')
        self.assertEqual(a['jsf_reverse_fee_yen'],'0')
        self.assertEqual(a['jsf_stock_shortage'],'no')
        self.assertNotIn('sbi_system_sellable',a)
        self.assertNotIn('next_earnings_date',a)
        self.assertNotIn('jpx_lending_eligible',a)
        self.assertFalse(a.get('jsf_last5_reverse_fees_yen'))
    def test_prefatory_metadata_rows_skipped(self):
        sample=('貸借取引対象銘柄一覧,,\n20261009,,\n'
                'コード,銘柄名,貸借区分\n8278,フジ,1\n')
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self, n): return sample.encode('utf-8')
        with patch('urllib.request.urlopen',return_value=FakeResponse()):
            rows=c.download_csv('https://www.taisyaku.jp/data/meigara.csv')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['コード'],'8278')

    def test_bad_schema_fails_closed(self):
        x=rows(); x=c.official_snapshot({'shina':[{'code':'8278','fee':'0'}]},D)
        self.assertTrue(x['source_status']['shina'].startswith('invalid'))
        a,b=c.add_official({},'8278',x,[],D)
        self.assertNotIn('jsf_reverse_fee_yen',a)
    def test_positive_fee_never_overridden_by_manual_zero(self):
        x=rows(fee='0.05')
        a,b=c.add_official({'jsf_reverse_fee_yen':'0'},'8278',x,[],D)
        self.assertEqual(a['jsf_reverse_fee_yen'],'0.05')
        self.assertIn('jsf_positive_reverse_fee',b['checks'])
    def test_positive_shortage_from_balances(self):
        x=rows(balance_lend='200')
        a,b=c.add_official({},'8278',x,[],D)
        self.assertEqual(a['jsf_stock_shortage'],'yes')
        self.assertIn('jsf_lending_balance_deficit',b['checks'])
    def test_negative_classification_and_restriction(self):
        x=rows(classification='2',restricted=True)
        a,b=c.add_official({},'8278',x,[],D)
        self.assertEqual(a['jsf_public_lending_status'],'not_loanable')
        self.assertEqual(a['jsf_restriction'],'yes')
    def test_stale_does_not_approve(self):
        x=rows(fee='0.0',asof='20260101')
        a,b=c.add_official({},'8278',x,[],D)
        self.assertNotIn('jsf_reverse_fee_yen',a)
        self.assertIn('jsf_fee_stale',b['checks'])
    def test_duplicated_snapshots_do_not_fake_history(self):
        x=rows()
        a,b=c.add_official({},'8278',x,[x,x,x,x],D)
        self.assertNotIn('jsf_last5_reverse_fees_yen',a)
    def test_five_distinct_recent_rates(self):
        histories=[]
        for delta in [1,2,3,4]:
            d=(D-timedelta(days=delta)).strftime('%Y%m%d')
            histories.append(rows(asof=d, fee='0.0'))
        x=rows(asof=D.strftime('%Y%m%d'))
        a,b=c.add_official({},'8278',x,histories,D)
        self.assertEqual(a['jsf_last5_reverse_fees_yen'],'0.0;0.0;0.0;0.0;0.0')
    def test_preserve_verified_manual_values(self):
        x=rows()
        manual={'sbi_system_sellable':'yes','sbi_checked_date':D.isoformat(),
                'earnings_checked_date':D.isoformat(),'next_earnings_date':'2026-10-27'}
        a,_=c.add_official(manual,'8278',x,[],D)
        self.assertEqual(a['sbi_system_sellable'],'yes')
        self.assertEqual(a['next_earnings_date'],'2026-10-27')
    def test_sbi_official_alert_parsing_and_no_false_clearance(self):
        html = ('<p>本日の注意銘柄 更新 10/9（金） 20:00</p>'
                '<table><tr><td>ヤマタネ</td><td>9305</td>'
                '<td>日証金</td><td>新規売停止</td></tr>'
                '<tr><td>CAICA</td><td>2315</td>'
                '<td>日証金</td><td>貸株注意喚起</td></tr>'
                '<tr><td>別銘柄</td><td>9999</td>'
                '<td>東証</td><td>日々公表</td></tr></table>')
        parsed=c.parse_sbi_public_alerts(html,D)
        self.assertEqual(parsed['asof_date'],D.isoformat())
        self.assertEqual(parsed['alerts']['9305'],'new_sell_suspended')
        self.assertEqual(parsed['alerts']['2315'],'loan_caution')
        self.assertNotIn('9999',parsed['alerts'])
    def test_sbi_public_alert_overrides_manual_clearance(self):
        x=rows(); x['sbi_public']={'asof_date':D.isoformat(),
                    'alerts':{'8278':'loan_caution'}}
        a,b=c.add_official({'sbi_public_alert_status':'none'},'8278',x,[],D)
        self.assertEqual(a['sbi_public_alert_status'],'loan_caution')
    def test_sbi_missing_update_date_not_treated_as_fresh(self):
        r=c.parse_sbi_public_alerts('<table><tr><td>会社</td><td>8278</td><td>日証金</td><td>新規売停止</td></tr></table>',D)
        self.assertEqual(r['status'],'unverified_date')
        x=rows(); x['sbi_public']=r
        a,b=c.add_official({},'8278',x,[],D)
        self.assertNotIn('sbi_public_alert_status',a)

    def test_csv_field_names(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.csv'
            p.write_text('code,foo\n8278,yes\n8278,no\n')
            with self.assertRaises(ValueError):
                c.csv_data(p)
    def test_collection_failure_kept_unverified(self):
        with patch.object(c,'download_csv',side_effect=TimeoutError('test timeout')):
            x=c.collect(D)
            self.assertEqual(set(x['source_status']),set(c.SOURCES))
            self.assertEqual(sum(bool(v) for v in x['rows'].values()),0)

if __name__=='__main__':unittest.main()