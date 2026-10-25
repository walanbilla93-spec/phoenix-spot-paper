import copy,os,tempfile,unittest
from phoenix import *
from refill import *
from rotation import weekly_initial,weekly_audit,apply_selection
from test_rotation import snapshot
from test_phoenix import T,S,rule,book,bar,order,F
from service_v2 import active_symbols,selected_symbols

def candles(at=T,price=100):
 return [[at-(30-i)*MINUTE,price,price+.1,price-.1,price,1,100] for i in range(30)]
def plan(at=T,price=100):return recent_setup(candles(at,price),at+1000)
def state():
 st=initial();install_execution_policy(st,T-10000);install_cycle_entry_policy(st,T-9000);return st

class RefillTest(unittest.TestCase):
 def test_new_cycle_above_old_low_and_previous_cycle_price(self):
  st=state();p=st['positions'][S]
  p.update(qty=.0001,basis=.01,last_fill=80,last_exit=T-10*MINUTE,entry_at=T-20*MINUTE,cycles=1)
  st['cash']=999.99;f=dict(F,dip=False,prior_low=70)
  advance(st,S,bar(),f,book(T+1000),rule(T+1000),T+1000,entry_plan=plan())
  self.assertEqual(p['pending']['reason'],'fresh_cycle_stabilization')
  self.assertEqual(p['purchases'],0)
  advance(st,S,bar(),f,book(T+2000,100.1),rule(T+2000),T+2000,entry_plan=plan())
  self.assertTrue(p['active']);self.assertEqual(p['purchases'],1);self.assertEqual(p['cycles'],1)
  self.assertGreater(p['last_fill'],80);self.assertEqual(p['entry_at'],T+2000)
 def test_missing_or_red_setup_can_arrive_later_same_minute(self):
  st=state();p=st['positions'][S]
  advance(st,S,bar(),F,book(T+1000),rule(T+1000),T+1000,entry_plan={})
  self.assertIsNone(p['pending']);self.assertIsNone(p['last_bar'])
  advance(st,S,bar(),F,book(T+2000),rule(T+2000),T+2000,entry_plan=plan())
  self.assertIsNotNone(p['pending'])
 def test_no_chasing_no_phantom_and_no_same_observation_fill(self):
  st=state();p=st['positions'][S]
  advance(st,S,bar(),F,book(T+1000),rule(T+1000),T+1000,entry_plan=plan())
  self.assertEqual(p['qty'],0)
  ev=advance(st,S,bar(),F,book(T+2000,101),rule(T+2000),T+2000,entry_plan=plan())
  self.assertEqual(p['qty'],0);self.assertEqual(st['cash'],1000)
  self.assertEqual(next(e for e in ev if e['type']=='attempt')['fill']['status'],'rejected_price_guard')
 def test_completed_history_gap_future_and_decline(self):
  rr=candles();base=recent_setup(rr,T+1000)
  self.assertEqual(recent_setup(rr+[[T,1,999,.01,500,1,1]],T+1000),base)
  with self.assertRaises(ValueError):recent_setup(rr[:5]+rr[6:],T+1000)
  self.assertFalse(recent_setup(rr,T+56000)['ready'])
  rr[-1][4]=99.95;self.assertFalse(recent_setup(rr,T+1000)['ready'])
 def test_cooldown_and_stale_plan(self):
  p=position();p['last_exit']=T-MINUTE
  self.assertFalse(fresh_entry_valid(p,plan(),bar(),T+1000))
  p['last_exit']=T-3*MINUTE;self.assertTrue(fresh_entry_valid(p,plan(),bar(),T+1000))
  self.assertFalse(fresh_entry_valid(p,plan(),bar(T+MINUTE),T+MINUTE+1000))
 def test_dca_still_must_average_down_and_existing_tp_unchanged(self):
  for price,expected in ((100,1),(98,2)):
   st=state();st['cash']=900;p=st['positions'][S]
   p.update(active=True,qty=1,basis=100,purchases=1,last_fill=100,pending=order())
   advance(st,S,bar(p=price),F,book(price=price),rule(),T,entry_plan={})
   self.assertEqual(p['purchases'],expected)
  p['pending']=order('sell',T+MINUTE-5000)
  advance(st,S,bar(T+MINUTE,p=102),F,book(T+MINUTE,102),rule(T+MINUTE),T+MINUTE,entry_plan={})
  self.assertGreater(st['realized'],0);self.assertFalse(p['active'])
 def test_allocator_prefers_unused_and_keeps_open_and_pending(self):
  st=weekly_initial('weekly_five');install_execution_policy(st,T);install_cycle_entry_policy(st,T)
  snap=snapshot();apply_selection(st,snap,'weekly_five')
  symbols=entry_pool(st,'weekly_five')
  st['positions'][symbols[0]].update(active=True,qty=.07,basis=7,purchases=1,entry_at=T-5*MINUTE)
  st['cash']=993
  st['positions'][symbols[1]]['pending']=order()
  st['positions'][symbols[2]]['entry_at']=T-10*MINUTE
  plans={s:dict(plan(),strength_5m=.9 if s==symbols[2] else .01) for s in symbols}
  inputs=refill_inputs(st,'weekly_five',plans,T+1000)
  apply_refill_allocation(st,inputs,T+1000)
  a=st['refill_allocation'];self.assertEqual(len(a['selected']),5)
  self.assertIn(symbols[0],a['selected']);self.assertIn(symbols[1],a['selected'])
  self.assertTrue(a['ranked'][0]['never_traded'])
  self.assertEqual(st['positions'][symbols[0]]['basis'],7);weekly_audit(st)
  self.assertEqual(set(active_symbols(st,'weekly_five')),set(a['selected']))
 def test_allocation_replay_and_policy_restart_dedup(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   path=os.path.join(d,'paper.db');db=Store(path,factory=lambda:weekly_initial('weekly_five'),validator=weekly_audit)
   db.apply('selection',lambda st:apply_selection(st,snapshot(),'weekly_five'))
   db.apply('execution',lambda st:install_execution_policy(st,T))
   before=copy.deepcopy(db.state);db.apply('fresh',lambda st:install_cycle_entry_policy(st,T))
   self.assertEqual(before['positions'],db.state['positions']);self.assertEqual(before['cash'],db.state['cash'])
   inputs=refill_inputs(db.state,'weekly_five',{s:plan() for s in entry_pool(db.state,'weekly_five')},T+1000)
   copy_st=copy.deepcopy(db.state)
   db.apply('allocate',lambda st:apply_refill_allocation(st,inputs,T+1000))
   copy_st['positions']=dict(reversed(list(copy_st['positions'].items())))
   recorded=json.loads(canonical(inputs));apply_refill_allocation(copy_st,recorded,T+1000);self.assertEqual(copy_st,db.state)
   head=db.verify();saved=copy.deepcopy(db.state);db.db.close();db=Store(path,validator=weekly_audit)
   self.assertEqual(db.verify(),head);self.assertEqual(db.state,saved)
   self.assertFalse(db.apply('fresh',lambda st:install_cycle_entry_policy(st,T)));db.db.close()
if __name__=='__main__':unittest.main()
