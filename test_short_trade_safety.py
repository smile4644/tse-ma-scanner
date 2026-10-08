import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from short_trade_safety import GROUPS, apply_gate, evaluate, read_checks, run

TARGET = date(2026, 10, 8)


def row(**overrides):
    base = {
        'sbi_system_sellable': 'yes', 'sbi_checked_date': '2026-10-08',
        'sbi_source': 'SBI order-screen check', 'jsf_asof_date': '2026-10-07',
        'jsf_source': 'JSF official report', 'jsf_restriction': 'no',
        'jsf_reverse_fee_yen': '0', 'jsf_stock_shortage': 'no',
        'earnings_checked_date': '2026-10-08', 'earnings_source': 'JPX issuer notice',
        'next_earnings_date': '2026-10-23',
        'jpx_checked_date': '2026-10-08',
        'jpx_source': 'JPX official margin lending list and updates',
        'jpx_lending_eligible': 'yes',
        'jsf_last5_reverse_fees_yen': '0;0;0;0;0',
        'jsf_last5_shortage': 'no;no;no;no;no',
    }
    base.update(overrides)
    return base


class RiskGateTests(unittest.TestCase):
    def test_missing_evidence_fails_closed(self):
        self.assertEqual(evaluate(None, TARGET)['status'], 'verification_required')
    def test_all_verified_passes_conditionally(self):
        self.assertTrue(evaluate(row(), TARGET)['order_ready'])
    def test_sbi_denied_is_excluded(self):
        self.assertEqual(evaluate(row(sbi_system_sellable='no'), TARGET)['status'], 'excluded')
    def test_jsf_risk_is_excluded(self):
        for change in ({'jsf_reverse_fee_yen': '0.05'},
                       {'jsf_restriction': 'yes'}, {'jsf_stock_shortage': 'yes'}):
            with self.subTest(change=change):
                self.assertEqual(evaluate(row(**change), TARGET)['status'], 'excluded')
    def test_recent_reverse_fee_risk_is_excluded_even_with_current_zero(self):
        for history in ('0;0.05;0;0.05;0', '0;0;0;0.10;0'):
            with self.subTest(history=history):
                result = evaluate(row(jsf_last5_reverse_fees_yen=history), TARGET)
                self.assertIn('jsf_recent_fee_high_risk', result['flags'])
                self.assertEqual(result['status'], 'excluded')

    def test_recent_shortage_risk_is_excluded(self):
        result = evaluate(row(jsf_last5_shortage='no;yes;no;yes;no'), TARGET)
        self.assertEqual(result['status'], 'excluded')

    def test_missing_history_is_not_eligible(self):
        for update in ({'jsf_last5_reverse_fees_yen': ''},
                       {'jsf_last5_shortage': 'no;no;no'}):
            with self.subTest(update=update):
                self.assertEqual(evaluate(row(**update), TARGET)['status'],
                                 'verification_required')

    def test_jpx_not_lendable_and_unknown(self):
        self.assertEqual(evaluate(row(jpx_lending_eligible='no'), TARGET)['status'], 'excluded')
        self.assertEqual(evaluate(row(jpx_lending_eligible=''), TARGET)['status'], 'verification_required')

    def test_earnings_within_week_is_excluded(self):
        self.assertEqual(evaluate(row(next_earnings_date='2026-10-15'), TARGET)['status'], 'excluded')
    def test_future_not_known_is_unverified(self):
        self.assertEqual(evaluate(row(next_earnings_date=''), TARGET)['status'], 'verification_required')
    def test_stale_checks_not_tradeable(self):
        for change in ({'sbi_checked_date': '2026-10-07'},
                       {'jsf_asof_date': '2026-09-30'},
                       {'earnings_checked_date': '2026-10-07'}):
            with self.subTest(change=change):
                self.assertFalse(evaluate(row(**change), TARGET)['order_ready'])
    def test_all_six_research_groups_preserved(self):
        doc = {'target_date': TARGET.isoformat(), **{
            group: [{'code': str(i+1000), 'price': 110}] for i, group in enumerate(GROUPS)}}
        apply_gate(doc, {'1000': row()})
        self.assertEqual(doc['trade_safety_gate']['counts']['eligible'], 1)
        self.assertEqual(doc['trade_safety_gate']['counts']['verification_required'], 5)
        self.assertEqual(len(doc['short_9_of_9_candidates']), 1)
        self.assertTrue(doc['short_9_of_9_candidates'][0]['trade_safety']['order_ready'])
    def test_archive_latest_mismatch_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            (p/'short_archive').mkdir()
            base = {'target_date': TARGET.isoformat(), 'session': 'close',
                    'source_generated_at_jst': '2026-10-08T16:00:00+09:00',
                    **{group: [] for group in GROUPS}}
            (p/'short_archive'/'2026-10-08_close.json').write_text(json.dumps(base))
            base['source_generated_at_jst'] = '2026-10-08T17:00:00+09:00'
            (p/'latest_short_close.json').write_text(json.dumps(base))
            with self.assertRaises(ValueError):
                run(p, 'close', TARGET)


if __name__ == '__main__':
    unittest.main()
