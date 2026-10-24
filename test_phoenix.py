import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from phoenix import *

T=1800000000000
S='SOLUSDT'
def rule(at=T):
    return dict(step=.000001,quote_step=.000001,minimum=5,maximum=100000,tick=.000001,at=at)
def book(at=T,price=100,depth=100):
    return dict(s=S,ts=at,received_at=at,a=[[str(price),str(depth)]],b=[[str(price*.999),str(depth)]],u=at)
def order(side='buy',at=T-5000):
    return dict(side=side,reason='test',signal_at=at,observed_at=at,actionable_at=at,expires_at=at+60000,atr=1)
def bar(at=T,p=100,h=None,l=None):
    t=at//MINUTE*MINUTE-MINUTE
    return [t,p,h or p,l or p,p,100,10000]
F=dict(dip=True,atr=1)

class EngineTest(unittest.TestCase):
    def test_ladder_exact_fifteen(self):
        st=initial();p=st['positions'][S]
        for i,budget in enumerate(LADDER):
            at=T+i*60000;p['pending']=order(at=at-5000)
            ev=advance(st,S,bar(at),F,book(at),rule(at),at)
            self.assertEqual(p['purchases'],i+1)
            self.assertEqual(next(e for e in ev if e['type']=='attempt')['budget'],budget)
            self.assertLessEqual(p['basis'],sum(LADDER[:i+1])+1e-8)
        self.assertEqual(len(LADDER),15);self.assertEqual(sum(LADDER),150)
        self.assertAlmostEqual(p['basis'],150,places=5)
        p['pending']=order(at=T+900000-5000)
        advance(st,S,bar(T+900000),F,book(T+900000),rule(T+900000),T+900000)
        self.assertEqual(p['purchases'],15)

    def test_reserve_and_basis(self):
        st=initial()
        for s in SYMBOLS:
            st['positions'][s].update(active=True,qty=1,basis=150,purchases=15)
        st['cash']=250;audit(st)
        p=st['positions'][S];p.update(basis=143,purchases=7);st['cash']=257
        p['pending']=order()
        advance(st,S,bar(),F,book(),rule(),T)
        self.assertGreaterEqual(st['cash'],250-1e-8)
        p['purchases']=8;p['pending']=order(at=T+60000-5000)
        ev=advance(st,S,bar(T+60000),F,book(T+60000),rule(T+60000),T+60000)
        self.assertEqual(next(e for e in ev if e['type']=='attempt')['fill']['status'],'rejected_risk')
        self.assertGreaterEqual(st['cash'],250-1e-8)

    def test_tp_old_peak_next_observation(self):
        p=position();p.update(active=True,qty=1,basis=100,purchases=1)
        b=bar(T,p=101.6,h=102,l=100)
        self.assertIsNone(signal(p,b,F,T+1000)) # newly armed cannot sell
        b=bar(T+60000,p=101.5,h=103,l=101)
        self.assertEqual(signal(p,b,F,T+61000)['side'],'sell')
        st=initial();st['cash']=900;st['positions'][S]=p
        ev=advance(st,S,b,F,book(T+61000,101.5),rule(T+61000),T+61000)
        self.assertFalse(any(e['type']=='attempt' for e in ev))
        ev=advance(st,S,b,F,book(T+62000,101.5),rule(T+62000),T+62000)
        self.assertEqual(next(e for e in ev if e['type']=='attempt')['side'],'sell');self.assertFalse(p['active'])
        self.assertEqual(p['cycles'],1)

    def test_dca_old_trough_no_new_low(self):
        p=position();p.update(active=True,qty=1,basis=100,purchases=1,threshold=99)
        self.assertIsNone(signal(p,bar(T,p=99,h=101,l=98),F,T+1000))
        self.assertEqual(p['trough'],98)
        self.assertIsNone(signal(p,bar(T+60000,p=98,h=101,l=97),F,T+61000))
        self.assertEqual(p['trough'],97)
        self.assertEqual(signal(p,bar(T+120000,p=99,h=99,l=97),F,T+121000)['side'],'buy')

    def test_atr_threshold_uses_fill(self):
        st=initial();p=st['positions'][S];p['pending']=order()
        p['pending']['atr']=4
        advance(st,S,bar(),F,book(),rule(),T)
        self.assertAlmostEqual(p['threshold'],97)

    def test_depth_slip_partial_and_minimum(self):
        b=book();b['a']=[['100','.03'],['100.1','.02'],['101','10']]
        f=marketable(b,rule(),'buy',7,T)
        self.assertEqual(f['status'],'partial');self.assertAlmostEqual(f['qty'],.05)
        self.assertLessEqual(f['price'],100.15)
        self.assertEqual(marketable(b,rule(),'buy',4,T)['status'],'rejected_minimum')
        self.assertEqual(marketable(book(depth=.00000001),rule(),'buy',7,T)['status'],'rejected_depth')

    def test_partial_buy_consumes_rung_no_topup(self):
        st=initial();p=st['positions'][S];p['pending']=order()
        advance(st,S,bar(),F,book(depth=.03),rule(),T)
        self.assertEqual(p['purchases'],1);self.assertIsNone(p['pending'])
        self.assertAlmostEqual(p['basis'],3)

    def test_partial_sell_retains_inventory_and_dust(self):
        st=initial();p=st['positions'][S];st['cash']=900
        p.update(active=True,qty=1.0000004,basis=100,purchases=3,pending=order('sell'))
        b=book(depth=.5)
        advance(st,S,bar(),F,b,rule(),T)
        self.assertTrue(p['active']);self.assertIsNotNone(p['pending'])
        advance(st,S,bar(T+60000),F,book(T+60000,depth=1),rule(T+60000),T+60000)
        self.assertFalse(p['active']);self.assertGreater(p['qty'],0);self.assertGreater(p['basis'],0)
        audit(st)

    def test_stale_future_crossed_and_missing(self):
        for b in (book(T-2001),book(T+1),dict(book(),a=[]),dict(book(),b=[['101','1']])):
            with self.assertRaises(ValueError): valid_book(b,T,S)
        st=initial();before=copy.deepcopy(st)
        with self.assertRaises(ValueError):advance(st,S,bar(),F,book(T-3000),rule(),T)
        self.assertEqual(st,before)

    def test_buy_expiry_and_data_halt(self):
        for at,allowed in ((T+60000,True),(T,False)):
            st=initial();p=st['positions'][S];p['pending']=order()
            advance(st,S,bar(at),F,book(at),rule(at),at,allow_buy=allowed)
            self.assertEqual(p['qty'],0)
        st=initial();p=st['positions'][S];st['cash']=900
        p.update(active=True,qty=1,basis=100,purchases=1,pending=order('sell'))
        advance(st,S,bar(),F,book(),rule(),T,allow_buy=False)
        self.assertFalse(p['active']) # monitor/manage existing inventory during unrelated failure

    def test_restart_dedup_rollback_hash(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
            path=os.path.join(d,'state.db');db=Store(path)
            def fn(st):
                st['positions'][S]['pending']=order()
                return advance(st,S,bar(),F,book(),rule(),T)
            self.assertTrue(db.apply('same',fn));saved=copy.deepcopy(db.state);head=db.verify();db.db.close()
            db=Store(path);self.assertEqual(db.state,saved);self.assertEqual(db.verify(),head)
            self.assertFalse(db.apply('same',fn));self.assertEqual(db.state,saved)
            def fail(st): st['cash']=0;raise ValueError('crash')
            with self.assertRaises(ValueError): db.apply('crash',fail)
            self.assertEqual(db.state,saved);self.assertEqual(db.verify(),head)
            with self.assertRaises(sqlite3.IntegrityError):db.db.execute('DELETE FROM events')
            db.db.close()

    def test_no_same_bar_signal_after_fill(self):
        st=initial();p=st['positions'][S];p['pending']=order()
        advance(st,S,bar(p=100,h=1000,l=1),F,book(),rule(),T)
        ev=advance(st,S,bar(T+1000,p=100,h=1000,l=1),F,book(T+1000),rule(T+1000),T+1000)
        self.assertFalse(any(e['type']=='signal' for e in ev));self.assertIsNone(p['peak']);self.assertIsNone(p['trough'])

    def test_hour_feature_no_lookahead_and_gap(self):
        end=T//HOUR*HOUR
        rr=[[end-(241-i)*HOUR,100,101,99.1,100,1,100] for i in range(241)]
        at=end+300000
        a=feature(rr,at);self.assertTrue(a['dip']);self.assertEqual(a['hours'],241)
        future=[end,100,1000000,.01,999999,1,1]
        self.assertEqual(feature(rr+[future],at),a)
        with self.assertRaises(ValueError):feature(rr[:10]+rr[11:],at)
        with self.assertRaises(ValueError):feature(rr,at+HOUR)

    def test_spot_rule_and_network_allowlist(self):
        with self.assertRaises(ValueError):PublicSpot().get('order/create',S)
        with self.assertRaises(ValueError):PublicSpot().get('kline','BTCUSDT')
        with self.assertRaises(ValueError):parse_rule({'list':[]},'XMRUSDT',T)
        x=dict(symbol=S,baseCoin='SOL',quoteCoin='USDT',status='Trading',
               lotSizeFilter=dict(basePrecision='.001',quotePrecision='.01',minOrderAmt='5',maxMarketOrderQty='100'),priceFilter=dict(tickSize='.01'))
        self.assertEqual(parse_rule({'list':[x]},S,T)['minimum'],5)

if __name__=='__main__':unittest.main(verbosity=2)
