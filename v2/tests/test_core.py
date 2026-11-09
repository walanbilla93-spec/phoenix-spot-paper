import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from phoenix_v2.model import D, Bar, ATR, Aggregator, displacement, slip, canonical, digest
from phoenix_v2.structure import Structure, Pivot
from phoenix_v2.objects import Zone, Liquidity
from phoenix_v2.strategy import Signal, Strategy, Sweep, Setup
from phoenix_v2.manifest import Manifest
from phoenix_v2.portfolio import Arm, risk_day
from phoenix_v2.engine import Engine
from phoenix_v2.store import Store
from phoenix_v2.research import indices, interval, Holdout, acceptance, add_months
from phoenix_v2.scenarios import Scenarios
from phoenix_v2.service import safety, status, Handler

def fixture(symbols=('TESTUSDT',)):
    # Deliberately invented numbers for code assertions; never venue evidence.
    rule=dict(start=0,end=9999999999,tick='.01',step='.001',minimum_qty='.001',minimum_notional='1',multiplier='1',initial_margin='.3333333334',taker='.001',maker='.0005',free_collateral='1000')
    return Manifest(dict(capital='1000',symbols=list(symbols),contracts={s:[dict(rule)] for s in symbols},mark_max_age_seconds=120),fixture=True)

def bar(i,tf=5,o='100',h='101',l='99',c='100',v='10'):
    return Bar('TESTUSDT',tf,i*tf*60,*map(D,(o,h,l,c,v)))

def signal(symbol='TESTUSDT',side=1,at=300,b=True):
    z=Zone('exec',5,side,'FVG',D('97'),D('99'),0,at,at)
    return Signal('setup:'+symbol,symbol,side,at,0,D('95') if side==1 else D('105'),D('120') if side==1 else D('80'),D('1'),D('2'),D('96') if side==1 else D('104'),z if b else None,D('100'),{})

def arm(name='A',symbols=('TESTUSDT',)):
    logs=[]; a=Arm(name,fixture(symbols),lambda t,e,**kw:logs.append(dict(at=t,event=e,**kw)))
    for s in symbols: a.marks[s]=D('100'); a.mark_times[s]=300
    a.reset(300); return a,logs

def batch(end, symbols=('TESTUSDT',)):
    return dict(end=end,chronology_evidence='SYNTHETIC_ONLY',bars={s:dict(o='100',h='101',l='99',c='100',v='1') for s in symbols},points=[],opening_marks={s:dict(at=end-60,price='100') for s in symbols},closing_marks={s:dict(at=end,price='100') for s in symbols})

class CandleTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError): bar(0).validate(299)
        with self.assertRaises(ValueError): bar(0,h='98').validate(300)
        with self.assertRaises(ValueError): bar(0,c='NaN').validate(300)
    def test_aggregation_calendar_and_gap(self):
        a=Aggregator(); result=[]
        for i in range(15): result+=a.push(bar(i,tf=1), (i+1)*60)
        self.assertEqual([b.tf for b in result].count(5),3)
        self.assertEqual([b.tf for b in result].count(15),1)
        with self.assertRaises(ValueError): a.push(bar(16,tf=1),1020)
    def test_partial_first_bucket_not_filled(self):
        a=Aggregator(); out=[]
        for i in range(1,5): out+=a.push(bar(i,tf=1),(i+1)*60)
        self.assertNotIn(5,[b.tf for b in out])
    def test_atr_warmup(self):
        a=ATR()
        for i in range(249): a.update(bar(i))
        self.assertFalse(a.ready); a.update(bar(249)); self.assertTrue(a.ready); self.assertEqual(a.value,D('2'))
    def test_displacement_inclusive_threshold(self):
        self.assertTrue(displacement(bar(0,o='100',c='101.5',h='102',l='100'),D('1'),1))
        self.assertFalse(displacement(bar(0,o='100',c='101.49',h='102',l='100'),D('1'),1))
    def test_pivot_third_right_only_and_tie(self):
        s=Structure('TESTUSDT',5)
        for i,h in enumerate(('101','102','103','110','104','103')): s.close(bar(i,h=h))
        self.assertFalse(s.raw); s.close(bar(6,h='102'))
        self.assertEqual(s.raw[0].origin,900); self.assertEqual(s.raw[0].available,2100)
        t=Structure('TESTUSDT',5)
        for i,h in enumerate(('101','102','110','110','104','103','102')): t.close(bar(i,h=h))
        self.assertFalse(t.raw)
    def test_simultaneous_high_low_excluded_from_chain(self):
        s=Structure('TESTUSDT',5)
        for i in range(7): s.close(bar(i,h='110' if i==3 else '101',l='90' if i==3 else '99'))
        self.assertEqual(len(s.raw),2); self.assertFalse(s.chain); self.assertEqual(len(s.exclusions),2)
    def test_terminal_provisional_not_sixth_frozen(self):
        s=Structure('TESTUSDT',5)
        for i,(k,p) in enumerate(((1,110),(-1,90),(1,115),(-1,95),(1,120),(-1,100))):
            s.chain.append(Pivot(str(i),k,D(p),i*300,i*300+1200,9999 if i<5 else None))
        self.assertEqual(s.sequence()[0],0)
        s.chain[-1].frozen=9999
        self.assertEqual(s.sequence()[0],1)
    def test_old_anchor_invalidates_before_replacement(self):
        s=Structure('TESTUSDT',240); a=Pivot('a',-1,D('100'),0,100,100); b=Pivot('b',1,D('120'),100,200)
        s.regime=1;s.leg=(a,b)
        _,neutral=s.close(bar(0,tf=240,o='100',h='101',l='98',c='99'))
        self.assertTrue(neutral); self.assertEqual(s.regime,0)

class ObjectTests(unittest.TestCase):
    def prepared(self):
        logs=[];s=Strategy('TESTUSDT',lambda t,e,**kw:logs.append((t,e,kw)))
        s.warm=lambda:True
        h=s.frames[240];h.regime=1;h.leg=(Pivot('range-low',-1,D(90),0,1,1),Pivot('range-high',1,D(130),1,2))
        f=s.frames[5]
        for i,(kind,price) in enumerate(((1,112),(-1,108),(1,110),(-1,105),(1,103),(-1,98),(1,100))):
            f.chain.append(Pivot(str(i),kind,D(price),i*300,i*300+300,2500 if i<6 else None))
        f.atr.value=D(1)
        for tf in (15,60):s.frames[tf].atr.value=D(1)
        z=Zone('ctx',15,1,'OB',D(94),D(100),0,900,900);s.zones.append(z)
        s.liquidity.install(Pivot('sweep-low',-1,D(96),0,900),15,D(1))
        s.liquidity.install(Pivot('target-high',1,D(125),0,900),15,D(1))
        return s,logs
    def test_reclaim_then_later_mss_not_same_candle(self):
        s,logs=self.prepared();s.price(D('95.5'),2800)
        self.assertIn(1,s.sweeps)
        reclaim=bar(9,o='97',h='99',l='95.5',c='97');s.frames[5].atrs.append(D(1))
        self.assertFalse(s.five_close(reclaim));self.assertIn(1,s.setups)
        mss=bar(10,o='100',h='104.2',l='99.5',c='104');s.frames[5].atrs.append(D(1))
        result=s.five_close(mss);self.assertEqual(len(result),1);self.assertNotIn(1,s.setups)
        self.assertEqual(result[0].stop,D('95.40'));self.assertEqual(result[0].target,D('124.90'))
    def test_later_pool_cannot_join_original_sweep(self):
        s,logs=self.prepared();s.price(D('95.5'),2800);e=s.sweeps[1]
        s.liquidity.install(Pivot('late',-1,D('95.4'),2700,2850),15,D(1))
        s.price(D('95.3'),2900)
        self.assertTrue(any(p.id=='late' and p.consumed for p in s.liquidity.pools))
        self.assertFalse(any(p.id=='late' for p in e.pools));self.assertEqual(e.start,2800)
    def test_reclaim_already_beyond_rejected(self):
        s,logs=self.prepared();s.price(D('95.5'),2800)
        b=bar(9,o='97',h='105',l='95.5',c='104');s.frames[5].atrs.append(D(1));s.five_close(b)
        self.assertFalse(s.setups);self.assertTrue(any(kw.get('reason')=='reclaim_already_beyond' for _,_,kw in logs))
    def test_intrabar_extreme_invalidation_cannot_be_cured_by_mss_close(self):
        s,_=self.prepared();s.price(D('95.5'),2800);s.frames[5].atrs.append(D(1));s.five_close(bar(9,o='97',h='99',l='95.5',c='97'))
        s.price(D('95.49'),3100);self.assertFalse(s.setups)
        s.frames[5].atrs.append(D(1));self.assertFalse(s.five_close(bar(10,o='100',h='104.2',l='95.49',c='104')))
    def test_visit_strict_near_boundary_and_two_outside(self):
        z=Zone('z',15,1,'OB',D(90),D(100),0,900,900)
        z.update(D(100));self.assertEqual(z.visits,0)
        z.update(D(99)); z.update(D(98));self.assertEqual(z.visits,1)
        z.own_close(bar(1,15,o='102',h='104',l='101',c='102'))
        z.update(D(99));self.assertEqual(z.visits,1)
        for i in (2,3):z.own_close(bar(i,15,o='102',h='104',l='101',c='102'))
        z.update(D(99));self.assertEqual(z.visits,2)
    def test_fvg_midpoint_invalid_ob_exact_far_survives(self):
        z=Zone('f',15,1,'FVG',D(90),D(100),0,900,900);z.update(D(95));self.assertFalse(z.valid)
        z=Zone('o',15,1,'OB',D(90),D(100),0,900,900);z.update(D(90));self.assertTrue(z.valid);z.update(D('89.99'));self.assertFalse(z.valid)
    def test_cluster_total_width_frozen_tolerance_member_consumption(self):
        liq=Liquidity()
        for i,p in enumerate(('100','100.08')):liq.install(Pivot(str(i),1,D(p),i*100,i*100+1),15,D(1))
        cluster=next(p for p in liq.pools if p.cluster)
        liq.price(D('100.04'),200)
        self.assertTrue(cluster.member_consumed);self.assertFalse(cluster.consumed)
        self.assertNotIn(cluster,liq.targets(1,D(99)))
        liq.install(Pivot('2',1,D('100.16'),201,202),15,D(1))
        self.assertEqual(len(cluster.members),2)
        liq.price(D('100.09'),300);self.assertTrue(cluster.consumed)
    def test_reservation_at_penetration_does_not_resurrect(self):
        logs=[];s=Strategy('TESTUSDT',lambda t,e,**kw:logs.append(e));z=Zone('ctx',15,1,'OB',D(90),D(100),0,0,0)
        s.setups[1]=Setup('old','TESTUSDT',1,0,300,D(90),D(103),z,D(80),D(120),{})
        s.liquidity.install(Pivot('pool',-1,D(95),0,1),15,D(1));s.price(D(94),400)
        self.assertFalse(s.sweeps);self.assertTrue(s.liquidity.pools[0].consumed)
        s.setups.clear();s.price(D(94),500);self.assertFalse(s.sweeps)

class PortfolioTests(unittest.TestCase):
    def test_independent_arms(self):
        a,_=arm();b,_=arm('B');a.cash-=D(20);self.assertEqual(b.cash,D(1000));self.assertIsNot(a.orders,b.orders)
    def test_cost_sizing_and_gross_geometry(self):
        a,logs=arm();o=a.reserve(signal(),300,D(100));self.assertIsNotNone(o)
        self.assertLessEqual(o.qty*o.unit_risk,D(5));self.assertGreater(o.unit_risk,o.entry-o.stop)
        self.assertEqual(o.qty%o.rules['step'],0)
    def test_b_risk_recheck_cancel_not_resize(self):
        a,_=arm('B');o=a.reserve(signal(),300,D(100));q=o.qty
        a.cash=D(990);a.checkpoint(300,{'TESTUSDT':1},{'TESTUSDT':D(1)})
        self.assertFalse(a.orders);self.assertEqual(o.qty,q)
    def test_a_improved_open_cannot_increase_reserved_qty_or_risk(self):
        a,_=arm();s=signal();o=a.reserve(s,300,D(100));q=o.qty;r=o.reserved_risk
        a.attempt_a(s,300,D(99),dict(qty=str(q),evidence='SYNTHETIC'))
        p=a.positions['TESTUSDT'];self.assertLessEqual(p.qty,q);self.assertLessEqual(p.planned_risk,r)
    def test_limit_touch_no_fill_and_quantity_required(self):
        a,_=arm('B');o=a.reserve(signal(),300,D(100));a.price('TESTUSDT',301,o.entry,D(1),entry_quantity=o.qty)
        self.assertFalse(a.positions)
        a.price('TESTUSDT',302,o.entry-D('.01'),D(1))
        self.assertFalse(a.positions)
        a.price('TESTUSDT',303,o.entry-D('.01'),D(1),entry_quantity=o.qty)
        self.assertIn('TESTUSDT',a.positions)
    def test_gap_stop_uses_gap_price_and_one_slippage(self):
        a,_=arm();o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC')
        q=o.qty;a.price('TESTUSDT',301,D(94),D(1),exit_quantity=q)
        self.assertFalse(a.positions)
        self.assertEqual(a.completed[0]['net'],(D('93.95')-o.entry)*q-q*o.entry*D('.001')-q*D('93.95')*D('.001'))
        self.assertGreater(-a.completed[0]['net'],D(5))
    def test_partial_exit_retains_protection_and_original_risk(self):
        a,_=arm();o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC');p=a.positions['TESTUSDT'];risk=p.planned_risk
        a.exit_fill('TESTUSDT',301,p.qty/2,p.target,'tp');self.assertEqual(p.planned_risk,risk);self.assertEqual(len(a.completed),0)
        self.assertEqual(p.stop,o.stop);a.exit_fill('TESTUSDT',302,p.qty,p.target,'tp');self.assertEqual(len(a.completed),1)
        with self.assertRaises(KeyError):a.exit_fill('TESTUSDT',303,D(1),D(120),'tp')
    def test_be_prospective_and_no_backward_stop(self):
        a,_=arm();o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC');p=a.positions['TESTUSDT']
        a.price('TESTUSDT',301,p.entry+D('1.5')*p.r,D(1));self.assertEqual(p.stop,o.stop)
        a.price('TESTUSDT',302,D(110),D(1));self.assertEqual(p.stop,D('100.05'))
    def test_funding_is_signed_and_not_zero_default(self):
        a,_=arm();o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC');before=a.cash
        a.funding('TESTUSDT',301,D('.001'),D(100),'SYNTHETIC');self.assertEqual(a.cash,before-o.qty*D('.1'))
        with self.assertRaises(ValueError):a.funding('TESTUSDT',302,D(0),D(100),'')
    def test_daily_lock_latches_then_resets_colombo_midnight(self):
        a,_=arm();a.cash=D(979);a.checkpoint(300,{},{});self.assertTrue(a.locked)
        a.cash=D(1000);a.checkpoint(301,{},{});self.assertTrue(a.locked)
        a.reset(66600);self.assertFalse(a.locked);self.assertEqual(risk_day(66599),'1970-01-01');self.assertEqual(risk_day(66600),'1970-01-02')
    def test_capacity_ranking_is_sequential_and_one_coin(self):
        symbols=tuple(f'S{i}' for i in range(4));a,_=arm(symbols=symbols)
        for s in symbols:a.reserve(signal(s),300,D(100))
        self.assertEqual(len(a.orders),3)
        self.assertIsNone(a.reserve(signal(symbols[0],side=-1),300,D(100)))
    def test_stop_partial_remains_triggered(self):
        a,_=arm();o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC')
        a.price('TESTUSDT',301,D(94),D(1),exit_quantity=o.qty/2);p=a.positions['TESTUSDT'];self.assertTrue(p.triggered)
        a.price('TESTUSDT',302,D(100),D(1),exit_quantity=p.qty);self.assertFalse(a.positions)
    def test_short_adverse_entry_stop_and_be_rounding(self):
        a,_=arm();s=signal(side=-1,b=False);o=a.reserve(s,300,D(100));self.assertEqual(o.entry,D('99.9500'))
        a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC');p=a.positions['TESTUSDT']
        a.price('TESTUSDT',301,p.entry-D('1.5')*p.r,D(1));a.price('TESTUSDT',302,D(90),D(1));self.assertEqual(p.stop,D('99.95'))
    def test_midnight_exit_loss_latches_new_day(self):
        a,_=arm();a.mark_times['TESTUSDT']=66540;s=signal(at=66540);o=a.reserve(s,66540,D(100));a.fill_entry('TESTUSDT',66540,o.qty,o.entry,D(1),'SYNTHETIC')
        p=a.positions['TESTUSDT'];a.marks['TESTUSDT']=p.entry;a.mark_times['TESTUSDT']=66600;a.reset(66600)
        baseline=a.day_start;a.exit_fill('TESTUSDT',66600,p.qty,p.entry-D(30),'stop');a.checkpoint(66600,{},{});self.assertTrue(a.locked)
        a.reset(66600);self.assertTrue(a.locked);self.assertEqual(a.day_start,baseline)
    def test_simultaneous_losses_latch_before_profit_reset(self):
        symbols=('X','Y');a,_=arm(symbols=symbols);a.streak=4
        for s in symbols:
            o=a.reserve(signal(s),300,D(100));a.fill_entry(s,300,o.qty,o.entry,D(1),'SYNTHETIC')
        a.exit_group([dict(arm='A',symbol='X',qty=str(a.positions['X'].qty),price='120',reason='tp'),dict(arm='A',symbol='Y',qty=str(a.positions['Y'].qty),price='95',reason='stop')],301,ordered=False)
        self.assertTrue(a.locked);self.assertEqual(a.streak,1)
    def test_stale_pending_mark_cancels(self):
        a,_=arm('B');a.reserve(signal(),300,D(100));a.checkpoint(421,{'TESTUSDT':1},{'TESTUSDT':D(1)})
        self.assertFalse(a.orders)

class IntegrationTests(unittest.TestCase):
    def test_event_prefix_future_append_and_restart(self):
        rows=[batch((i+1)*60) for i in range(40)]
        e=Engine(fixture());prefix=[]
        for b in rows:e.batch(b);prefix.append(canonical(e.events))
        for i in (0,4,14,39):
            short=Engine(fixture())
            for b in rows[:i+1]:short.batch(b)
            self.assertEqual(canonical(short.events),prefix[i])
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'paper.db';s=Store(path,Engine(fixture()))
            for b in rows:s.apply(b)
            self.assertFalse(s.apply(rows[-1]));head=s.head;s.close()
            restored=Store(path,Engine(fixture()));self.assertEqual(restored.head,head);self.assertEqual(canonical(restored.engine.events),canonical(e.events));restored.close()
    def test_conflicting_duplicate_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(Path(td)/'a.db',Engine(fixture()));s.apply(batch(60));before=canonical(s.engine.state())
            bad=batch(120);bad['bars']['TESTUSDT']['h']='98'
            with self.assertRaises(ValueError):s.apply(bad)
            self.assertEqual(canonical(s.engine.state()),before)
            conflict=batch(60);conflict['bars']['TESTUSDT']['v']='2'
            with self.assertRaises(ValueError):s.apply(conflict)
            s.close()
    def test_corrupt_journal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'a.db';s=Store(p,Engine(fixture()));s.apply(batch(60));s.close()
            db=sqlite3.connect(p);db.execute("UPDATE batches SET state_hash='bad'");db.commit();db.close()
            with self.assertRaises(ValueError):Store(p,Engine(fixture()))
    def test_missing_evidence_never_operational(self):
        self.assertEqual(len(Manifest().missing()),8);self.assertFalse(status()['forward_paper_operational'])
        a=Arm('A',Manifest(),lambda *args,**kw:None);self.assertIsNone(a.reserve(signal(),300,D(100)))
    def test_unknown_path_quarantines_without_fills(self):
        e=Engine(fixture());b=batch(60);del b['chronology_evidence'];e.batch(b)
        self.assertIn('CHRONOLOGY_UNVERIFIED',e.incomplete);self.assertFalse(e.arms['A'].data_valid)
    def test_safety_refuses_keys_and_live_mode(self):
        safety({'PAPER_ONLY':'true'})
        with self.assertRaises(RuntimeError):safety({'PAPER_ONLY':'false'})
        with self.assertRaises(RuntimeError):safety({'BYBIT_API_KEY':'example'})
    def test_bootstrap_is_exact_deterministic_grid_recipe(self):
        x=list(indices(31,2));self.assertEqual(x,list(indices(31,2)));self.assertTrue(all(len(r)==31 and min(r)>=0 and max(r)<31 for r in x))
        self.assertIsNone(interval([(D(0),0)]*14))
    def test_scenario_bounds_are_paired_not_difference_of_minima(self):
        a=Engine(fixture());a.previous=60;b=copy.deepcopy(a)
        a.arms['A'].cash=D(1005);a.arms['B'].cash=D(999)
        b.arms['A'].cash=D(998);b.arms['B'].cash=D(1003)
        s=Scenarios(a);s.branches={'first':a,'second':b};s.primary={'A':'second','B':'first'}
        bounds=s.bounds();self.assertEqual(bounds['paired_difference_min'],D(-5))
        self.assertEqual(bounds['arms']['A']['minimum']-bounds['arms']['B']['minimum'],D(-1))
    def test_scenario_resource_cap_is_incomplete(self):
        e=Engine(fixture());s=Scenarios(e,max_branches=1)
        self.assertFalse(s.episode([dict(id='a',batches=[]),dict(id='b',batches=[])],dict(coverage_review='test',sha256='synthetic')))
        self.assertIn('SCENARIO_RESOURCE_LIMIT',s.incomplete)
    def test_counts_only_extension_excludes_performance(self):
        arms=Engine(fixture()).arms;h=Holdout(0,100,200,'synthetic-freeze')
        result=h.counts_only(arms,100);self.assertEqual(h.end,200)
        self.assertNotIn('cash',canonical(result));self.assertNotIn('net',canonical(result))
        with self.assertRaises(ValueError):h.counts_only(arms,100)
        h.endpoint(arms,200);self.assertTrue(all(a.ended for a in arms.values()))
    def test_incomplete_research_cannot_pass(self):
        result=acceptance([],[],[])
        self.assertEqual(result['disposition'],'INVALID_INCOMPLETE_EVIDENCE')
    def test_missing_settlement_does_not_become_zero_funding(self):
        m=fixture();m.data['funding_calendar']=[dict(symbol='TESTUSDT',at=360)]
        e=Engine(m);a=e.arms['A'];a.marks['TESTUSDT']=D(100);a.mark_times['TESTUSDT']=300;a.reset(300)
        o=a.reserve(signal(),300,D(100));a.fill_entry('TESTUSDT',300,o.qty,o.entry,D(1),'SYNTHETIC')
        e.batch(batch(360));self.assertTrue(any(x.startswith('FUNDING_MISSING') for x in e.incomplete));self.assertFalse(a.data_valid)
    def test_http_health_is_distinct_from_paper_readiness(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        try:
            with urllib.request.urlopen(url+'/healthz') as r:self.assertEqual(r.status,200)
            with urllib.request.urlopen(url+'/api/status') as r:self.assertFalse(json.load(r)['forward_paper_operational'])
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+'/readyz')
            self.assertEqual(error.exception.code,503)
            request=urllib.request.Request(url+'/api/status',data=b'{}',method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,501)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
