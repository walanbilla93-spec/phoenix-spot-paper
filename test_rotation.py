import copy
import datetime as dt
import os
import tempfile
import unittest
from phoenix import *
from rotation import *
from service_v2 import active_symbols,summary,DBS,STATUSES
from selector import history
from test_phoenix import T,S,rule,book,bar,order,F

def snapshot():
    metrics=[dict(symbol=s,eligible=True,median_atr_ratio=v,structure=.1,median_daily_turnover=2000000,reasons=[])
        for s,v in [('BTCUSDT',.012),('SOLUSDT',.01),('ETHUSDT',.009),('BCHUSDT',.008),('DOGEUSDT',.007),('AVAXUSDT',.006)]]
    return dict(boundary=week_boundary(T),available_at=week_boundary(T)+FIVE,recorded_at=T,
        selection_hash=SELECTION_HASH,evidence_hash='synthetic',selected=[r['symbol'] for r in ranking(metrics)[:5]],metrics=metrics)

class RotationTest(unittest.TestCase):
    def test_weekly_clock_and_availability(self):
        monday=int(dt.datetime(2026,10,26,tzinfo=dt.timezone.utc).timestamp()*1000)
        self.assertEqual(week_boundary(monday+299999),monday-7*DAY)
        self.assertEqual(week_boundary(monday+300000),monday)
        st=weekly_initial();snap=snapshot();snap['recorded_at']=snap['available_at']-1
        with self.assertRaises(ValueError):apply_selection(st,snap,'weekly')

    def test_native_history_metrics_no_future(self):
        b=week_boundary(T)
        hours=[[b-40*DAY+i*HOUR,100,101,99,100,100,100000] for i in range(960)]
        five=[[b-30*DAY+i*FIVE,100,101,99,100,10,10000] for i in range(8640)]
        row=candidate('BTCUSDT',five,hours,b)
        self.assertTrue(row['eligible']);self.assertAlmostEqual(row['median_atr_ratio'],.02)
        self.assertEqual(row['median_daily_turnover'],2400000)
        self.assertEqual(candidate('BTCUSDT',five+[[b,1,2,.1,1,1,999999]],hours+[[b,1,2,.1,1,1,999999]],b),row)
        self.assertFalse(candidate('BTCUSDT',five[10:],hours,b)['eligible'])
        self.assertFalse(candidate('TRXUSDT',five,hours,b)['eligible'])

    def test_tiebreak_and_highest_mover_fifth(self):
        snap=snapshot();st=initial();prior=copy.deepcopy(st)
        events=apply_selection(st,snap,'fixed')
        self.assertEqual(st['replacement']['symbol'],'BTCUSDT')
        self.assertNotIn('XMRUSDT',active_symbols(st,'fixed'))
        self.assertEqual(len(active_symbols(st,'fixed')),5)
        self.assertEqual(st['cash'],prior['cash']);self.assertEqual(st['positions'][S],prior['positions'][S])
        self.assertEqual(events[0]['type'],'fixed_fifth_amendment')
        self.assertFalse(apply_selection(st,snap,'fixed'))
        for row in snap['metrics']:row['median_atr_ratio']=.01;row['structure']=0;row['median_daily_turnover']=1e6
        self.assertEqual([r['symbol'] for r in ranking(snap['metrics'])],sorted(r['symbol'] for r in snap['metrics']))

    def test_dropped_position_kept_and_managed(self):
        st=weekly_initial();snap=snapshot();apply_selection(st,snap,'weekly')
        st['cash']=900;st['positions']['BTCUSDT'].update(active=True,qty=1,basis=100,purchases=1,pending=order('sell'))
        snap=copy.deepcopy(snap)
        for r in snap['metrics']:r['eligible']=r['symbol']=='ETHUSDT'
        snap['selected']=['ETHUSDT'];apply_selection(st,snap,'weekly')
        self.assertIn('BTCUSDT',active_symbols(st,'weekly'))
        b=book();b['s']='BTCUSDT'
        ev=advance(st,'BTCUSDT',bar(),F,b,rule(),T,allow_entry=False)
        self.assertFalse(st['positions']['BTCUSDT']['active']);self.assertGreater(st['cash'],900)
        weekly_audit(st)

    def test_not_selected_blocks_entry_not_dca(self):
        st=initial();p=st['positions'][S];p['pending']=order()
        advance(st,S,bar(),F,book(),rule(),T,allow_entry=False)
        self.assertEqual(p['qty'],0);self.assertIsNone(p['pending'])
        p.update(active=True,qty=1,basis=100,purchases=1,pending=order());st['cash']=900
        advance(st,S,bar(),F,book(),rule(),T,allow_entry=False)
        self.assertEqual(p['purchases'],2)

    def test_separate_durable_accounts_and_restart(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
            fixed=Store(os.path.join(d,'fixed.db'));weekly=Store(os.path.join(d,'weekly.db'),factory=weekly_initial,validator=weekly_audit)
            weekly.apply('select',lambda st:apply_selection(st,snapshot(),'weekly'))
            fixed.apply('amend',lambda st:apply_selection(st,snapshot(),'fixed'))
            self.assertEqual(fixed.state['cash'],1000);self.assertEqual(weekly.state['cash'],1000)
            old=copy.deepcopy(weekly.state);head=weekly.verify();weekly.db.close()
            weekly=Store(os.path.join(d,'weekly.db'),factory=weekly_initial,validator=weekly_audit)
            self.assertEqual(weekly.state,old);self.assertEqual(weekly.verify(),head)
            self.assertFalse(weekly.apply('select',lambda st:apply_selection(st,snapshot(),'weekly')))
            weekly.db.close();fixed.db.close()

    def test_reason_explains_idle_without_forcing_purchase(self):
        p=position();t=dict(feature=dict(close=100,prior_low=98,dip=False))
        self.assertIn('above entry ceiling',entry_reason(p,t,True))
        self.assertIn('stabilization',entry_reason(p,dict(feature=dict(close=98.5,prior_low=98,dip=False)),True))
        self.assertIn('Outside',entry_reason(p,t,False))

    def test_history_paginates_backwards_without_future(self):
        class Client:
            def get(self,e,s,**p):
                end=p['end'];start=p['start'];times=list(range(start,end+1,1))[-1000:]
                return {'list':[[str(t),'100','101','99','100','1','10'] for t in reversed(times)]},{}
        rr,_=history(Client(),'BTCUSDT',5,0,2499)
        self.assertEqual(len(rr),2500);self.assertEqual(rr[0][0],0);self.assertEqual(rr[-1][0],2499)

if __name__=='__main__':unittest.main(verbosity=2)
