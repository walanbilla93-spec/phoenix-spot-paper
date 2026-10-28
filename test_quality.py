import asyncio,gzip,io,json,tarfile,tempfile,unittest,resource
from pathlib import Path
from aiohttp import web,ClientSession
from recorder import CoinBuffer,SetupFilter,Recorder,compact,app_for
class BufferTests(unittest.TestCase):
 def test_noisy_coin_isolation(self):
  f=SetupFilter(['ETHUSDT','SOLUSDT']);f.buffers['ETHUSDT'].add(1,100000,b'quiet')
  for i in range(100):f.buffers['SOLUSDT'].add(i,100000+i,b'x'*100000)
  self.assertEqual(f.buffers['ETHUSDT'].rows[0][2],b'quiet');self.assertFalse(f.buffers['ETHUSDT'].coverage(100100)['capacity_truncated']);self.assertTrue(f.buffers['SOLUSDT'].coverage(100100)['capacity_truncated']);self.assertLessEqual(f.buffers['SOLUSDT'].bytes,2000000)
 def test_age_expiry_not_capacity_loss_and_record_bound(self):
  b=CoinBuffer(max_records=10);b.add(1,100,b'a');self.assertTrue(b.coverage(101)['warming_up']);b.add(2,120101,b'b');self.assertFalse(b.coverage(120101)['capacity_truncated']);self.assertFalse(b.coverage(120101)['warming_up'])
  for i in range(100):b.add(i,120101,b'x')
  self.assertEqual(len(b.rows),10);self.assertTrue(b.coverage(120101)['capacity_truncated'])
 def test_same_millisecond_messages_survive(self):
  f=SetupFilter(['ETHUSDT']);t=2000000
  for i in range(3):f.process('ETHUSDT',{'topic':'orderbook.1.ETHUSDT'},compact({'id':i}),t)
  f.zones['ETHUSDT'].append({'kind':'FVG','side':'bull','low':99,'high':100,'formed_ms':t-60000,'available_ms':t})
  rows=[json.loads(x) for x in f.process('ETHUSDT',{'topic':'publicTrade.ETHUSDT','data':[{'T':t+1,'i':'one','S':'Buy','p':'100','v':'2'}]},compact({'id':3}),t+1)]
  self.assertEqual([r['id'] for r in rows if 'id' in r],[0,1,2,3]);self.assertTrue(rows[0]['prebuffer_warming_up']);self.assertIn('window_id',rows[0])
 def test_delta_count_volume_and_truncation(self):
  f=SetupFilter(['ETHUSDT']);t=100000;f.process('ETHUSDT',{'topic':'publicTrade.ETHUSDT','data':[{'T':t,'S':'Buy','p':'100','v':'3'},{'T':t,'S':'Sell','p':'100','v':'1'}]},b'raw',t)
  v=f.flow_metrics('ETHUSDT',t);self.assertEqual(v['trade_count'],2);self.assertEqual(v['delta_pct'],50)
  for i in range(4001):f.process('ETHUSDT',{'topic':'publicTrade.ETHUSDT','data':[{'T':t,'S':'Buy','p':'100','v':'1'}]},b'raw',t)
  v=f.flow_metrics('ETHUSDT',t);self.assertEqual(v['trade_count'],4000);self.assertTrue(v['capacity_truncated'])
 def test_all_sixteen_reserved_buffers_stay_bounded(self):
  import recorder
  f=SetupFilter(recorder.DEFAULT_SYMBOLS.split(','));self.assertEqual(len(f.buffers),16)
  for s in f.buffers:
   for i in range(1500):f.buffers[s].add(i,100000+i,bytes([i%256])*2000)
  self.assertEqual(sum(b.bytes for b in f.buffers.values()),32000000)
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
 async def test_disconnect_interrupts_window_and_pairs_recovery(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,symbols=['ETHUSDT','SOLUSDT']);worker=asyncio.create_task(r.writer());t=1000000
   r.per_symbol['ETHUSDT']['last_quote_at']=t+200000;r.per_symbol['SOLUSDT']['last_quote_at']=t+199000
   r.filter.zones['ETHUSDT'].append({'kind':'FVG','side':'bull','low':99,'high':100,'formed_ms':t,'available_ms':t});r.filter.process('ETHUSDT',{'topic':'publicTrade.ETHUSDT','data':[{'T':t+200001,'S':'Buy','p':'100','v':'1'}]},b'raw',t+200001)
   await r.mark_gap('old',t,ConnectionError('CLOSE'),t+201000);self.assertEqual(r.pending_gap['since_ms'],t+197000);self.assertFalse(r.filter.until['ETHUSDT'])
   await r.feed_ready('new',t+203000);self.assertIsNone(r.state['last_error']);self.assertIsNotNone(r.state['last_disconnect_error']);r.disk.rotate();rows=[]
   for p in Path(root).glob('*.gz'):
    with gzip.open(p,'rt') as f:rows.extend(json.loads(x) for x in f)
   self.assertFalse(next(x for x in rows if x['kind']=='capture_window_end')['complete_requested_interval']);self.assertEqual(next(x for x in rows if x['kind']=='gap_recovered')['until_ms'],t+203000)
   worker.cancel()
   with self.assertRaises(asyncio.CancelledError):await worker
 async def test_unknown_pipeline_failure_remains_conservative(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,symbols=['ETHUSDT']);worker=asyncio.create_task(r.writer());r.per_symbol['ETHUSDT']['last_quote_at']=9000
   await r.mark_gap('a',1000,ValueError('BAD_PAYLOAD'),10000);gap=r.pending_gap['gap_id'];self.assertEqual(r.pending_gap['since_ms'],1000)
   await r.mark_gap('b',11000,ConnectionError('CLOSE'),12000);self.assertEqual(r.pending_gap['gap_id'],gap);self.assertEqual(r.pending_gap['since_ms'],1000)
   worker.cancel()
   with self.assertRaises(asyncio.CancelledError):await worker
   r.disk.rotate()
 async def test_export_scope_preservation_live_storage_and_readiness(self):
  with tempfile.TemporaryDirectory() as root:
   old=Path(root)/'1000-older-000001.jsonl.gz'
   with gzip.open(old,'wt') as f:f.write('{"kind":"legacy"}\n')
   r=Recorder(root,symbols=['ETHUSDT']);r.disk.write([compact({'kind':'new'})]);self.assertGreater(r.disk.usage()['active_segment_bytes'],0)
   runner=web.AppRunner(app_for(r,'test-token'));await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
   try:
    async with ClientSession() as c:
     async with c.get(f'http://127.0.0.1:{port}/ready') as response:self.assertEqual(response.status,503)
     r.state.update(capture='RECORDING',connected=True)
     async with c.get(f'http://127.0.0.1:{port}/ready') as response:self.assertEqual(response.status,200)
     for scope,count in [('current_boot',1),('all',2)]:
      async with c.post(f'http://127.0.0.1:{port}/export.tar',data={'token':'test-token','scope':scope}) as response:body=await response.read();self.assertEqual(response.status,200)
      with tarfile.open(fileobj=io.BytesIO(body)) as tar:
       m=json.load(tar.extractfile('manifest.json'));self.assertEqual(m['export_scope'],scope);self.assertEqual(len(m['files']),count)
     self.assertTrue(old.exists())
   finally:r.disk.rotate();await runner.cleanup()

