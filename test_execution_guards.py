import copy,json,os,tempfile,unittest
from unittest.mock import patch
from phoenix import *
from test_phoenix import T,S,rule,book,bar,order,F
import service_v2 as app

class GuardTest(unittest.TestCase):
 def state(self,active=True):
  st=initial();install_execution_policy(st,T-10000)
  if active:
   st['cash']=900;st['positions'][S].update(active=True,qty=1,basis=100,purchases=1,last_fill=100,threshold=99,entry_at=T-10000)
  return st
 def attempt(self,st,price=98,at=T,depth=100):
  st['positions'][S]['pending']=order(at=at-5000)
  return advance(st,S,bar(at,p=price),dict(F,prior_low=100),book(at,price,depth),rule(at),at)
 def test_dca_strict_previous_and_fee_average(self):
  for price in (100,100.01,99.95):
   st=self.state();self.attempt(st,price)
   self.assertEqual(st['positions'][S]['purchases'],1)
  st=self.state();ev=self.attempt(st,99.8);p=st['positions'][S]
  self.assertEqual(p['purchases'],2);self.assertLess(p['basis']/p['qty'],100)
  self.assertLess(p['last_fill'],100)
 def test_depth_never_crosses_guard(self):
  b=book(price=98);b['a']=[['98','.03'],['98.1','10']]
  f=marketable(b,rule(),'buy',7,T,max_buy_price=98.05)
  self.assertEqual(f['status'],'partial');self.assertEqual(f['price'],98)
  b=book(price=102);b['b']=[['101.898','.03'],['101.8','10']]
  f=marketable(b,rule(),'sell',1,T,min_sell_price=101.85)
  self.assertEqual(f['status'],'partial');self.assertAlmostEqual(f['price'],101.898)
 def test_fifteen_decreasing_buys(self):
  st=self.state(False)
  for i,n in enumerate(LADDER):
   at=T+i*MINUTE;price=100-i*2
   ev=self.attempt(st,price,at);self.assertEqual(st['positions'][S]['purchases'],i+1)
   self.assertEqual(next(e for e in ev if e['type']=='attempt')['budget'],n)
  p=st['positions'][S];self.assertLessEqual(p['basis'],150);self.assertGreater(p['basis'],149.99)
  self.attempt(st,60,T+15*MINUTE);self.assertEqual(p['purchases'],15)
 def test_all_fifteen_rungs_can_exit_profit(self):
  for n in range(1,16):
   st=self.state();p=st['positions'][S];p.update(purchases=n,pending=order('sell'))
   advance(st,S,bar(p=102),F,book(price=102),rule(),T)
   self.assertFalse(p['active']);self.assertGreater(st['realized'],0)
 def test_gap_profit_intent_cancels_and_dca_can_resume(self):
  st=self.state();p=st['positions'][S];p.update(peak=103,pending=order('sell'))
  advance(st,S,bar(p=98),F,book(price=98),rule(),T)
  self.assertTrue(p['active']);self.assertEqual(st['realized'],0);self.assertIsNone(p['pending']);self.assertIsNone(p['peak'])
  ev=self.attempt(st,97,T+MINUTE);self.assertEqual(p['purchases'],2)
 def test_profit_priority_over_queued_dca_and_new_trough(self):
  st=self.state();p=st['positions'][S];p['pending']=order()
  advance(st,S,bar(p=100,h=102,l=98),F,book(price=98),rule(),T)
  self.assertEqual(p['purchases'],1);self.assertIsNotNone(p['peak'])
  self.assertIsNone(p['pending'])
  self.assertEqual(signal(p,bar(T+MINUTE,p=101,h=102,l=100),F,T+MINUTE,guarded=True)['side'],'sell')
 def test_initial_real_quote_cap_all_profiles(self):
  for force in (False,True):
   st=self.state(False)
   advance(st,S,bar(p=100),dict(F,prior_low=100),book(price=100),rule(),T,force_entry=force)
   advance(st,S,bar(T+1000,p=102),dict(F,prior_low=100),book(T+1000,102),rule(T+1000),T+1000,force_entry=force)
   self.assertEqual(st['positions'][S]['purchases'],0)
   self.attempt(st,100,T+MINUTE);self.assertEqual(st['positions'][S]['purchases'],1)
 def test_amendment_cancels_old_buys_once(self):
  st=initial();p=st['positions'][S];p['pending']=order()
  ev=install_execution_policy(st,T);self.assertIsNone(p['pending'])
  self.assertEqual(len(ev[0]['cancelled_buys']),1)
  self.assertEqual(install_execution_policy(st,T+1),[])
 def test_continuing_fall_lowers_trough_without_buying(self):
  p=self.state()['positions'][S]
  for i,price in enumerate((98,97,96,95)):
   self.assertIsNone(signal(p,bar(T+i*MINUTE,p=price,h=price+.5,l=price),F,T+i*MINUTE,guarded=True))
   self.assertEqual(p['trough'],price)
  self.assertEqual(signal(p,bar(T+4*MINUTE,p=96.5,h=96.5,l=95),F,T+4*MINUTE,guarded=True)['side'],'buy')
 def test_amendment_persists_without_reset_and_deduplicates(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   path=os.path.join(d,'paper.db');db=Store(path)
   db.apply('old',lambda st:self.attempt(st,100));before=copy.deepcopy(db.state)
   db.apply('policy',lambda st:install_execution_policy(st,T));head=db.verify();saved=copy.deepcopy(db.state);db.db.close()
   for k in ('cash','realized','fees','positions'):self.assertEqual(before[k],saved[k])
   db=Store(path);self.assertEqual(db.verify(),head);self.assertEqual(db.state,saved)
   self.assertFalse(db.apply('policy',lambda st:install_execution_policy(st,T)));db.db.close()
 def test_purchase_history_reads_beyond_recent_ledger_and_account_scope(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   db=Store(os.path.join(d,'paper.db'));db.apply('buy',lambda st:self.attempt(st,100))
   for i in range(60):db.apply('quiet'+str(i),lambda st:[])
   with patch.dict(app.DBS,{'fixed':db,'weekly':None}):
    r=app.position_details('fixed',S);self.assertEqual(len(r['trades']),1)
    t=r['trades'][0];self.assertEqual(t['buy_number'],1);self.assertTrue(t['current_cycle'])
    self.assertAlmostEqual(t['usdt'],7);self.assertEqual(t['fee_currency'],'SOL')
    self.assertIsNone(app.position_details('weekly',S));self.assertIsNone(app.position_details('fixed','FAKE'))
   db.db.close()
if __name__=='__main__':unittest.main()

