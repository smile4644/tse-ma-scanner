import unittest
from buy_day_audit import validate


def source(day, picks, symbol, today_picks=None):
    doc={'write_status':'accepted','safety_gate_errors':[],'schema_version':19,
         'session':'close','target_date':day,'price_series_mode':'normal_close_auto_adjust_false',
         'candidates': picks, 'near_9_of_9_candidates':[],
         'all_ma_above_candidates':[], 'near_all_ma_above_price_candidates':[],
         'near_all_ma_above_touch_candidates':[],
         'diagnostics':{'symbols':symbol}}
    return doc

class BacktestTests(unittest.TestCase):
    def test_today_selections_are_not_used(self):
        a=source('2026-10-07',[{'code':'1111','name':'A','score':9,'price':100}],[])
        b=source('2026-10-08',[{'code':'2222','name':'B','score':9,'price':123}],
          [{'code':'1111','price':99,'quarantined':False,'last_bar':'2026-10-08','day_low':97},
           {'code':'2222','price':123,'quarantined':False,'last_bar':'2026-10-08'}])
        z=validate(a,b,100,98)
        self.assertEqual(z['summary']['①9/9']['selected'],1)
        self.assertEqual(z['summary']['①9/9']['matched'],1)
        self.assertEqual(z['rows'][0]['code'],'1111')
        self.assertTrue(z['rows'][0]['beat_benchmark'])
    def test_same_day_intraday_datetime_is_accepted(self):
        a=source('2026-10-07',[{'code':'1111','name':'A','score':9,'price':100}],[])
        b=source('2026-10-08',[],[{'code':'1111','price':98,'quarantined':False,
          'last_bar':'2026-10-08T15:25:00+09:00'}])
        z=validate(a,b,100,99)
        self.assertEqual(z['summary']['①9/9']['matched'],1)
        self.assertEqual(z['rows'][0]['status'],'matched')

    def test_stale_intraday_datetime_is_rejected(self):
        a=source('2026-10-07',[{'code':'1111','name':'A','score':9,'price':100}],[])
        b=source('2026-10-08',[],[{'code':'1111','price':98,'quarantined':False,
          'last_bar':'2026-10-07T15:25:00+09:00'}])
        z=validate(a,b,100,99)
        self.assertEqual(z['summary']['①9/9']['matched'],0)
        self.assertEqual(z['rows'][0]['status'],'date_mismatch')

    def test_quarantine_reported_not_counted(self):
        a=source('2026-10-07',[{'code':'1111','name':'A','score':9,'price':100}],[])
        b=source('2026-10-08',[],[{'code':'1111','price':99,'quarantined':True,'last_bar':'2026-10-08'}])
        z=validate(a,b,100,98)
        self.assertEqual(z['summary']['①9/9']['matched'],0)
        self.assertEqual(z['summary']['①9/9']['missing'][0]['reason'],'quarantined_today')
    def test_refuse_bad_source(self):
        a=source('2026-10-07',[],[])
        b=source('2026-10-08',[],[])
        a['safety_gate_errors']=['bad']
        with self.assertRaises(ValueError):validate(a,b,100,98)

if __name__=='__main__':unittest.main()

