"""Native Bybit boundaries and exact convention reconciliation; synthetic cases."""
import json,tempfile,threading,unittest,gzip
from pathlib import Path
from dataclasses import replace,asdict
from phoenix_v2.bybit_public import TradeStream,coverage,reconciliation
from phoenix_v2.forward import Runtime
from phoenix_v2.journal import Journal
from phoenix_v2.model import D,digest
from phoenix_v2.universe import SYMBOLS
from test_forward import bar,trade,rule,signal

def proof(b):
    row=dict(start=b.start*1000,end=b.end*1000-1,interval='1',confirm=True,
        open=str(b.o),high=str(b.h),low=str(b.l),close=str(b.c),volume=str(b.v),timestamp=b.end*1000-1)
    return dict(symbol=b.symbol,received_at=b.end+1,source_topic='kline.1.'+b.symbol,row=row)

class ReconciliationTests(unittest.TestCase):
    def test_same_real_wire_minutes_reconcile_all_twelve_without_changing_trades(self):
        fixture=json.loads(gzip.decompress((Path(__file__).parent/'fixtures'/'bybit_native_20261109.json.gz').read_bytes()))
        seen=set();old_clean=new_clean=0
        for row in fixture['rows']:
            c=row['confirmed']['row'];s=row['symbol'];seen.add(s)
            b=replace(bar(start=row['start']),symbol=s,o=D(c['open']),h=D(c['high']),l=D(c['low']),c=D(c['close']),v=D(c['volume']))
            self.assertTrue(row['rest_matches_ws']);self.assertGreaterEqual(row['start'],fixture['subscription_ack_at'])
            old_clean+=coverage(b,row['trades']) is None
            self.assertIsNone(coverage(b,row['trades'],previous_close=row['previous_close'],confirmed=row['confirmed']))
            new_clean+=1
        self.assertEqual(seen,set(SYMBOLS));self.assertEqual(new_clean,24);self.assertLess(old_clean,new_clean)
    def test_all_twelve_native_denominations_use_exact_previous_close_anchor(self):
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                b=replace(bar(o='100',h='101',l='100',c='101'),symbol=symbol)
                ticks=[dict(trade(at=300123,p='101'),s=symbol)]
                self.assertEqual(coverage(b,ticks),'trade_ohlcv_mismatch')
                r=reconciliation(b,ticks,previous_close='100',confirmed=proof(b))
                self.assertIsNone(r['reason']);self.assertEqual(r['mode'],'previous_close_confirmed')
    def test_anchor_can_be_native_high_and_is_not_a_synthetic_trade(self):
        b=bar(o='100',h='100',l='99',c='99');ticks=[trade(at=301000,p='99')];before=json.dumps(ticks)
        self.assertIsNone(coverage(b,ticks,previous_close='100',confirmed=proof(b)))
        self.assertEqual(json.dumps(ticks),before);self.assertEqual(len(ticks),1)
    def test_anchor_requires_closed_native_confirmation(self):
        b=bar(o='100',h='101',l='100',c='101');ticks=[trade(p='101')]
        self.assertEqual(coverage(b,ticks,previous_close='100'),'native_candle_confirmation_missing')
        p=proof(b);p['row']['confirm']=False
        self.assertEqual(coverage(b,ticks,previous_close='100',confirmed=p),'confirmed_native_rest_disagreement')
    def test_native_rest_disagreement_rejects_even_exact_trade_ohlcv(self):
        b=bar();p=proof(b);p['row']['volume']='101'
        self.assertEqual(coverage(b,[trade()],confirmed=p),'confirmed_native_rest_disagreement')
    def test_wrong_previous_close_or_missing_volume_is_not_repaired(self):
        b=bar(o='100',h='101',l='100',c='101')
        for previous,ticks in (('99',[trade(p='101')]),('100',[trade(p='101',v='99')])):
            self.assertEqual(coverage(b,ticks,previous_close=previous,confirmed=proof(b)),'trade_ohlcv_mismatch')
    def test_boundary_is_half_open_and_bad_coverage_still_wins(self):
        b=bar();p=proof(b)
        self.assertIsNone(coverage(b,[trade(at=300000)],confirmed=p))
        self.assertEqual(coverage(b,[trade(at=360000)],confirmed=p),'trade_outside_candle')
        self.assertEqual(coverage(b,[trade()],bad='stream_disconnected',previous_close='100',confirmed=p),'stream_disconnected')
    def test_native_confirmation_cannot_arrive_before_close(self):
        p=proof(bar());p['received_at']=359
        self.assertEqual(coverage(bar(),[trade()],confirmed=p),'confirmed_native_rest_disagreement')

class StreamTests(unittest.TestCase):
    def stream(self):
        s=TradeStream(SYMBOLS,threading.Event());s.connected=True;s.coverage_start=240;return s
    def test_multiple_messages_same_sequence_keep_unique_trades(self):
        s=self.stream();s.add([trade(v='50')],received=301);s.add([trade(at=300001,v='50',i='two')],received=301)
        rows,bad=s.take('BTCUSDT',300);self.assertIsNone(bad);self.assertEqual(len(rows),2)
        self.assertEqual(sum(D(r['v']) for r in rows),D(100))
    def test_sequence_number_gap_is_not_assumed_packet_loss(self):
        s=self.stream();s.add([trade(v='50'),dict(trade(at=300001,v='50',i='two'),seq=1000)],received=301)
        self.assertIsNone(s.take('BTCUSDT',300)[1])
    def test_sequence_regression_is_explicit_quarantine(self):
        s=self.stream();s.add([trade(v='50'),dict(trade(at=300001,v='50',i='two'),seq=99)],received=301)
        self.assertEqual(s.take('BTCUSDT',300)[1],'trade_sequence_regression');self.assertEqual(s.sequence_regressions,1)
    def test_flags_are_preserved_and_late_trade_is_retained_separately(self):
        s=self.stream();s.add([dict(trade(),BT=False,RPI=True)],received=301)
        rows,bad=s.take('BTCUSDT',300);self.assertTrue(rows[0]['RPI']);self.assertFalse(rows[0]['BT'])
        s.add([trade(at=359999,i='late')],received=361)
        late=s.take_late();self.assertEqual(late[0]['trade']['i'],'late');self.assertFalse(s.take_late())
        self.assertFalse(s.take('BTCUSDT',300)[0])
    def test_forming_native_candle_ignored_and_inclusive_end_validated(self):
        s=self.stream();p=proof(bar());r=dict(p['row'],confirm=False)
        s.add_candles('kline.1.BTCUSDT',[r],received=361);self.assertIsNone(s.take_native('BTCUSDT',300))
        s.add_candles('kline.1.BTCUSDT',[p['row']],received=361)
        self.assertEqual(s.take_native('BTCUSDT',300)['row'],p['row'])
        r=dict(p['row'],end=360000)
        with self.assertRaises(ValueError):s.add_candles('kline.1.BTCUSDT',[r],received=361)
    def test_conflicting_confirmed_candle_is_not_overwritten(self):
        s=self.stream();r=proof(bar())['row'];s.add_candles('kline.1.BTCUSDT',[r],received=361)
        s.add_candles('kline.1.BTCUSDT',[dict(r,volume='101')],received=362)
        self.assertEqual(s.take('BTCUSDT',300)[1],'conflicting_confirmed_candle')
        self.assertEqual(s.take_native('BTCUSDT',300)['row']['volume'],'100')

class RuntimeFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.j=Journal(self.tmp.name);self.r=Runtime(self.j)
        self.r.state['previous']=300;self.r.state['contracts']={'BTCUSDT':[rule()]};self.r.bind()
        self.r.state['quality']={'BTCUSDT':dict(expected=asdict(bar(start=240)))}
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def run_minute(self):
        b=bar(o='100',h='101',l='100',c='101');p=proof(b)
        self.r.minute(360,{'BTCUSDT':[b]},{'BTCUSDT':bar()}, {'BTCUSDT':([trade(p='101')],None,p)},{})
    def test_confirmed_anchor_is_archived_without_fabricating_price_points(self):
        self.run_minute();self.assertEqual(self.r.state['clean_symbols'],['BTCUSDT'])
        self.assertEqual(self.j.evidence_counts['confirmed_native_candle'],1)
        data=self.j.minute_evidence('BTCUSDT',300);values={x['kind']:x['value'] for x in data['items']}
        self.assertEqual(len(values['public_trades']),1);self.assertEqual(values['public_trades'][0]['p'],'101')
        for x in data['items']:self.assertEqual(x['canonical_sha256'],digest(x['value']))
        self.assertEqual(self.j.counts.get('entry_fill',0),0)
    def test_pending_opening_entry_remains_quarantined_without_price_evidence(self):
        a=self.r.state['arms']['A'];a.marks['BTCUSDT']=D(100);a.mark_times['BTCUSDT']=300;a.reset(300)
        a.reserve(signal(),300,D(100));self.r.state['pending']=[signal()]
        self.run_minute()
        self.assertEqual(self.r.state['quality']['BTCUSDT']['reason'],'opening_trade_reference_ambiguous')
        self.assertEqual(self.j.counts.get('entry_fill',0),0);self.assertFalse(a.orders);self.assertFalse(a.positions)
    def test_late_evidence_is_committed_before_a_later_public_api_failure(self):
        from phoenix_v2.forward import Worker
        w=Worker(self.tmp.name,('BTCUSDT',));w.runtime=self.r;w.journal=self.j
        w.stream.late=[dict(trade=trade(at=299999,i='late'),received_at=301,reason='received_after_minute_sealed')]
        self.r.state['clean_symbols']=['BTCUSDT']
        def fail(*a):raise OSError('synthetic unavailable API')
        self.r.update_contracts=fail
        with self.assertRaises(OSError):w.cycle()
        self.assertEqual(self.j.evidence_counts['late_public_trades'],1)
        self.assertEqual(self.j.counts['late_trade_evidence'],1);self.assertFalse(self.r.state['clean_symbols'])
    def test_read_only_evidence_http_is_bounded_to_requested_symbol_and_minute(self):
        from phoenix_v2 import service
        from phoenix_v2.forward import Worker
        from http.server import ThreadingHTTPServer
        import urllib.request,urllib.error
        self.run_minute();head=self.j.head;old=service.WORKER
        w=Worker(self.tmp.name,('BTCUSDT',));w.journal=self.j;service.WORKER=w
        server=ThreadingHTTPServer(('127.0.0.1',0),service.ForwardHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urllib.request.urlopen(base+'/api/evidence?symbol=BTCUSDT&start=300') as r:data=json.load(r)
            self.assertEqual(len(data['items']),4)
            for query,code in (('symbol=BTCUSDT&start=301',400),('symbol=UNREQUESTED&start=300',400),('symbol=BTCUSDT&start=0',404)):
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(base+'/api/evidence?'+query)
                self.assertEqual(error.exception.code,code)
            self.assertEqual(self.j.head,head)
        finally:
            server.shutdown();thread.join();server.server_close();service.WORKER=old

if __name__=='__main__':unittest.main()
