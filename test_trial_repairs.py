"""Boundary, migration, execution and every-scope privacy regression tests."""
import copy,gzip,io,json,os,sqlite3,tempfile,tarfile,unittest
from pathlib import Path
from aiohttp import web,ClientSession
from paper_trials import PaperTrials,HASH,LEGACY_HASH,VARIANTS,HOLDOUT_DELAY,MINUTE,FIVE,HOUR,DAY
from recorder import Recorder,app_for
from test_paper_trials import S,T,b,w,q

class Repairs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.emitted=[]
        self.e=PaperTrials(self.root,[S,'XRPUSDT'],T,emit=self.emitted.append)
    def tearDown(self):self.e.db.close();self.tmp.cleanup()
    def signal(self,ts=T+FIVE+5000,s=S,setup='case',warm=True):
        if warm:
            for i in range(80):self.e._bar(self.e.m5[s],b(ts//FIVE*FIVE-(80-i)*FIVE,100,102,98,100))
        self.e.quote(s,q(ts-1),ts-1);watch=w();watch['id']=setup
        trigger=b(ts//FIVE*FIVE-FIVE)
        self.e.signal(s,watch,trigger,{'mss':{'passed':True}},ts)
    def test_clock_starts_on_first_signal_not_initialization_and_survives_restart(self):
        self.assertIsNone(self.e.started);self.signal();start=self.e.started;cutoff=self.e.holdout_start()
        self.assertEqual(start,T+FIVE+5000);self.assertEqual(cutoff,start+HOLDOUT_DELAY)
        self.assertEqual(self.e.clock_path.stat().st_mode&0o222,0)
        self.e.db.close();self.e=PaperTrials(self.root,[S,'XRPUSDT'],T+DAY)
        self.assertEqual(self.e.holdout_start(),cutoff);self.assertEqual(self.e.started,start)
    def test_clock_rejects_update_delete_replace_and_file_tampering(self):
        self.signal()
        for sql in ["UPDATE meta SET v='{}' WHERE k='clock_lock'","DELETE FROM meta WHERE k='clock_lock'","INSERT OR REPLACE INTO meta VALUES('clock_lock','{}')"]:
            with self.assertRaises(sqlite3.IntegrityError):self.e.db.execute(sql)
        os.chmod(self.e.clock_path,0o644);self.e.clock_path.write_text('{}')
        with self.assertRaises(ValueError):self.e.report()
    def test_more_signals_cannot_move_cutoff(self):
        self.signal();cutoff=self.e.holdout_start();self.signal(T+2*FIVE+5000,setup='second')
        self.assertEqual(self.e.holdout_start(),cutoff)
    def test_two_coins_and_five_variants_are_one_time_cluster(self):
        self.signal();self.signal(s='XRPUSDT',setup='other')
        self.assertEqual(self.e.signal_count,2);self.assertEqual(self.e.cluster_count(),1)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM trades').fetchone()[0],10)
    def test_150_signals_in_one_cluster_cannot_seal(self):
        self.signal();self.e.signal_count=150
        self.e.maybe_seal(self.e.started+28*DAY+FIVE)
        self.assertEqual(self.e.phase,'collecting')
    def test_delayed_warmup_merges_missing_past_and_preserves_availability(self):
        live=b(T,step=FIVE);self.e._bar(self.e.m5[S],live)
        payload={'result':{'list':[[T-i*FIVE,100,102,98,100,10] for i in range(1,81)]}}
        received=T+FIVE+10000;self.e.metadata('warmup_5m_'+S,payload,received)
        rows=list(self.e.m5[S]);self.assertEqual(len(rows),81)
        self.assertEqual(rows[0]['available_ms'],received);self.assertEqual(rows[-1],live)
        # New context is usable only after the REST response was observed.
        self.signal(T+2*FIVE+5000,warm=False)
        c=json.loads(self.e.db.execute('SELECT context FROM signals').fetchone()[0])
        self.assertIsNotNone(c['atr_5m']);self.assertIsNotNone(c['volume_ratio_5m'])
    def test_closed_exchange_5m_fills_startup_partial_window(self):
        k=dict(start=T,open=100,high=102,low=98,close=100,volume=10,confirm=False)
        self.e.market_message({'topic':'kline.5.'+S,'data':[k]},T+FIVE)
        self.assertFalse(self.e.m5[S]);k['confirm']=True
        self.e.market_message({'topic':'kline.5.'+S,'data':[k]},T+FIVE+1)
        self.assertEqual(self.e.m5[S][0]['available_ms'],T+FIVE+1)
    def test_missing_context_excludes_only_wide_stop(self):
        self.signal(warm=False)
        c=json.loads(self.e.db.execute('SELECT context FROM signals').fetchone()[0]);self.assertTrue(c['incomplete'])
        statuses=dict(self.e.db.execute('SELECT variant,status FROM trades'))
        self.assertEqual(statuses['wide_stop'],'excluded');self.assertEqual(statuses['baseline'],'open')
        self.assertEqual(statuses['arrival'],'open');self.assertEqual(statuses['limit'],'pending')
    def test_touch_and_one_tick_through_differ_both_directions(self):
        self.signal();p=copy.deepcopy(self.e.active[(1,'limit')]);p.update(entry=101.,reference_entry=101.,stop=99.)
        self.e._prepare(p);strict=copy.deepcopy(p);strict['fill_through_ticks']=1
        self.assertIsNotNone(self.e._limit_path(p,[103,101,102],T))
        self.assertIsNone(self.e._limit_path(strict,[103,101,102],T))
        self.assertIsNotNone(self.e._limit_path(strict,[103,100.99,102],T))
        p.update(side=-1,stop=103.);self.e._prepare(p);strict=copy.deepcopy(p);strict['fill_through_ticks']=1
        self.assertIsNotNone(self.e._limit_path(p,[99,101,100],T))
        self.assertIsNone(self.e._limit_path(strict,[99,101,100],T))
        self.assertIsNotNone(self.e._limit_path(strict,[99,101.01,100],T))
    def test_arrival_uses_receipt_ask_original_stop_and_logs_gap(self):
        self.signal();base=self.e.active[(1,'baseline')];arrival=self.e.active[(1,'arrival')]
        self.assertEqual(arrival['reference_entry'],104.01);self.assertEqual(arrival['stop'],base['stop'])
        self.assertEqual(arrival['entry_ms'],T+FIVE+5000)
        self.assertAlmostEqual(arrival['context']['arrival_minus_close_price'],.01)
    def test_arrival_ignores_preentry_ticks_and_partial_candle_extremes(self):
        self.signal();p=self.e.active[(1,'arrival')];ts=p['entry_ms']
        self.e.arrival_ticks(S,[{'T':ts-1,'p':1},{'T':ts+1,'p':104.}],ts+1)
        self.assertEqual(p['partial_ticks'],1);self.assertFalse(p['states'][0]['closed'])
        minute=ts//MINUTE*MINUTE
        self.e.record(dict(kind='candle_1m',symbol=S,receive_ms=minute+MINUTE+5000,candle=b(minute,104,999,1,104,step=MINUTE)))
        self.assertIn((1,'arrival'),self.e.active);self.assertFalse(p['states'][0]['closed'])
    def test_arrival_postentry_stop_is_recorded_from_ticks(self):
        self.signal();p=self.e.active[(1,'arrival')];ts=p['entry_ms']
        self.e.arrival_ticks(S,[{'T':ts+1,'p':p['stop']-.01}],ts+1)
        self.assertNotIn((1,'arrival'),self.e.active)
        self.assertEqual(self.e.db.execute("SELECT status FROM trades WHERE variant='arrival'").fetchone()[0],'closed')
    def test_holdout_terminal_outcomes_never_enter_any_event_log(self):
        self.signal();cutoff=self.e.holdout_start();self.signal(cutoff,setup='holdout')
        p=self.e.active[(2,'baseline')]
        self.e._finish((2,'baseline'),p,'closed',{'lower':{'net_R':-918273.123},'reason':'SECRET_LOSS'},cutoff+MINUTE)
        self.assertNotIn('SECRET_LOSS',json.dumps(self.emitted));self.assertNotIn('918273',json.dumps(self.emitted))
        self.assertNotIn('SECRET_LOSS',''.join(x[0] for x in self.e.db.execute('SELECT payload FROM events')))
        # Private result remains available to the eventual kill check.
        self.assertIn('SECRET_LOSS',self.e.db.execute("SELECT result FROM trades WHERE signal=2 AND variant='baseline'").fetchone()[0])
        self.assertEqual(self.e.privacy_audit()['exposed_protected_outcomes'],0)
    def test_r1_migration_keeps_history_and_does_not_invent_new_variants(self):
        self.e.db.close();db=sqlite3.connect(self.root/'research_trials.sqlite')
        db.execute("UPDATE meta SET v=? WHERE k='protocol_hash'",(json.dumps(LEGACY_HASH),))
        db.commit();db.close();self.e=PaperTrials(self.root,[S],T+100)
        self.assertEqual(self.e.meta('previous_protocol_hash'),LEGACY_HASH)
        self.assertEqual(self.e.meta('protocol_hash'),HASH)
    def test_no_public_final_report_before_kill(self):
        self.signal();self.e.boundary(T+HOUR,'test');self.e.phase='sealed'
        self.assertEqual(self.e.report(True)['reason'],'KILL_CHECK_NOT_RUN')
    def test_150_clusters_seal_and_kill_before_releasing_outcomes(self):
        self.signal();start=self.e.started;self.e.boundary(start+MINUTE,'test')
        context=json.loads(self.e.db.execute('SELECT context FROM signals LIMIT 1').fetchone()[0])
        # A fixed time cutoff, not 70% of these deliberately uneven arrivals.
        for i in range(1,151):
            at=start+20*DAY+i*FIVE;bucket=at//FIVE*FIVE
            cur=self.e.db.execute('INSERT INTO signals(setup,s,side,at,bucket,context,plan) VALUES(?,?,?,?,?,?,?)',(str(i),S,1,at,bucket,json.dumps(context),'{}'))
            result={k:dict(net_R=-1.,gross_R=-.5,cost_R=.5,fees_R=.2,funding_R=.1,execution_R=.2,exit_ms=at+MINUTE) for k in ('lower','upper')}
            for v in VARIANTS:self.e.db.execute('INSERT INTO trades VALUES(?,?,?,?,?)',(cur.lastrowid,v,'closed',json.dumps({'entry':100.,'distance':1.}),json.dumps(result)))
        self.e.signal_count=151;self.e.db.commit();self.e.maybe_seal(start+28*DAY)
        self.assertEqual(self.e.phase,'complete');self.assertEqual(self.e.meta('kill_checked_ms'),start+28*DAY)
        report=self.e.report(True);self.assertEqual(report['test_start_ms'],start+HOLDOUT_DELAY)
        self.assertEqual(report['deduplicated_signals'],150);self.assertTrue(all(v=='STOP' for v in report['kill'].values()))
        self.assertIn('limit_through',report['metrics']);self.assertEqual(report['metrics']['limit']['all']['all']['fill_rate'],1.)
        self.assertEqual(report['metrics']['limit_through']['all']['all']['fill_rate'],1.)
    def test_populated_r1_migration_preserves_three_original_plans_and_clock(self):
        self.signal();original=self.e.db.execute("SELECT payload FROM trades WHERE variant='baseline'").fetchone()[0]
        first=self.e.started;self.e.db.close();db=sqlite3.connect(self.root/'research_trials.sqlite')
        for name in ('clock_no_update','clock_no_delete','clock_no_replace'):db.execute('DROP TRIGGER '+name)
        db.execute("DELETE FROM meta WHERE k='clock_lock'");db.execute("DELETE FROM trades WHERE variant IN ('arrival','limit_through')")
        db.execute("UPDATE meta SET v=? WHERE k='protocol_hash'",(json.dumps(LEGACY_HASH),));db.commit();db.close()
        self.e.clock_path.unlink();self.e=PaperTrials(self.root,[S],first+MINUTE)
        self.assertEqual(self.e.started,first);self.assertEqual(self.e.db.execute('SELECT count(*) FROM trades').fetchone()[0],3)
        self.assertEqual(self.e.db.execute("SELECT payload FROM trades WHERE variant='baseline'").fetchone()[0],original)
        self.assertEqual(self.e.meta('amendment_first_signal_id'),2)

class ArchivePrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_every_archive_scope_and_http_report_hides_injected_legacy_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            rec=Recorder(td,symbols=[S]);rec.trials._lock_clock(T)
            # Unknown IDs must fail closed too, including old log members.
            secret={'kind':'trial_trade_terminal','receive_ms':T+HOLDOUT_DELAY,'signal_id':999,'variant':'baseline','status':'closed','result':{'net_R':-918273.123,'reason':'SECRET_LOSS'}}
            with gzip.open(Path(td)/f'000-{rec.disk.boot}-000001.jsonl.gz','wt') as f:f.write(json.dumps(secret)+'\n')
            runner=web.AppRunner(app_for(rec,'token'));await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
            try:
                async with ClientSession() as client:
                    for scope in ('all','current_boot','current_boot_part','research'):
                        async with client.post(url+'/export.tar',data=dict(token='token',scope=scope,part='0')) as response:
                            self.assertEqual(response.status,200);raw=await response.read()
                        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                            self.assertNotIn('research_trials_export.sqlite',tar.getnames())
                            for member in tar:
                                data=tar.extractfile(member).read()
                                if member.name.endswith('.gz'):data=gzip.decompress(data)
                                self.assertNotIn(b'SECRET_LOSS',data);self.assertNotIn(b'918273.123',data)
                    for endpoint in ('/status','/paper-variants/report'):
                        async with client.get(url+endpoint) as response:
                            data=await response.read();self.assertNotIn(b'SECRET_LOSS',data);self.assertNotIn(b'918273.123',data)
                    async with client.post(url+'/paper-variants/final',data={'token':'token'}) as response:self.assertEqual(response.status,409)
            finally:
                await runner.cleanup();rec.trials.db.close();rec.research.db.close()

if __name__=='__main__':unittest.main()
