import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from paper_trials import PaperTrials,snapshot_context,metrics_for,HASH,VARIANTS,MINUTE,FIVE,HOUR,DAY
from paper_capture import UnifiedCapture

S='SOLUSDT';T=10*DAY
def b(t,o=103.,h=104.1,l=102.9,c=104.,step=FIVE):return dict(t=t,o=o,h=h,l=l,c=c,v=10.,available_ms=t+step+5000)
def w():return dict(id='case',side=1,touch_ms=T,expires_ms=T+90*MINUTE,zone=dict(id='z',kind='FVG',side=1,low=100.,high=104.,available_ms=T-10000,formed_ms=T-FIVE),
 gates={},reference=dict(type='H',price=103.8,available_ms=T-10000),reference_unbroken=True,liquidity=dict(type='L',price=101.,available_ms=T-10000),nearer=None,last_close=103.,local_atr=2.,h1_atr=None,
 instrument=dict(tick=.01,step=.001,minimum=.001,min_notional=5.),row_width=.1)
def q(ts):return dict(bid=104.,ask=104.01,bid_size=10000,ask_size=10000,exchange_ms=ts)

class TrialsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.e=PaperTrials(self.root,[S],T)
 def tearDown(self):self.e.db.close();self.tmp.cleanup()
 def warm(self):
  for i in range(80):self.e.m5[S].append(b(T-(80-i)*FIVE,100,102,98,100))
 def signal(self,ts=T+FIVE+5000):
  self.warm();self.e.quote(S,q(T+FIVE-100),T+FIVE-100)
  self.e.quote(S,q(ts),ts);self.e.signal(S,w(),b(T),{'mss':dict(passed=True,available_ms=ts,reference=w()['reference'],bar=b(T))},ts)
  return json.loads(self.e.db.execute('SELECT plan FROM signals').fetchone()[0])
 def test_two_confirmation_mss_without_elliott_or_hourly(self):
  self.e.base_event(dict(kind='research_watch',symbol=S,receive_ms=T,frozen=w(),price_context_5m=[b(T-FIVE)]))
  self.e.base_event(dict(kind='research_bar_5m',symbol=S,receive_ms=T+FIVE+5000,bar=b(T),footprint_levels=None))
  self.assertEqual(self.e.signal_count,1)
 def test_poi_alone_not_enough(self):
  self.e.base_event(dict(kind='research_watch',symbol=S,receive_ms=T,frozen=w(),price_context_5m=[b(T-FIVE)]))
  self.e.base_event(dict(kind='research_bar_5m',symbol=S,receive_ms=T+FIVE+5000,bar=b(T,o=102,h=103,l=101.5,c=102),footprint_levels=None))
  self.assertEqual(self.e.signal_count,0)
 def test_dedup_keeps_first_not_best(self):
  self.signal();x=w();x['id']='duplicate';self.e.signal(S,x,b(T),{'mss':{'passed':True}},T+FIVE+6000)
  self.assertEqual(self.e.signal_count,1);self.assertEqual(self.e.db.execute('SELECT count(*) FROM trades').fetchone()[0],len(VARIANTS))
 def test_context_never_uses_later_data(self):
  rows=[b(T-i*HOUR,100,101,99,100,step=HOUR) for i in reversed(range(60))]
  c=snapshot_context({'BTCUSDT':rows,'coin':rows},[],{'rate':.1,'observed_ms':T+1},dict(q(T),observed_ms=T+1),1,100,99,T)
  self.assertIsNone(c['funding_rate']);self.assertIsNone(c['spread_bps']);self.assertEqual(c['regime'],'unknown')
 def test_preentry_quote_cutoff_excludes_receipt_quote(self):
  self.signal();c=json.loads(self.e.db.execute('SELECT context FROM signals').fetchone()[0])
  self.assertLessEqual(c['quote_observed_ms'],T+FIVE);self.assertGreater(c['signal_received_ms'],c['at_ms'])
 def test_thin_stop_is_only_a_flag(self):
  c=snapshot_context({},[],None,None,1,100,99.5,T)
  self.assertTrue(c['thin_stop']);self.assertEqual(self.e.signal_count,0)
 def test_wider_stop_shrinks_size(self):
  self.signal();a=self.e.active[(1,'baseline')];v=self.e.active[(1,'wide_stop')]
  self.assertGreater(v['distance'],a['distance']);self.assertLessEqual(v['quantity'],a['quantity']);self.assertLessEqual(v['quantity']*v['distance'],2.5+1e-8)
 def test_limit_does_not_use_prefill_high_as_profit(self):
  self.signal();p=copy.deepcopy(self.e.active[(1,'limit')]);p.update(entry=101.,reference_entry=101.,stop=99.,activated_ms=T+2*FIVE,status='pending');self.e._prepare(p)
  z=self.e._limit_path(p,[103,105,100.5,102],T+2*FIVE)
  self.assertFalse(z['tp1']);self.assertFalse(z['closed'])
 def test_limit_timeout_is_not_a_loss(self):
  self.signal();p=self.e.active[(1,'limit')];p['expected_minute']=p['expires_ms']//MINUTE*MINUTE+MINUTE;p['activated_ms']=p['expected_minute']
  t=p['expected_minute'];self.e.record(dict(kind='candle_1m',symbol=S,receive_ms=t+MINUTE,candle=b(t,105,105.1,104.8,105,step=MINUTE)))
  r=self.e.db.execute("SELECT status FROM trades WHERE variant='limit'").fetchone()[0];self.assertEqual(r,'not_filled')
 def test_post_only_marketable_is_not_filled(self):
  self.warm();self.e.quote(S,dict(q(T+FIVE+5000),ask=103,bid=102.99),T+FIVE+5000)
  self.e.signal(S,w(),b(T),{'mss':{'passed':True}},T+FIVE+5000)
  self.assertEqual(self.e.db.execute("SELECT status FROM trades WHERE variant='limit'").fetchone()[0],'not_filled')
 def test_cost_components_reconcile(self):
  self.signal();p=copy.deepcopy(self.e.active[(1,'baseline')]);from paper_paths import path_step
  p['states']=[path_step(p['states'][0],[p['entry'],p['tp2']+1,p['entry'],p['tp2']],T+2*FIVE,p)]
  out=self.e._outcome(p)
  for k in ('lower','upper'):self.assertAlmostEqual(out[k]['gross_R']-out[k]['cost_R'],out[k]['net_R']);self.assertGreater(out[k]['execution_R'],0)
 def test_gap_censors_and_does_not_become_loss(self):
  self.signal();self.e.record(dict(kind='candle_1m',symbol=S,receive_ms=T+8*MINUTE,candle=b(T+7*MINUTE,step=MINUTE)))
  self.assertEqual(self.e.db.execute("SELECT status FROM trades WHERE variant='baseline'").fetchone()[0],'censored')
 def test_filled_in_candles_not_promoted_to_context(self):
  self.signal();old=len(self.e.m5[S]);self.e.record(dict(kind='candle_1m',symbol=S,receive_ms=T+7*MINUTE,candle=b(T+6*MINUTE,step=MINUTE),filled_in=True))
  self.assertEqual(len(self.e.m5[S]),old);self.assertEqual(self.e.db.execute("SELECT status FROM trades WHERE variant='baseline'").fetchone()[0],'censored')
 def test_restart_preserves_old_signals_and_censors_open(self):
  self.signal();self.e.db.close();self.e=PaperTrials(self.root,[S],T+DAY)
  self.assertEqual(self.e.signal_count,1);self.assertEqual(self.e.started,T+FIVE+5000);self.assertFalse(self.e.active)
 def test_live_reports_and_exports_do_not_expose_outcomes(self):
  self.signal();self.assertTrue(self.e.report()['locked'])
  files=self.e.write_exports(self.root);self.assertFalse(any(p.suffix=='.sqlite' for p in files));self.assertTrue(json.loads((self.root/'research-trial-report.json').read_text())['final']['locked'])
 def test_policy_hash_and_no_imported_old_rows(self):
  self.assertEqual(self.e.meta('protocol_hash'),HASH);self.assertEqual(self.e.signal_count,0)
 def test_final_requires_position_maturity(self):
  self.signal();self.e.phase='sealed';self.assertTrue(self.e.report(final=True)['locked'])
 def test_regime_has_unknown_not_fake_choppy(self):
  c=snapshot_context({},[],None,None,1,100,99,T);self.assertEqual(c['regime'],'unknown');self.assertIn('atr_1h',c['missing'])
 def test_colombo_hour_offset(self):
  c=snapshot_context({},[],None,None,1,100,99,T);self.assertEqual(c['colombo_hour'],5.5);self.assertEqual(c['utc_hour'],0.)
 def test_unified_source_has_no_second_entry_engine(self):
  core=UnifiedCapture(self.root,[S],self.e,recover_ms=T)
  try:
   with self.assertRaises(RuntimeError):core._try_entry(None,None,None)
   self.assertEqual(core.path.name,'research_capture.sqlite')
  finally:core.db.close()
 def test_chronological_holdout_and_purge_and_kill(self):
  base={'at_ms':T,'regime':'choppy','hour_block_utc':'00-04','stop_distance_pct':.005}
  outcome={'lower':dict(net_R=-1,gross_R=-.8,cost_R=.2,fees_R=.1,funding_R=.05,execution_R=.05,exit_ms=T+DAY),'upper':dict(net_R=-.5,gross_R=-.3,cost_R=.2,fees_R=.1,funding_R=.05,execution_R=.05,exit_ms=T+DAY)}
  self.e._lock_clock(T)
  for i in range(200):
   at=T+i*4*HOUR;bucket=at//FIVE*FIVE
   cur=self.e.db.execute('INSERT INTO signals(setup,s,side,at,bucket,context,plan) VALUES(?,?,?,?,?,?,?)',(str(i),S,1,at,bucket,json.dumps(base),'{}'))
   for v in VARIANTS:self.e.db.execute('INSERT INTO trades VALUES(?,?,?,?,?)',(cur.lastrowid,v,'closed',json.dumps({'entry':100,'distance':.5}),json.dumps(outcome)))
  self.e.signal_count=200;self.e.phase='complete';self.e.db.commit()
  final=self.e.report(True);dev=self.e.report(False)
  self.assertEqual(final['independent_signals'],82);self.assertEqual(dev['independent_signals'],112)
  self.assertTrue(all(v=='STOP' for v in final['kill'].values()))
  self.assertEqual(final['metrics']['baseline']['all']['all']['lower']['win_rate'],0)
  self.assertTrue(final['metrics']['baseline']['coin'][S]['sample_warning']==False)
 def test_baseline_all_95_saved_outcomes_reconcile(self):
  path=Path(__file__).parent/'reference/All-95-Trade-Analysis-Package/details.json'
  if not path.exists():self.skipTest('Reference package is available locally for pre-deployment parity check')
  count=0
  for x in json.load(open(path)):
   if not x['reconstruction']['all_closed']:continue
   p=dict(x['trade'],reference_entry=x['source']['plan']['trigger_close'],entry_fee=.0006,quantity=1.,states=x['reconstruction']['states'],quality_flags=[])
   out=self.e._outcome(p)
   self.assertAlmostEqual(out['lower']['net_R'],x['reconstruction']['net_R_low'],places=7)
   self.assertAlmostEqual(out['upper']['net_R'],x['reconstruction']['net_R_high'],places=7);count+=1
  self.assertEqual(count,92)
 def test_integrity_audit_detects_tampering(self):
  from paper_audit import audit
  self.signal();self.assertTrue(audit(self.e.path)['audit_ok'])
  self.e.db.execute("UPDATE events SET payload='{}' WHERE id=1");self.e.db.commit()
  try:ok=audit(self.e.path)['audit_ok']
  except KeyError:ok=False
  self.assertFalse(ok)

class ReportTests(unittest.TestCase):
 def test_net_metrics_and_small_sample_warning(self):
  def result(net,ts):return dict(net_R=net,gross_R=net+.2,cost_R=.2,fees_R=.1,funding_R=.05,execution_R=.05,exit_ms=ts)
  items=[dict(signal_id=1,cohort=1,status='closed',result=dict(lower=result(2,1),upper=result(2,1))),dict(signal_id=2,cohort=1,status='closed',result=dict(lower=result(-1,2),upper=result(-1,2)))]
  m=metrics_for(items);self.assertEqual(m['lower']['win_rate'],.5);self.assertEqual(m['lower']['profit_factor'],2);self.assertEqual(m['lower']['expectancy'],.5);self.assertEqual(m['lower']['max_drawdown_R'],1);self.assertTrue(m['sample_warning']);self.assertEqual(m['temporal_cohorts'],1)

if __name__=='__main__':unittest.main()
