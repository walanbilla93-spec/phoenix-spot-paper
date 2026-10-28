import unittest,json,tempfile,gzip,asyncio,io,tarfile
from recorder import SetupFilter,Recorder,app_for,compact
from aiohttp import web,ClientSession
class SelectionTests(unittest.TestCase):
 def test_causal_fvg_ob_and_invalidation(self):
  f=SetupFilter(['SOLUSDT'])
  for i in range(20):f.candle('SOLUSDT',{'t':i*60000,'o':100.2,'h':101,'l':99,'c':100,'v':1})
  z=f.candle('SOLUSDT',{'t':1200000,'o':102,'h':104,'l':102,'c':103,'v':1})
  self.assertEqual({x['kind'] for x in z},{'OB','FVG'});self.assertTrue(all(x['available_ms']==1260000 for x in z))
  self.assertEqual(f.candle('SOLUSDT',{'t':1200000,'o':0,'h':0,'l':0,'c':0}),[])
  f.candle('SOLUSDT',{'t':1260000,'o':100,'h':100,'l':90,'c':90,'v':1});self.assertFalse(any(x['side']=='bull' for x in f.zones['SOLUSDT']))
 def test_window_prebuffer_cooldown_gap_and_memory(self):
  f=SetupFilter(['SOLUSDT','XRPUSDT']);s='SOLUSDT';t=2000000
  f.zones[s].append({'kind':'FVG','side':'bull','low':99,'high':100,'formed_ms':t-60000,'available_ms':t})
  def feed(sym,p,ts):return f.process(sym,{'topic':'publicTrade.'+sym,'data':[{'p':str(p),'T':ts,'i':str(ts),'S':'Buy','v':'1'}]},compact({'symbol':sym,'ts':ts}),ts)
  self.assertEqual(feed(s,110,t),[]);self.assertEqual(feed('XRPUSDT',110,t+1),[])
  out=feed(s,100,t+2);self.assertEqual(json.loads(out[0])['kind'],'capture_window_start');self.assertEqual(f.windows,1)
  self.assertTrue(any(json.loads(x).get('ts')==t for x in out));self.assertFalse(any(json.loads(x).get('symbol')=='XRPUSDT' for x in out))
  self.assertTrue(feed(s,100,t+5000));out=feed(s,100,t+600003);self.assertTrue(any(json.loads(x).get('kind')=='capture_window_end' for x in out))
  feed(s,110,t+610000);self.assertFalse(any(json.loads(x).get('kind')=='raw' for x in feed(s,100,t+620000)));self.assertEqual(f.windows,1)
  for i in range(100):f.process('XRPUSDT',{'topic':'orderbook.1.XRPUSDT'},b'x'*100000,t+630000+i)
  self.assertLessEqual(f.buffer_bytes,8000000)
  f.reset_gap();self.assertEqual(f.buffer_bytes,0);self.assertFalse(f.zones[s]);self.assertEqual(f.until[s],0)
 def test_compact_flow_retained_outside_windows(self):
  f=SetupFilter(['SOLUSDT'])
  def trade(t):return f.process('SOLUSDT',{'topic':'publicTrade.SOLUSDT','data':[{'p':'100','T':t,'i':str(t),'S':'Buy','v':'2'}]},b'raw',t)
  self.assertEqual(trade(60000),[])
  out=trade(120000);summary=json.loads(out[0]);self.assertEqual(summary['kind'],'flow_summary_1m');self.assertEqual(sum(x[0] for x in summary['bins'].values()),2)
  self.assertEqual(f.windows,0)
 def test_closed_candle_only(self):
  f=SetupFilter(['SOLUSDT']);p={'topic':'kline.1.SOLUSDT','data':[{'confirm':False}]};self.assertEqual(f.process('SOLUSDT',p,b'raw',100),[])
  p['data']=[{'confirm':True,'start':60000,'open':100,'high':101,'low':99,'close':100,'volume':'2','turnover':'200'}]
  out=f.process('SOLUSDT',p,b'raw',120000);self.assertEqual(json.loads(out[0])['kind'],'candle_1m');self.assertEqual(f.process('SOLUSDT',p,b'raw',120001),[])
class HTTPTests(unittest.IsolatedAsyncioTestCase):
 async def test_prebuffer_burst_uses_backpressure(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,queue_bytes=40000,symbols=['SOLUSDT']);worker=asyncio.create_task(r.writer())
   try:
    await r.enqueue_selected([compact({'kind':'buffered','n':i}) for i in range(10000)])
    await r.queue.join();self.assertEqual(r.state['persisted_messages'],10000);self.assertEqual(r.state['overflow_messages'],0);self.assertEqual(r.queued,0)
   finally:
    worker.cancel()
    try:await worker
    except asyncio.CancelledError:pass
    r.disk.rotate()

 async def test_selected_archive_and_existing_data(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,symbols=['SOLUSDT']);r.disk.write([compact({'kind':'legacy'})]);r.disk.rotate()
   r.filter.zones['SOLUSDT'].append({'kind':'FVG','side':'bull','low':99,'high':100,'formed_ms':0,'available_ms':0})
   out=r.filter.process('SOLUSDT',{'topic':'publicTrade.SOLUSDT','data':[{'p':'100','T':120000,'i':'120000','S':'Buy','v':'1'}]},compact({'kind':'raw','test':True}),120000)
   r.disk.write(out)
   runner=web.AppRunner(app_for(r,'test-token'));await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
   try:
    async with ClientSession() as c:
     async with c.post(f'http://127.0.0.1:{port}/export.tar',data={'token':'test-token'}) as response:
      self.assertEqual(response.status,200);archive=await response.read()
     async with c.get(f'http://127.0.0.1:{port}/status') as response:self.assertEqual((await response.json())['selection']['windows_started'],1)
    with tarfile.open(fileobj=io.BytesIO(archive)) as t:
     kinds=[]
     for member in t:
      if member.name.endswith('.gz'):kinds.extend(json.loads(x)['kind'] for x in gzip.decompress(t.extractfile(member).read()).splitlines())
     self.assertIn('legacy',kinds);self.assertIn('capture_window_start',kinds);self.assertIn('raw',kinds)
   finally:await runner.cleanup();r.disk.rotate()
if __name__=='__main__':unittest.main(verbosity=2)

