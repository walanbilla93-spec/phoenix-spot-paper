import io
import gzip
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from research_engine import ResearchEngine, DAY, HOUR, MINUTE, FIVE, LAYERS, context_gates, footprint_permission
from research_replay import audit, replay

S = 'SOLUSDT'


def quote(price, ts, size=10000):
    return dict(bid=price-.005, ask=price+.005, bid_size=size, ask_size=size, exchange_ms=ts)


def ready_plan(engine, ts=3*DAY, side=1, name='case'):
    zone = dict(low=100., high=104., side=side, id='zone', available_ms=ts-10000)
    profile = dict(passed=True, day=2*DAY, poc=120. if side == 1 else 80.)
    gates = {layer:dict(passed=True, available_ms=ts) for layer in LAYERS}
    gates['profile'] = profile
    setup = dict(id=name, side=side, zone=zone, gates=gates, row_width=.1,
        local_atr=10., instrument=dict(tick=.01, step=.001, minimum=.001, min_notional=5.),
        reference_unbroken=True)
    plan = dict(campaign_id=name, symbol=S, side=side, setup=setup,
        stop=98. if side == 1 else 106., original_stop=98. if side == 1 else 106.,
        trigger_close=103. if side == 1 else 101., signal_ms=ts, expires_ms=ts+FIVE,
        profile_day=2*DAY)
    engine.ready[S] = plan
    engine._event('research_qualified', ts, S, campaign_id=name, plan=plan)
    return plan


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.e=ResearchEngine(self.root, [S])

    def tearDown(self):
        self.e.db.close(); self.tmp.cleanup()

    def test_no_closed_trades_is_unavailable_not_zero_wr(self):
        self.assertEqual(self.e.status()['qualified_setups'], 0)
        self.assertIsNone(self.e.status()['win_rate_pct'])
        self.assertTrue(audit(self.e.path)['audit_ok'])

    def test_pivots_causal_and_all_context_requirements(self):
        points=[98,100,99,102,100,104,102,103]; bars=[]
        for i in range(len(points)-1):
            for j in range(4):
                price=points[i]+(points[i+1]-points[i])*j/4
                bars.append(dict(t=len(bars)*HOUR,o=price,h=price+.1,l=price-.1,c=price,available_ms=(len(bars)+1)*HOUR))
        g=context_gates(bars,1,28*HOUR)
        self.assertTrue(g['elliott']['passed']); self.assertTrue(g['smc_context']['passed'])
        self.assertFalse(context_gates(bars,1,5*HOUR)['elliott']['passed'])
        self.assertFalse(context_gates(bars,-1,28*HOUR)['smc_context']['passed'])
        self.assertFalse(context_gates(bars,1,40*HOUR)['elliott']['passed'])

    def test_footprint_requires_both_delta_and_adjacent_diagonal_stack(self):
        levels=[[str(100+i/10),'10','1'] for i in range(4)]
        self.assertTrue(footprint_permission(levels,1,.1)['passed'])
        self.assertFalse(footprint_permission([['100','1000','0']],1,.1)['passed'])
        self.assertFalse(footprint_permission([['100','10','1'],['102','10','1'],['104','10','1']],1,.1)['passed'])

    def test_profile_requires_every_minute_and_late_data_revokes_it(self):
        with self.e.db:
            self.e.db.executemany('INSERT INTO minutes VALUES (?,?,?,?,?)',[(S,i*MINUTE,'1',1,'') for i in range(1440)])
            self.e.db.executemany('INSERT INTO levels VALUES (?,?,?,?)',[(S,0,'100','31'),(S,0,'110','40'),(S,0,'120','29')])
        p=self.e._profile(S,0,.01)
        self.assertTrue(p['passed']); self.assertAlmostEqual(p['val'],100.)
        self.assertGreater(p['poc'],110.); self.assertLess(p['poc'],111.)
        self.e._invalidate_day(S,MINUTE,'LATE_TRADE')
        self.assertFalse(self.e._profile(S,0,.01)['passed'])

    def test_entry_latency_spread_and_displayed_size_are_real_filters(self):
        p=ready_plan(self.e); t=p['signal_ms']
        self.e.quote(S,quote(103,t+249),t+249)
        self.assertIsNone(self.e.position)
        self.e.quote(S,quote(103,t+250,0),t+250)
        self.assertEqual(self.e.status()['missed'],1); self.assertIsNone(self.e.position)

    def test_long_partial_targets_costs_and_restart_keep_closed_results(self):
        p=ready_plan(self.e); t=p['signal_ms']
        self.e.quote(S,quote(103,t+250),t+250)
        self.assertEqual(self.e.status()['entered'],1)
        p=self.e.position; first=p['target1']+.02; second=p['target2']+.02
        self.e.quote(S,quote(first,t+500),t+500)
        self.e.quote(S,quote(first,t+750),t+750)
        self.assertTrue(self.e.position['target1_done']); self.assertEqual(self.e.status()['closed'],0)
        self.e.quote(S,quote(second,t+1000),t+1000)
        self.e.quote(S,quote(second,t+1250),t+1250)
        status=self.e.status(); self.assertEqual(status['closed'],1); self.assertEqual(status['win_rate_pct'],100.)
        self.assertGreater(status['model_cost_R'],0); self.assertGreater(status['net_R'],0)
        self.assertTrue(audit(self.e.path)['audit_ok'])
        self.e.db.close(); self.e=ResearchEngine(self.root,[S])
        self.assertAlmostEqual(self.e.status()['net_R'],status['net_R'])
        self.assertEqual(self.e.status()['closed'],1)

    def test_short_stop_gap_uses_quote_not_stop_and_restart_censors_open(self):
        plan=ready_plan(self.e,side=-1); t=plan['signal_ms']
        self.e.quote(S,quote(101,t+250),t+250)
        self.e.quote(S,quote(108,t+500),t+500)
        self.e.quote(S,quote(109,t+750),t+750)
        self.assertEqual(self.e.status()['losses'],1)
        close=json.loads(self.e.db.execute("SELECT payload FROM ledger WHERE kind='paper_closed'").fetchone()[0])
        self.assertLess(close['net_R'],0)
        ready_plan(self.e,t+1000,name='open'); self.e.quote(S,quote(103,t+1250),t+1250)
        self.e.db.close(); self.e=ResearchEngine(self.root,[S],recover_ms=t+1500)
        self.assertEqual(self.e.status()['censored'],1); self.assertEqual(self.e.status()['closed'],1)

    def test_gaps_stale_quotes_and_invalid_footprints_never_become_losses(self):
        for n, mode in enumerate(['gap','stale','footprint']):
            t=3*DAY+n*MINUTE; self.e.quote(S,quote(103,t),t); ready_plan(self.e,t,name=mode)
            self.e.quote(S,quote(103,t+250),t+250)
            if mode=='gap': self.e.boundary(t+500,'TEST_GAP')
            elif mode=='stale': self.e.quote(S,quote(104,t-5000),t+500)
            else: self.e.record(dict(kind='footprint_late_trade',symbol=S,minute_ms=t,receive_ms=t+500))
            self.assertIsNone(self.e.position)
        self.assertEqual(self.e.status()['censored'],3); self.assertEqual(self.e.status()['closed'],0)
        self.assertIsNone(self.e.status()['win_rate_pct'])

    def test_late_data_retracts_already_closed_affected_outcome(self):
        plan=ready_plan(self.e,side=-1); t=plan['signal_ms']
        self.e.quote(S,quote(101,t+250),t+250)
        self.e.quote(S,quote(108,t+500),t+500); self.e.quote(S,quote(109,t+750),t+750)
        self.assertEqual(self.e.status()['closed'],1)
        self.e.record(dict(kind='footprint_late_trade',symbol=S,minute_ms=2*DAY,receive_ms=t+1000))
        self.assertEqual(self.e.status()['closed'],0); self.assertIsNone(self.e.status()['win_rate_pct'])
        self.assertEqual(self.e.status()['revoked_outcomes'],1)
        self.assertTrue(audit(self.e.path)['audit_ok'])

    def test_ladder_independently_confirmed_improves_average_and_budget(self):
        plan=ready_plan(self.e); t=plan['signal_ms']
        self.e.quote(S,quote(103,t+250),t+250)
        p=self.e.position; initial=p['average']; quantity=p['quantity']
        self.assertFalse(p['no_adds'])
        # A price visit by itself cannot add anything.
        self.e.quote(S,quote(102,t+500),t+500); self.assertEqual(p['legs'],1)
        p['pending_add']=dict(at=102.,signal_ms=t+500,rung=1,expires_ms=t+FIVE)
        self.e.quote(S,quote(102,t+750),t+750)
        self.assertEqual(p['legs'],2); self.assertLess(p['average'],initial); self.assertGreater(p['quantity'],quantity)
        record=json.loads(self.e.db.execute("SELECT payload FROM ledger WHERE kind='paper_add'").fetchone()[0])
        self.assertLessEqual(record['reserved_stop_loss'],2.5+1e-8)
        self.assertEqual(p['stop'],p['original_stop'])

    def test_validated_minute_flow_and_missing_candle_fail_closed(self):
        t=3*DAY
        candle=dict(t=t,o=100,h=101,l=99,c=100,v='1')
        self.e.record(dict(kind='candle_1m',symbol=S,receive_ms=t+MINUTE,candle=candle))
        fp=dict(kind='footprint_1m',symbol=S,receive_ms=t+MINUTE+5000,minute_ms=t,
            complete_observation=True,quality_flags=[],buy_volume='1',sell_volume='0',levels=[['100','1','0',1,0]])
        self.e.record(fp)
        self.assertEqual(self.e.db.execute('SELECT valid FROM minutes WHERE s=? AND t=?',(S,t)).fetchone()[0],1)
        self.assertIsNone(self.e.halted)
        bad=dict(fp,minute_ms=t+MINUTE,receive_ms=t+2*MINUTE+5000)
        self.e.record(bad); self.e.quote(S,quote(100,t+3*MINUTE),t+3*MINUTE)
        self.assertEqual(len(self.e.pending[S]),0)
        self.assertEqual(self.e.status()['qualified_setups'],0)

    def test_actual_seven_layer_qualification_to_executable_entry(self):
        t=3*DAY; points=[98,100,99,102,100,104,102,103]; bars=[]
        for i in range(len(points)-1):
            for j in range(4):
                price=points[i]+(points[i+1]-points[i])*j/4
                bars.append(dict(t=t-28*HOUR+len(bars)*HOUR,o=price,h=price+.1,l=price-.1,c=price,available_ms=t-27*HOUR+len(bars)*HOUR))
        self.e.h1[S].extend(bars)
        self.e.instruments[S]=dict(tick=.01,step=.001,minimum=.001,min_notional=5.,available_ms=t-1000)
        with self.e.db:
            self.e.db.executemany('INSERT INTO minutes VALUES (?,?,?,?,?)',[(S,2*DAY+i*MINUTE,'1',1,'') for i in range(1440)])
            self.e.db.executemany('INSERT INTO levels VALUES (?,?,?,?)',[(S,2*DAY,'102','31'),(S,2*DAY,'110','40'),(S,2*DAY,'120','29')])
        for i in range(30):
            self.e.m5[S].append(dict(t=t-(30-i)*FIVE,o=103.5,h=103.8 if i==24 else 103.6,
                l=103.2 if i==21 else 103.4,c=103.5,v='1',available_ms=t-(29-i)*FIVE))
        self.e.failed_zones[S].append(dict(id='nearer',side=1,low=105,high=106,failed_ms=t-1000))
        zone=dict(id='intended',side=1,low=102.,high=104.,available_ms=t-10000,formed_ms=t-10000,touched=False)
        self.e.zones[S].append(zone); self.e.last_ms=t
        self.e.quote(S,quote(103.5,t),t)
        w=self.e.watches[S]
        self.assertIsNotNone(w)
        self.assertTrue(w['gates']['elliott']['passed']); self.assertTrue(w['gates']['smc_context']['passed'])
        self.assertTrue(w['gates']['profile']['passed']); self.assertTrue(w['reference_unbroken'])
        width=w['row_width']; start=round(103.7/width)*width
        levels=[[str(start+i*width),'10','1'] for i in range(4)]
        for i,prices in enumerate([(103.5,103.6,103.4,103.5),(103.5,103.65,103.1,103.6),(103.5,104.1,103.5,104.)]):
            at=t+i*FIVE; o,h,l,c=prices
            self.e._closed_bar(S,dict(t=at,o=o,h=h,l=l,c=c,v='44',available_ms=at+FIVE+5000),levels,at+FIVE+5000)
        self.assertEqual(self.e.status()['qualified_setups'],1)
        self.assertEqual(self.e.status()['evaluated_setups'],1)
        when=t+3*FIVE+5000
        # Keep quote coverage continuous while the three five-minute bars form.
        self.e.last_quote_ms[S]=when
        self.e.quote(S,quote(103.5,when+250),when+250)
        self.assertEqual(self.e.status()['entered'],1)
        self.assertEqual(self.e.status()['closed'],0)
        self.assertTrue(audit(self.e.path)['audit_ok'])

    def test_snapshot_and_hash_tampering_detected(self):
        self.e.boundary(1,'TEST')
        target=self.root/'snapshot.sqlite'; self.e.snapshot(target)
        self.assertTrue(audit(target)['audit_ok'])
        db=sqlite3.connect(target); db.execute("UPDATE ledger SET payload='{}'"); db.commit(); db.close()
        self.assertFalse(audit(target)['audit_ok'])

    def test_streamed_archive_replay_deduplicates_inputs_and_audits_snapshot(self):
        self.e.boundary(1,'TEST')
        snapshot=self.root/'research-export.sqlite'; self.e.snapshot(snapshot)
        record=dict(kind='raw',receive_ms=10,boot_id='x',receive_monotonic_ns=1,
            payload=dict(topic='orderbook.1.SOLUSDT',cts=10,data=dict(b=[['100','2']],a=[['100.01','3']])))
        raw=gzip.compress((json.dumps(record)+'\n'+json.dumps(record)+'\n').encode())
        archive=self.root/'capture.tar'
        with tarfile.open(archive,'w') as tar:
            info=tarfile.TarInfo('1.jsonl.gz');info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
            tar.add(snapshot,arcname='research-export.sqlite')
        report=replay(archive,self.root/'replay-work')
        self.assertEqual(report['input']['duplicates_removed'],1)
        self.assertTrue(report['audit']['audit_ok']); self.assertTrue(report['exported_snapshot_audit']['audit_ok'])


if __name__=='__main__': unittest.main()
