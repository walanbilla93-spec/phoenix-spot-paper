"""Twelve-asset migration, isolated feed failure, accounting and full history."""
import json,tempfile,threading,time,unittest
from dataclasses import replace
from unittest.mock import patch
from phoenix_v2 import checkpoint
from phoenix_v2.forward import Runtime,Worker,LEGACY_POLICY_HASH,POLICY
from phoenix_v2.journal import Journal
from phoenix_v2.model import D,Bar,digest
from phoenix_v2.objects import Zone
from phoenix_v2.bybit_public import PublicAPI,TradeStream
from phoenix_v2.universe import SYMBOLS,META,assess
from test_forward import bar,trade,rule,signal

class TwelveTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.j=Journal(self.tmp.name)
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def test_explicit_native_denominations_and_no_unrequested_symbol(self):
        self.assertEqual(len(SYMBOLS),12);self.assertIn('SHIB1000USDT',SYMBOLS);self.assertIn('1000PEPEUSDT',SYMBOLS)
        self.assertEqual(META['SHIB1000USDT']['underlying_units_per_native_unit'],1000)
        with self.assertRaises(ValueError):Worker('unused',('SHIBUSDT',))
        with self.assertRaises(ValueError):Worker('unused',('BTCUSDT','BTCUSDT'))
    def test_authorized_additive_migration_preserves_balances_pending_and_journal(self):
        r=Runtime(self.j);r.state['policy_hash']=LEGACY_POLICY_HASH;r.state['previous']=300
        r.state['contracts']={'BTCUSDT':[rule()]};r.bind()
        for a in r.state['arms'].values():a.marks['BTCUSDT']=D(100);a.mark_times['BTCUSDT']=300;a.reset(300)
        r.state['arms']['A'].cash=D('997.125');r.state['arms']['B'].reserve(signal(),300,D(100))
        r.commit();old_head=self.j.head;old_seq=self.j.sequence
        migrated=Runtime(self.j,SYMBOLS)
        self.assertEqual(migrated.state['arms']['A'].cash,D('997.125'))
        self.assertIn('BTCUSDT',migrated.state['arms']['B'].orders)
        self.assertEqual(migrated.state['previous'],300);self.assertEqual(len(migrated.state['strategies']),12)
        migrated.commit();self.assertGreater(self.j.sequence,old_seq);self.assertNotEqual(self.j.head,old_head)
        self.assertEqual(self.j.counts['universe_expanded'],1)
        self.assertFalse(Runtime(self.j,SYMBOLS).events)
    def test_unknown_policy_migration_is_rejected_without_reset(self):
        r=Runtime(self.j);r.state['policy_hash']='unknown';r.commit();head=self.j.head
        with self.assertRaises(ValueError):Runtime(self.j,SYMBOLS)
        self.assertEqual(self.j.head,head)
    def test_restore_releases_old_graph_and_resets_dirty_before_rebinding(self):
        r=Runtime(self.j,SYMBOLS);r.state['arms']['A'].cash=D('998.25');r.commit()
        saved=self.j.load();decode=checkpoint.loads
        def checked(text):
            self.assertIsNone(r.state)
            return decode(text)
        r.log(300,'synthetic_uncommitted')
        with patch('phoenix_v2.forward.checkpoint.loads',side_effect=checked):r.restore(saved)
        self.assertEqual(len(r.state['strategies']),12);self.assertEqual(r.state['arms']['A'].cash,D('998.25'))
        self.assertFalse(r.dirty)
    def test_public_fetch_failure_does_not_reload_unchanged_strategy_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            w=Worker(directory,SYMBOLS)
            def failure():
                w.stop.set()
                raise OSError('synthetic public fetch failure')
            w.cycle=failure;w.stream.start=lambda:None
            with patch.object(Runtime,'restore',side_effect=AssertionError('unnecessary graph reload')) as restore:
                w.run()
                restore.assert_not_called()
            self.assertFalse(w.fatal);self.assertIn('synthetic public fetch failure',w.error)
    def test_one_bad_symbol_does_not_erase_clean_symbol_state(self):
        syms=('BTCUSDT','ETHUSDT');r=Runtime(self.j,syms);r.state['previous']=300
        r.state['contracts']={s:[rule()] for s in syms};r.bind()
        z=Zone('keep',15,1,'OB',D(90),D(95),0,0,0);r.state['strategies']['BTCUSDT'].zones=[z]
        frames={s:[replace(bar(),symbol=s)] for s in syms};marks={s:replace(bar(),symbol=s) for s in syms}
        streams={s:([dict(trade(),s=s,v='99' if s=='ETHUSDT' else '100')],None) for s in syms}
        r.minute(360,frames,marks,streams,{})
        self.assertEqual(r.state['clean_symbols'],['BTCUSDT']);self.assertIn(z,r.state['strategies']['BTCUSDT'].zones)
        self.assertTrue(r.state['arms']['A'].data_valid);self.assertFalse(r.state['unresolved'])
        self.assertEqual(r.state['feed_counts']['ETHUSDT']['quarantined'],1)
    def test_only_due_candle_timeframes_are_requested(self):
        r=Runtime(self.j);r.state['previous']=300;r.state['contracts']={'BTCUSDT':[rule()]};r.bind()
        for tf,f in r.state['strategies']['BTCUSDT'].frames.items():
            end=300//(tf*60)*(tf*60);f.bars=[bar(end-tf*60,tf=tf)]
        r.update_contracts=lambda *args:None;r.update_eligibility=lambda *args:None
        api=PublicAPI();calls=[]
        def candles(s,tf,*args,**kwargs):calls.append((s,tf,kwargs.get('mark',False)));return [bar()]
        api.candles=candles
        w=Worker(self.tmp.name,('BTCUSDT',),api=api);w.journal=self.j;w.runtime=r
        w.stream.take=lambda *args:([trade()],None)
        with patch('phoenix_v2.forward.time.time',return_value=370):w.cycle()
        self.assertEqual(calls,[('BTCUSDT',1,False),('BTCUSDT',1,True)])
    def test_global_trade_buffer_cap_is_visible_and_quarantined(self):
        stream=TradeStream(SYMBOLS,threading.Event());stream.connected=True;stream.coverage_start=240
        from phoenix_v2.bybit_public import TRADE_BUFFER_LIMIT
        stream.buffered=TRADE_BUFFER_LIMIT;stream.add([trade()],received=301)
        self.assertEqual(stream.overflow,1);self.assertEqual(stream.total,0)
        self.assertEqual(stream.take('BTCUSDT',300)[1],'trade_buffer_overflow')
    def test_http_access_ban_and_throttle_pause_network_retries(self):
        import urllib.error
        for code,wait in ((403,600),(429,60)):
            with self.subTest(code=code):
                api=PublicAPI();began=time.monotonic()
                with patch.object(api.opener,'open',side_effect=urllib.error.HTTPError('https://api.bybit.com',code,'blocked',{},None)) as request:
                    with self.assertRaises(urllib.error.HTTPError):api.get('/v5/market/time')
                    self.assertGreaterEqual(api.cooldown_until,began+wait)
                    with self.assertRaisesRegex(ValueError,'cooling down'):api.get('/v5/market/time')
                    self.assertEqual(request.call_count,1);self.assertEqual(api.errors,1)
    def test_eligibility_thresholds_and_expiration(self):
        instrument=dict(symbol='BTCUSDT',baseCoin='BTC',quoteCoin='USDT',settleCoin='USDT',status='Trading',contractType='LinearPerpetual')
        ticker=dict(symbol='BTCUSDT',turnover24h='5000001')
        book=dict(s='BTCUSDT',ts=300000,b=[['99.99','200']],a=[['100.01','200']])
        self.assertTrue(assess(instrument,ticker,book,300)['liquidity_eligible'])
        ticker['turnover24h']='4999999';self.assertIn('low_24h_turnover',assess(instrument,ticker,book,300)['failures'])
        ticker['turnover24h']='5000001';self.assertIn('stale_orderbook',assess(instrument,ticker,book,400)['failures'])
    def test_paper_policy_blocks_expired_liquidity(self):
        from phoenix_v2.forward import PaperPolicy
        p=PaperPolicy({'BTCUSDT':[rule()]},{'BTCUSDT':dict(available=True,liquidity_eligible=True,observed_at=0,expires_at=300)})
        self.assertEqual(p.rules('BTCUSDT',299)['tick'],D('.01'))
        with self.assertRaises(ValueError):p.rules('BTCUSDT',300)
    def test_liquidity_expiry_cancels_pending_without_stalling_minute(self):
        r=Runtime(self.j);r.state['previous']=300;r.state['contracts']={'BTCUSDT':[rule()]};r.bind()
        a=r.state['arms']['A'];a.marks['BTCUSDT']=D(100);a.mark_times['BTCUSDT']=300;a.reset(300)
        a.reserve(signal(),300,D(100));r.state['pending']=[signal()]
        r.state['enforce_liquidity']=True;r.bind()
        r.minute(360,{'BTCUSDT':[bar()]},{'BTCUSDT':bar()},{'BTCUSDT':([trade()],None)},{})
        self.assertEqual(r.state['previous'],360);self.assertFalse(a.positions);self.assertFalse(a.orders)
    def test_liquidity_expiry_does_not_disable_existing_protection(self):
        r=Runtime(self.j);r.state['previous']=300;r.state['contracts']={'BTCUSDT':[rule()]};r.bind()
        a=r.state['arms']['A'];a.marks['BTCUSDT']=D(100);a.mark_times['BTCUSDT']=300;a.reset(300)
        o=a.reserve(signal(),300,D(100));a.fill_entry('BTCUSDT',300,D('.1'),o.entry,D(1),'SYNTHETIC')
        r.state['strategies']['BTCUSDT'].frames[5].atr.value=D(1)
        r.state['enforce_liquidity']=True;r.bind()
        r.minute(360,{'BTCUSDT':[bar(o='94',h='94',l='94',c='94',v='100')]},
            {'BTCUSDT':bar(o='94',h='94',l='94',c='94')},{'BTCUSDT':([trade(p='94')],None)},{})
        self.assertEqual(r.state['previous'],360);self.assertFalse(a.positions)
        self.assertEqual(self.j.page(table='paper_history')['items'][0]['exit_reason'],'stop')

class HistoryTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.j=Journal(self.tmp.name)
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def state(self):return checkpoint.dumps({})
    def entry(self,i,arm='A'):
        snap=dict(symbol='BTCUSDT',side=1,entry='100',entry_at=i,original_qty='2',remaining_qty='2',
            sl='95',initial_sl='95',tp='120',fees='.11',funding='0',realized_gross='0',multiplier='1')
        return dict(at=i,event='entry_fill',id=arm+':'+str(i),arm=arm,symbol='BTCUSDT',side=1,qty='2',price='100',fee='.11',position_snapshot=snap)
    def test_full_history_pagination_has_no_fifty_record_ceiling(self):
        events=[self.entry(i) for i in range(65)];self.j.commit([],events,self.state())
        seen=[];before=None
        while True:
            p=self.j.page(table='paper_history',before=before,limit=25)
            seen.extend(x['id'] for x in p['items']);before=p['next_cursor']
            if before is None:break
        self.assertEqual(len(seen),65);self.assertEqual(len(set(seen)),65)
        self.assertEqual(len(self.j.page(table='paper_history',arm='B')['items']),0)
    def test_partial_exits_fees_and_signed_funding_are_not_double_counted(self):
        events=[self.entry(1),dict(at=2,event='exit_fill',id='A:1',arm='A',qty='1',price='110',fee='.06',residual='1',reason='tp'),
            dict(at=3,event='funding',id='A:1',arm='A',cashflow='-.02'),
            dict(at=4,event='exit_fill',id='A:1',arm='A',qty='1',price='105',fee='.05',residual='0',reason='stop'),
            dict(at=4,event='position_closed',id='A:1',arm='A',exit_at=4,net='14.76')]
        self.j.commit([],events,self.state());row=self.j.page(table='paper_history')['items'][0]
        self.assertEqual(D(row['fees']),D('.22'));self.assertEqual(D(row['funding']),D('-.02'))
        self.assertEqual(D(row['net_pnl']),D('14.76'));self.assertEqual(D(row['exit_price']),D('107.5'))
        self.assertEqual(row['exit_reason'],'stop');self.assertEqual(len(row['exits']),2)
        self.assertEqual(D(self.j.financials()['A']['fees']),D('.22'))
    def test_unresolved_funding_keeps_net_result_unavailable(self):
        self.j.commit([],[self.entry(1),dict(at=2,event='funding_missing',id='A:1',arm='A',symbol='BTCUSDT')],self.state())
        row=self.j.page(table='paper_history')['items'][0]
        self.assertIsNone(row['net_pnl']);self.assertEqual(row['remaining_size'],'2')
        self.assertEqual(row['state'],'QUARANTINED_OPEN')
    def test_projection_rolls_back_with_event_transaction(self):
        head=self.j.head
        with self.assertRaises(KeyError):self.j.commit([],[self.entry(1),dict(at=2,event='exit_fill',id='A:1')],self.state())
        self.assertEqual(self.j.head,head);self.assertEqual(self.j.page(table='paper_history')['items'],[])
    def test_projection_and_financials_survive_restart(self):
        self.j.commit([],[self.entry(1,'B')],self.state());head=self.j.head
        self.j.close();self.j=Journal(self.tmp.name)
        self.assertEqual(self.j.head,head);self.assertEqual(self.j.page(table='paper_history')['items'][0]['arm'],'B')
        self.assertEqual(D(self.j.financials()['B']['fees']),D('.11'))
    def test_coin_and_arm_filters_include_shared_signal_evidence(self):
        events=[dict(at=1,event='signal',id='s1',signal=dict(symbol='BTCUSDT',side=1)),
            dict(at=2,event='opportunity',id='s1',arm='A',symbol='BTCUSDT',status='no_zone'),
            dict(at=2,event='opportunity',id='s1',arm='B',symbol='BTCUSDT',status='no_zone')]
        self.j.commit([],events,self.state())
        rows=self.j.page(symbol='BTCUSDT',arm='A')['items']
        self.assertEqual(len(rows),2);self.assertFalse(any(r.get('arm')=='B' for r in rows))

if __name__=='__main__':unittest.main()
