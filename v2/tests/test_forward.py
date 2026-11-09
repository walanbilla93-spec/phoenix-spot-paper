"""Synthetic failure/recovery checks. Fixtures are never market evidence."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from phoenix_v2 import checkpoint
from phoenix_v2.bybit_public import PublicAPI, TradeStream, coverage
from phoenix_v2.forward import Runtime, PaperPolicy, Worker, POLICY
from phoenix_v2.journal import Journal
from phoenix_v2.manifest import Manifest
from phoenix_v2.model import Bar, D, digest
from phoenix_v2.objects import Zone
from phoenix_v2.strategy import Signal

SYMBOL='BTCUSDT'
def bar(start=300,tf=1,o='100',h='100',l='100',c='100',v='100'):
    return Bar(SYMBOL,tf,start,*map(D,(o,h,l,c,v)))
def trade(at=300000,p='100',v='100',i='trade-1',side='Sell'):
    return dict(T=at,s=SYMBOL,S=side,p=p,v=v,i=i,seq=100)
def rule():
    return dict(start=0,end=9999999999,tick='.01',step='.001',minimum_qty='.001',
        minimum_notional='1',multiplier='1',initial_margin='.33333333334',
        taker='.00055',maker='.00055',free_collateral='1000',funding_minutes=480)
def signal():
    z=Zone('exec',5,1,'FVG',D('97'),D('99'),0,300,300)
    return Signal('test-signal',SYMBOL,1,300,0,D('95'),D('120'),D('1'),D('2'),D('96'),z,D('100'),{})

class AdapterTests(unittest.TestCase):
    def test_only_public_get_paths(self):
        with self.assertRaises(ValueError): PublicAPI().get('/v5/order/create')
    def test_closed_bar_filter_and_ascending_order(self):
        api=PublicAPI()
        api.get=lambda *a,**k:dict(symbol=SYMBOL,category='linear',list=[
            ['360000','100','101','99','100','1'],['300000','100','101','99','100','1'],
            ['240000','100','101','99','100','1']])
        rows=api.candles(SYMBOL,1,360)
        self.assertEqual([b.start for b in rows],[240,300])
    def test_wrong_instrument_rejected(self):
        api=PublicAPI();api.get=lambda *a,**k:dict(symbol='ETHUSDT',category='linear',list=[])
        with self.assertRaises(ValueError):api.candles(SYMBOL,1,360)
    def test_reconciliation_requires_volume_and_all_ohlc(self):
        self.assertIsNone(coverage(bar(),[trade()]))
        self.assertEqual(coverage(bar(),[trade(v='99')]),'trade_ohlcv_mismatch')
        self.assertIsNotNone(coverage(bar(),[trade(p='101')]))
        self.assertIsNotNone(coverage(bar(),[trade(at=360000)]))
        self.assertEqual(coverage(bar(),[]),'no_chronological_trades')
    def test_dedup_by_trade_id_not_cross_sequence(self):
        stream=TradeStream((SYMBOL,),threading.Event());stream.coverage_start=240;stream.connected=True
        stream.add([trade(),trade(),trade(at=300001,i='trade-2')],received=301)
        rows,bad=stream.take(SYMBOL,300)
        self.assertIsNone(bad);self.assertEqual(len(rows),2);self.assertEqual(stream.total,2)
    def test_partial_interval_and_disconnect_are_quarantined(self):
        stream=TradeStream((SYMBOL,),threading.Event());stream.coverage_start=301;stream.connected=True
        stream.add([trade()],received=302)
        self.assertEqual(stream.take(SYMBOL,300)[1],'stream_not_present_at_interval_start')
        stream.coverage_start=240;stream.connected=False
        self.assertEqual(stream.take(SYMBOL,300)[1],'stream_disconnected')
    def test_conflicting_trade_id(self):
        stream=TradeStream((SYMBOL,),threading.Event());stream.coverage_start=240;stream.connected=True
        stream.add([trade(),trade(p='101')],received=302)
        self.assertEqual(stream.take(SYMBOL,300)[1],'conflicting_trade_id')

class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.j=Journal(self.tmp.name);self.r=Runtime(self.j)
        self.r.state['contracts']={SYMBOL:[rule()]};self.r.bind();self.r.state['previous']=300
        self.r.commit()
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def test_immutable_evidence_and_atomic_conflict_rollback(self):
        self.j.commit([('x','test',1,{'x':1})],[dict(at=1,event='one')],checkpoint.dumps(self.r.state))
        seq=self.j.sequence;head=self.j.head
        with self.assertRaises(ValueError):
            self.j.commit([('y','test',2,{}),('x','test',1,{'x':2})],[],checkpoint.dumps(self.r.state))
        self.assertEqual(self.j.sequence,seq);self.assertEqual(self.j.head,head)
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM evidence WHERE key='y'").fetchone()[0],0)
        with self.assertRaises(sqlite3.IntegrityError):self.j.db.execute("DELETE FROM evidence")
        self.j.db.rollback()
    def test_single_worker_owns_volume(self):
        with self.assertRaises(RuntimeError):Journal(self.tmp.name)
    def test_restart_restores_independent_accounts_pending_and_aliases(self):
        r=self.r;a=r.state['arms']['A'];b=r.state['arms']['B'];s=signal()
        for arm in (a,b):arm.marks[SYMBOL]=D(100);arm.mark_times[SYMBOL]=300;arm.reset(300)
        a.reserve(s,300,D(100));b.reserve(s,300,D(100));r.state['pending']=[s]
        a.cash=D('999.1234');r.commit();head=self.j.head
        self.j.close();self.j=Journal(self.tmp.name);r=Runtime(self.j)
        self.assertEqual(r.state['arms']['A'].cash,D('999.1234'))
        self.assertEqual(r.state['arms']['B'].cash,D('1000'))
        self.assertIs(r.state['arms']['A'].orders[SYMBOL].signal,r.state['pending'][0])
        self.assertIs(r.state['arms']['B'].orders[SYMBOL].signal,r.state['pending'][0])
        self.assertEqual(self.j.head,head)
    def test_structural_graph_forward_references_survive_sorted_json(self):
        from phoenix_v2.structure import Pivot
        f=self.r.state['strategies'][SYMBOL].frames[5]
        p=Pivot('confirmed',1,D(110),0,300,300)
        # Raw is serialized before chain, but canonical JSON sorts chain first.
        f.raw=[p];f.chain=[p];f.leg=(p,p);f.bars=[bar(0,tf=5)]
        self.r.commit();restored=Runtime(self.j).state['strategies'][SYMBOL].frames[5]
        self.assertIs(restored.raw[0],restored.chain[0])
        self.assertIs(restored.leg[0],restored.raw[0])
        self.assertIs(restored.leg[0],restored.leg[1])
    def test_corrupt_checkpoint_fails_closed(self):
        self.j.db.execute("UPDATE checkpoint SET payload=X'00'");self.j.db.commit()
        with self.assertRaises(ValueError):self.j.verify()
    def test_unsupported_checkpoint_class_rejected(self):
        with self.assertRaises(ValueError):checkpoint.loads('{"version":1,"graph":{"id":0,"type":"eval","value":{}}}')
    def test_no_fabricated_trades_on_gap_and_liability_retained(self):
        a=self.r.state['arms']['A'];s=signal()
        a.marks[SYMBOL]=D(100);a.mark_times[SYMBOL]=300;a.reset(300)
        o=a.reserve(s,300,D(100));self.assertIsNotNone(o)
        a.fill_entry(SYMBOL,300,o.qty,o.entry,D(1),'SYNTHETIC')
        qty=a.positions[SYMBOL].qty
        self.r.minute(360,{SYMBOL:[bar()]},{SYMBOL:bar()}, {SYMBOL:([],'disconnect')},{})
        self.assertEqual(a.positions[SYMBOL].qty,qty);self.assertFalse(a.data_valid)
        self.assertTrue(self.r.state['unresolved'])
        self.assertEqual(self.j.counts.get('exit_fill',0),0)
        self.assertEqual(self.j.counts['liability_quarantined'],1)
    def test_public_trade_entry_model_is_partial_and_arm_independent(self):
        s=signal();a=self.r.state['arms']['A'];b=self.r.state['arms']['B']
        for arm in (a,b):arm.marks[SYMBOL]=D(100);arm.mark_times[SYMBOL]=300;arm.reset(300);arm.reserve(s,300,D(100))
        # A bias must survive the exact entry checkpoint.
        st=self.r.state['strategies'][SYMBOL];st.frames[240].regime=1;st.frames[240].leg=(object(),object())
        # Replace test-only leg with allowed objects for serialization.
        from phoenix_v2.structure import Pivot
        st.frames[240].leg=(Pivot('a',-1,D(90),0,0),Pivot('b',1,D(110),0,0))
        st.frames[5].atr.value=D(1);self.r.state['pending']=[s]
        self.r.minute(360,{SYMBOL:[bar(v='10')]},{SYMBOL:bar()}, {SYMBOL:([trade(v='10')],None)},{})
        self.assertIn(SYMBOL,a.positions);self.assertEqual(a.positions[SYMBOL].qty,D('0.10'))
        self.assertNotIn(SYMBOL,b.positions);self.assertIn(SYMBOL,b.orders)
        self.assertLess(a.cash,b.cash);self.assertEqual(self.j.counts['entry_fill'],1)
    def test_market_entry_and_gap_stop_round_adversely_to_legal_tick(self):
        a=self.r.state['arms']['A'];s=signal()
        self.r.state['contracts'][SYMBOL][0]['tick']='.1'
        a.marks[SYMBOL]=D(100);a.mark_times[SYMBOL]=300;a.reset(300)
        o=a.reserve(s,300,D(100));self.assertEqual(o.entry,D('100.1'))
        a.attempt_a(s,300,D(100),dict(qty='.1',evidence='SYNTHETIC'))
        self.assertEqual(a.positions[SYMBOL].entry,D('100.1'))
        a.price(SYMBOL,301,D('94.97'),D(1),D('.1'))
        exits=[e for e in self.r.events if e['event']=='exit_fill']
        self.assertEqual(exits[0]['price'],D('94.9'))
        self.assertLess(exits[0]['price'],D('94.97')-D('.05'))
    def test_different_prices_at_identical_millisecond_quarantined(self):
        rows=[trade(p='100',v='50'),trade(p='101',v='50',i='t2')]
        self.r.state['strategies'][SYMBOL].zones=[Zone('active',15,1,'OB',D(99),D(102),0,0,0)]
        self.r.minute(360,{SYMBOL:[bar(h='101',c='101')]},{SYMBOL:bar()}, {SYMBOL:(rows,None)},{})
        self.assertIn('same_millisecond',self.r.state['last_quarantine']['reason'])
        self.assertEqual(self.j.counts.get('entry_fill',0),0)
    def test_volume_mismatch_retains_diagnostic_evidence_without_fill(self):
        self.r.minute(360,{SYMBOL:[bar(v='100')]},{SYMBOL:bar()},
                      {SYMBOL:([trade(v='99')],None)},{})
        q=self.r.state['quality'][SYMBOL]
        self.assertEqual(q['expected']['v'],D(100));self.assertEqual(q['observed']['v'],D(99))
        self.assertEqual(q['trade_count'],1);self.assertTrue(q['trade_evidence_hash'])
        self.assertEqual(self.j.counts.get('entry_fill',0),0)
    def test_irrelevant_timestamp_ties_do_not_block_observation(self):
        rows=[trade(p='100',v='50'),trade(p='101',v='50',i='t2')]
        self.r.minute(360,{SYMBOL:[bar(h='101',c='101')]},{SYMBOL:bar()}, {SYMBOL:(rows,None)},{})
        self.assertEqual(self.r.state['last_clean'],360)
        self.assertEqual(self.j.counts.get('entry_fill',0),0)
    def test_active_order_makes_timestamp_tie_material(self):
        a=self.r.state['arms']['B'];s=signal()
        a.marks[SYMBOL]=D(100);a.mark_times[SYMBOL]=300;a.reset(300);a.reserve(s,300,D(100))
        self.assertTrue(self.r.ambiguous_ties(SYMBOL,[trade(),trade(p='101',i='t2')]))
    def test_missing_funding_preserves_exposure_and_blocks_new_risk(self):
        self.r.state['previous']=28740
        a=self.r.state['arms']['A'];s=signal()
        a.marks[SYMBOL]=D(100);a.mark_times[SYMBOL]=300;a.reset(300)
        o=a.reserve(s,300,D(100));a.fill_entry(SYMBOL,300,o.qty,o.entry,D(1),'SYNTHETIC')
        self.r.minute(28800,{SYMBOL:[bar(28740)]},{SYMBOL:bar(28740)},
                      {SYMBOL:([],'missing_trade_history')},{})
        self.assertIn(SYMBOL,a.positions);self.assertFalse(a.data_valid)
        self.assertEqual(self.j.counts['funding_missing'],1)
        self.assertEqual(self.j.counts.get('funding',0),0)
    def test_state_corruption_cannot_be_silently_reseeded(self):
        self.j.db.execute("UPDATE checkpoint SET head='wrong'");self.j.db.commit()
        with self.assertRaises(ValueError):self.j.verify()
    def test_minute_gap_rejected_not_flat_filled(self):
        with self.assertRaises(ValueError):self.r.minute(420,{}, {}, {}, {})
        self.assertEqual(self.r.state['previous'],300)
    def test_duplicate_committed_minute_cannot_advance(self):
        args=(360,{SYMBOL:[bar()]},{SYMBOL:bar()},{SYMBOL:([trade()],None)},{})
        self.r.minute(*args);seq=self.j.sequence
        with self.assertRaises(ValueError):self.r.minute(*args)
        self.assertEqual(seq,self.j.sequence)
    def test_paper_policy_never_closes_research_gates(self):
        self.assertFalse(POLICY['research_certified']);self.assertTrue(Manifest().missing())
        self.assertTrue(all(v['status']=='MISSING' for v in Manifest().report().values()))
    def test_storage_ceiling_stops_before_write(self):
        self.j.max_bytes=1
        with self.assertRaises(RuntimeError):self.r.commit()

class StatusTests(unittest.TestCase):
    def test_web_server_does_not_imply_live_worker(self):
        w=Worker('unused');s=w.status()
        self.assertFalse(s['worker_alive']);self.assertFalse(s['ready']);self.assertFalse(s['forward_paper_operational'])
    def test_ephemeral_storage_cannot_be_ready(self):
        w=Worker('unused');w.thread=threading.current_thread();w.heartbeat=time.time();w.last_success=time.time()
        w.view={'previous':int(time.time())//60*60}
        self.assertTrue(w.status()['forward_paper_operational']);self.assertFalse(w.status()['ready'])
    def test_stale_market_data_is_not_ready(self):
        w=Worker('unused',persistent=True);w.thread=threading.current_thread();w.heartbeat=time.time();w.last_success=time.time()
        w.view={'previous':time.time()-240}
        self.assertFalse(w.status()['ready'])
    def test_latest_quarantined_minute_is_not_execution_ready(self):
        w=Worker('unused',persistent=True);w.thread=threading.current_thread()
        w.heartbeat=time.time();w.last_success=time.time();w.stream.connected=True
        latest=int(time.time())//60*60
        w.view={'previous':latest,'last_clean':latest-60}
        self.assertTrue(w.status()['ready']);self.assertFalse(w.status()['execution_ready'])
        w.view['last_clean']=latest;self.assertTrue(w.status()['execution_ready'])
    def test_live_http_reports_worker_failure_even_when_web_is_running(self):
        from phoenix_v2 import service
        w=Worker('unused');w.error='synthetic worker failure';w.fatal=True
        with patch.object(service,'WORKER',w):
            server=ThreadingHTTPServer(('127.0.0.1',0),service.ForwardHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url='http://127.0.0.1:'+str(server.server_port)
            try:
                with urllib.request.urlopen(url+'/api/status') as response:
                    status=json.load(response)
                self.assertFalse(status['forward_paper_operational']);self.assertFalse(status['real_orders_enabled'])
                for path in ('/healthz','/readyz','/executionz'):
                    with self.assertRaises(urllib.error.HTTPError) as result:urllib.request.urlopen(url+path)
                    self.assertEqual(result.exception.code,503)
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(urllib.request.Request(url+'/api/trades',data=b'{}',method='POST'))
                self.assertEqual(result.exception.code,501)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
