"""Synthetic RC3F acceptance scenarios; never production signals/fills or performance evidence."""
import copy,tempfile,unittest
from dataclasses import replace,asdict
from unittest.mock import patch
from phoenix_v2.fast_strategy import FastStrategy,FastStructure,PROFILE
from phoenix_v2.structure import Structure,Pivot
from phoenix_v2.objects import Zone,Pool
from phoenix_v2.model import D
from phoenix_v2.forward import Runtime
from phoenix_v2.journal import Journal
from phoenix_v2 import checkpoint
from test_forward import bar,trade,rule
from test_feed import proof

def configured(symbol='BTCUSDT',side=1):
 logs=[];s=FastStrategy(symbol,lambda at,event,**kw:logs.append(dict(at=at,event=event,**kw)))
 for f in s.frames.values():f.atr.count=300;f.atr.value=D(2);f.atrs=[D(2)]
 def price(p):return D(p) if side==1 else D(200)-D(p)
 s.frames[60].regime=side;s.frames[60].leg=(Pivot('range-anchor',-side,price(90),0,0),Pivot('range-end',side,price(150),0,0))
 s.frames[5].bars=[replace(bar(0,tf=5,o='106',h='108',l='103',c='106'),symbol=symbol)] if side==1 else []
 s.frames[5].raw=[Pivot('known-mss',side,price(108),0,300)]
 low,high=sorted((price(100),price(104)));s.zones=[Zone('known-poi',15,side,'OB',low,high,0,300,300)]
 s.liquidity.pools=[Pool('swept',15,-side,[Pivot('low',-side,price(101),0,300)],300),Pool('target',15,side,[Pivot('high',side,price(145),0,300)],300)]
 return s,logs,price

class FastStrategyTests(unittest.TestCase):
 def test_two_right_confirmation_is_causal_and_earlier_than_rc2(self):
  fast=FastStructure('BTCUSDT',5);old=Structure('BTCUSDT',5)
  for i,h in enumerate((101,102,110,103,102)):
   b=bar(i*300,tf=5,o='100',h=str(h),l='99',c='100')
   a,_=fast.close(b);old.close(b)
   if i<4:self.assertFalse(fast.raw)
  self.assertEqual(fast.raw[0].origin,600);self.assertEqual(fast.raw[0].available,1500);self.assertFalse(old.raw)
 def test_hourly_known_swing_break_enables_direction_without_three_steps(self):
  f=FastStructure('BTCUSDT',60);f.raw=[Pivot('hi',1,D(110),0,3600),Pivot('lo',-1,D(90),0,3600)]
  f.close(bar(3600,tf=60,o='105',h='121',l='104',c='120'))
  self.assertEqual(f.regime,1);self.assertEqual(f.leg[0].id,'lo');self.assertEqual(f.leg[1].price,D(121))
 def test_daily_veto_preserved_and_four_hour_disagreement_is_context(self):
  s,_,_=configured();s.frames[240].regime=-1;self.assertEqual(s.bias(),1)
  s.frames[1440].regime=-1;self.assertEqual(s.bias(),0)
 def test_same_reclaim_candle_can_generate_both_long_and_short_signal(self):
  for side in (1,-1):
   s,logs,p=configured(side=side)
   for at,v in ((540.1,'100.5'),(550,'102'),(590,'112')):s.price(p(v),at)
   vals=dict(o=p(106),h=max(p('113'),p('100.5')),l=min(p('113'),p('100.5')),c=p(112))
   b=replace(bar(300,tf=5),**vals);result=s.five_close(b)
   self.assertEqual(len(result),1);sig=result[0]
   self.assertEqual(sig.evidence['mode'],'FAST_RECLAIM');self.assertEqual(sig.side,side)
   self.assertLess(sig.evidence['anchor_available'],sig.sweep_at);self.assertLess(sig.evidence['sweep_at'],sig.evidence['reclaim_tick'])
   self.assertLessEqual(sig.evidence['reclaim_tick'],sig.evidence['break_tick']);self.assertEqual(sig.at,600)
 def test_close_alone_cannot_invent_an_unobserved_intrabar_break(self):
  s,logs,p=configured();s.price(p('100.5'),540);s.price(p(102),550)
  result=s.five_close(bar(300,tf=5,o='106',h='113',l='100.5',c='112'))
  self.assertFalse(result);self.assertTrue(s.setups)
 def test_already_beyond_reclaim_can_use_later_displacement_without_recross(self):
  s,_,p=configured();s.price(p('100.5'),540);s.price(p(109),590)
  self.assertFalse(s.five_close(bar(300,tf=5,o='106',h='120',l='100.5',c='109')))
  self.assertTrue(s.setups)
  result=s.five_close(bar(600,tf=5,o='109',h='113.5',l='109',c='113'))
  self.assertEqual(len(result),1);self.assertEqual(result[0].evidence['mode'],'DELAYED_MSS')
 def test_future_anchor_cannot_qualify_sweep(self):
  s,logs,p=configured();s.frames[5].raw[0].available=600;s.price(p('100.5'),540)
  self.assertFalse(s.sweeps);self.assertIn('no_preexisting_5m_break_reference',logs[-1]['reasons'])
 def test_midpoint_invalid_fvg_cannot_be_rescued_by_strong_close(self):
  s,logs,p=configured();s.zones[0].kind='FVG';s.price(p('100.5'),540);s.price(p(112),590)
  self.assertFalse(s.sweeps);self.assertFalse(s.zones[0].valid)
 def test_new_adverse_extreme_resets_tick_confirmation(self):
  s,_,p=configured();s.price(p('100.5'),540);s.price(p(112),550);s.price(p('100.25'),560)
  self.assertIsNone(s.sweeps[1].break_tick)
 def test_fast_sweep_dynamic_evidence_survives_checkpoint(self):
  s,_,p=configured();s.price(p('100.5'),540);s.price(p(112),550)
  restored=checkpoint.loads(checkpoint.dumps(s));self.assertIsInstance(restored,FastStrategy)
  self.assertEqual(restored.sweeps[1].break_tick,550);self.assertEqual(restored.sweeps[1].anchor.id,'known-mss')

class FastRuntimeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.j=Journal(self.tmp.name);self.r=Runtime(self.j,strategy_profile=PROFILE)
  self.r.state['contracts']={'BTCUSDT':[rule()]};self.r.bind();s,_,_=configured();s.log=self.r.log
  self.r.state['strategies']={'BTCUSDT':s};self.r.state['previous']=540
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def signal_minute(self):
  rows=[dict(trade(at=t,p=p,v='100',i=str(t)),received_at=t/1000+.1) for t,p in ((540100,'100.5'),(550000,'102'),(590000,'112'))]
  one=bar(540,o='100.5',h='112',l='100.5',c='112',v='300');five=bar(300,tf=5,o='106',h='113',l='100.5',c='112')
  with patch('phoenix_v2.forward.time.time',return_value=610):self.r.minute(600,{'BTCUSDT':[one,five]},{'BTCUSDT':one},{'BTCUSDT':(rows,None)},{})
 def test_signal_to_receipt_aware_fill_and_restart_without_native_open_fiction(self):
  self.signal_minute();self.assertEqual(self.j.counts['signal'],1);self.assertEqual(self.j.counts.get('entry_fill',0),0)
  self.assertEqual(self.r.state['arms']['A'].orders['BTCUSDT'].submitted,610)
  one=bar(600,o='112',h='113',l='111',c='113',v='200')
  rows=[dict(trade(at=605000,p='111',v='100',i='pre'),received_at=605.1),dict(trade(at=611000,p='113',v='100',i='post'),received_at=611.1)]
  self.r.minute(660,{'BTCUSDT':[one]},{'BTCUSDT':one},{'BTCUSDT':(rows,None,proof(one))},{})
  self.assertEqual(self.j.counts['entry_fill'],1);p=self.r.state['arms']['A'].positions['BTCUSDT'];self.assertEqual(p.opened,611)
  self.assertGreater(p.entry,D(113));self.assertFalse(self.r.state['arms']['B'].positions)
  head=self.j.head;self.j.close();self.j=Journal(self.tmp.name);self.r=Runtime(self.j,strategy_profile=PROFILE)
  self.assertEqual(self.j.head,head);self.assertEqual(self.r.state['arms']['A'].positions['BTCUSDT'].opened,611)
 def test_stale_first_reference_is_no_fill_not_later_price_selection(self):
  self.signal_minute();one=bar(600,o='112',h='113',l='111',c='113',v='200')
  rows=[dict(trade(at=617000,p='111',v='100',i='late'),received_at=617.1),dict(trade(at=618000,p='113',v='100',i='later'),received_at=618.1)]
  self.r.minute(660,{'BTCUSDT':[one]},{'BTCUSDT':one},{'BTCUSDT':(rows,None,proof(one))},{})
  self.assertFalse(self.r.state['arms']['A'].positions);self.assertEqual(self.j.counts.get('entry_fill',0),0)
 def test_migration_retains_old_cash_and_immutable_checkpoint(self):
  self.assertEqual(self.r.state['archived_experiments'][0]['books']['A']['cash'],'1000')
  self.assertEqual(self.j.evidence_counts.get('strategy_cutover_checkpoint',0),0) # Fresh local journal, no old checkpoint.
 def test_legacy_flat_journal_migration_archives_original_state_without_deleting_evidence(self):
  with tempfile.TemporaryDirectory() as d:
   j=Journal(d);r=Runtime(j);r.state['arms']['A'].cash=D('990');r.commit();original=j.load();head=j.head
   r=Runtime(j,strategy_profile=PROFILE)
   self.assertEqual(r.state['archived_experiments'][0]['books']['A']['cash'],'990')
   self.assertEqual(r.state['arms']['A'].cash,D(1000));self.assertEqual(j.evidence_counts['strategy_cutover_checkpoint'],1)
   self.assertEqual(r.state['archived_experiments'][0]['journal_head'],head)
   import json,zlib
   archive=json.loads(zlib.decompress(j.db.execute("SELECT payload FROM evidence WHERE kind='strategy_cutover_checkpoint'").fetchone()[0]))
   self.assertEqual(archive['payload'],original)
   j.close()

 def test_synthetic_pipeline_stop_exit_is_journaled_with_fees_and_independent_books(self):
  self.test_signal_to_receipt_aware_fill_and_restart_without_native_open_fiction()
  one=bar(660,o='113',h='113',l='99',c='99',v='100')
  rows=[dict(trade(at=670000,p='99',v='100',i='stop'),received_at=670.1)]
  self.r.minute(720,{'BTCUSDT':[one]},{'BTCUSDT':one},{'BTCUSDT':(rows,None,proof(one))},{})
  self.assertFalse(self.r.state['arms']['A'].positions);self.assertEqual(self.j.counts['position_closed'],1)
  self.assertEqual(self.r.state['arms']['B'].cash,D(1000));self.assertLess(self.r.state['arms']['A'].cash,D(1000))
  history=self.j.page(table='paper_history')['items'][0]
  self.assertEqual(D(history['net_pnl']),self.r.state['arms']['A'].cash-D(1000));self.assertGreater(D(history['fees']),0)

class ContextRecoveryTests(unittest.TestCase):
 def old(self,low='102'):
  from phoenix_v2.strategy import Strategy
  s=Strategy('BTCUSDT',None);f=s.frames[15]
  for b in (bar(0,tf=15,o='105',h='106',l='105',c='105'),bar(900,tf=15,o='105',h='106',l=low,c='105')):f.close(b)
  return s
 def zone_factory(self,f):
  return [Zone('recovered',15,1,'OB',D(100),D(104),0,900,900)] if f.tf==15 and f.bars[-1].end==900 else []
 def test_recovery_preserves_a_provable_visit_instead_of_granting_freshness(self):
  from phoenix_v2.fast_strategy import rebuild_context
  with patch('phoenix_v2.fast_strategy.new_zones',side_effect=self.zone_factory):new=rebuild_context(self.old(),lambda *a,**k:None,as_of=1800)
  self.assertEqual(len(new.zones),1);self.assertEqual(new.zones[0].visits,1);self.assertTrue(new.zones[0].midpoint_touched)
 def test_any_invalidating_extremum_removes_the_zone_regardless_of_path(self):
  from phoenix_v2.fast_strategy import rebuild_context
  with patch('phoenix_v2.fast_strategy.new_zones',side_effect=self.zone_factory):new=rebuild_context(self.old('99'),lambda *a,**k:None,as_of=1800)
  self.assertFalse(new.zones)
 def test_unknown_forming_tail_is_censored_and_known_tail_can_invalidate(self):
  from phoenix_v2.fast_strategy import rebuild_context
  with patch('phoenix_v2.fast_strategy.new_zones',side_effect=self.zone_factory):
   missing=rebuild_context(self.old(),lambda *a,**k:None,as_of=1860)
   known=rebuild_context(self.old(),lambda *a,**k:None,recent=[bar(1800,o='105',h='106',l='99',c='105')],as_of=1860)
  self.assertFalse(missing.zones);self.assertFalse(known.zones)
 def test_cutover_with_unresolved_exposure_cannot_reseed(self):
  with tempfile.TemporaryDirectory() as d:
   j=Journal(d);r=Runtime(j);r.state['unresolved']=['evidence-needed'];r.commit();seq=j.sequence
   with self.assertRaisesRegex(ValueError,'resolved flat'):Runtime(j,strategy_profile=PROFILE)
   self.assertEqual(j.sequence,seq);j.close()

 def test_old_consumed_three_right_pivot_cannot_reappear_as_fresh_two_right_pool(self):
  from phoenix_v2.strategy import Strategy
  from phoenix_v2.fast_strategy import rebuild_context
  s=Strategy('BTCUSDT',None);f=s.frames[15]
  for i,h in enumerate((101,102,110,103,102)):f.close(bar(i*900,tf=15,o='100',h=str(h),l='99',c='100'))
  old=Pivot('BTCUSDT:15:1800:1',1,D(110),1800,4500);s.liquidity.members[old.id]=old;s.liquidity.consumed.add(old.id)
  recovered=rebuild_context(s,lambda *a,**k:None,as_of=4500)
  self.assertFalse([p for p in recovered.liquidity.pools if p.kind==1 and p.outer==D(110)])

 def test_structural_cache_bounds_do_not_reset_the_wilder_state(self):
  f=FastStructure('BTCUSDT',5)
  for i in range(450):f.close(bar(i*300,tf=5,o='100',h='102',l='99',c='101'))
  self.assertEqual(len(f.bars),300);self.assertEqual(len(f.atrs),300);self.assertEqual(f.atr.count,450)
  self.assertEqual(f.bars[0].start,150*300)

class FastOrderingTests(unittest.TestCase):
 def runtime(self):
  from types import SimpleNamespace
  from phoenix_v2.portfolio import Arm
  from phoenix_v2.forward import PaperPolicy
  policy=PaperPolicy({'BTCUSDT':[rule()]});policy.data['a_activation_window_seconds']=60
  s,_,_=configured();s.zones=[];s.liquidity.pools=[]
  arms={n:Arm(n,policy,lambda *a,**k:None) for n in ('A','B')}
  a=arms['A'];a.marks['BTCUSDT']=D(112);a.mark_times['BTCUSDT']=610
  from phoenix_v2.strategy import Signal
  sig=Signal('synthetic','BTCUSDT',1,600,540,D(100),D(150),D(2),D(2),D('100.5'),None,D(112),{})
  a.reserve(sig,610,D(112))
  return SimpleNamespace(state=dict(strategies={'BTCUSDT':s},arms=arms)),sig
 def rows(self,at,*prices):return [trade(at=at,p=str(p),v='100',i=str(i)) for i,p in enumerate(prices)]
 def test_harmless_ties_before_activation_do_not_create_an_entry(self):
  from phoenix_v2.fast_execution import ambiguous,material_times
  r,_=self.runtime();rows=self.rows(605000,111,112)
  self.assertFalse(ambiguous(r,'BTCUSDT',rows));self.assertFalse(material_times(r,'BTCUSDT',rows))
 def test_first_post_decision_tied_prices_are_quarantined(self):
  from phoenix_v2.fast_execution import ambiguous
  r,_=self.runtime();self.assertTrue(ambiguous(r,'BTCUSDT',self.rows(611000,112,113)))
 def position(self):
  r,sig=self.runtime();r.state['arms']['A'].attempt_a(sig,611,D(112),dict(qty='0.01',evidence='synthetic'))
  self.assertIn('BTCUSDT',r.state['arms']['A'].positions);return r
 def test_harmless_position_ticks_have_no_shared_account_effect(self):
  from phoenix_v2.fast_execution import ambiguous,material_times
  r=self.position();rows=self.rows(612000,113,114)
  self.assertFalse(ambiguous(r,'BTCUSDT',rows));self.assertFalse(material_times(r,'BTCUSDT',rows))
 def test_stop_target_or_triggered_partial_exit_remains_ambiguous(self):
  from phoenix_v2.fast_execution import ambiguous,material_times
  r=self.position()
  for prices in ((99,113),(150,151)):
   rows=self.rows(612000,*prices);self.assertTrue(ambiguous(r,'BTCUSDT',rows));self.assertEqual(material_times(r,'BTCUSDT',rows),{612000})
  r.state['arms']['A'].positions['BTCUSDT'].triggered=True
  self.assertTrue(ambiguous(r,'BTCUSDT',self.rows(613000,113,114)))
