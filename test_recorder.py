import asyncio,gzip,io,json,resource,tarfile,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp import ClientSession,web
from recorder import Recorder,Disk,Capacity,compact,app_for

class StoreTests(unittest.TestCase):
 def test_disappearing_sqlite_sidecars_do_not_break_writes_or_usage(self):
  with tempfile.TemporaryDirectory() as root:
   d=Disk(root);base=Path(root)/'research.sqlite';base.write_bytes(b'database')
   original=Path.stat
   for suffix in ('-journal','-wal','-shm'):
    sidecar=Path(str(base)+suffix);sidecar.write_bytes(b'temporary')
    def vanishing(path,*a,**kw):
     if path==sidecar:
      sidecar.unlink(missing_ok=True)
      raise FileNotFoundError(str(path))
     return original(path,*a,**kw)
    # is_file was the first check in the old code; force that earlier
    # observation to succeed so the subsequent stat reproduces the race.
    with patch.object(Path,'is_file',return_value=True),patch.object(Path,'stat',vanishing):
     d.write([compact({'kind':'survives-journal-removal'})])
     self.assertEqual(d.usage()['research_bytes'],len(b'database'))
   d.rotate()

 def test_storage_accounting_does_not_hide_real_failures(self):
  with tempfile.TemporaryDirectory() as root:
   d=Disk(root);base=Path(root)/'research.sqlite';base.write_bytes(b'database')
   with patch.object(Path,'stat',side_effect=FileNotFoundError('missing database')):
    with self.assertRaises(FileNotFoundError):d.auxiliary_bytes()
   sidecar=Path(str(base)+'-journal');sidecar.write_bytes(b'journal')
   with patch.object(Path,'stat',side_effect=PermissionError('denied')):
    with self.assertRaises(PermissionError):d.auxiliary_bytes()

 def test_rotation_restart_and_capacity(self):
  with tempfile.TemporaryDirectory() as root:
   d=Disk(root,200000);d.write([compact({'id':1})]);snapshot=d.snapshot();self.assertEqual(len(snapshot),1)
   d2=Disk(root,200000);d2.write([compact({'id':2})]);d2.snapshot();self.assertEqual(d2.count,2)
   rows=[]
   for p in Path(root).glob('*.gz'):
    with gzip.open(p,'rt') as f:rows.extend(json.loads(s) for s in f)
   self.assertEqual(sorted(r['id'] for r in rows),[1,2])
   with self.assertRaises(Capacity):d2.write([b'x'*300000])
   d2.rotate()
 def test_multi_symbol_subscription_and_status(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root)
   self.assertEqual(len(r.symbols),16);self.assertEqual(len(r.topics()),81)
   for symbol in r.symbols:
    self.assertIn('publicTrade.'+symbol,r.topics());self.assertIn('orderbook.1.'+symbol,r.topics())
   self.assertEqual(set(r.status()['per_symbol']),set(r.symbols))
   with self.assertRaises(ValueError):Recorder(root,symbols=['invalid'])
 def test_queue_bound(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,queue_bytes=10);r.enqueue(b'123456')
   with self.assertRaises(Capacity):r.enqueue(b'123456')
   self.assertEqual(r.queued,6);self.assertEqual(r.state['overflow_messages'],1)

class HTTPTests(unittest.IsolatedAsyncioTestCase):
 async def test_export_and_health_while_recording(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root);worker=asyncio.create_task(r.writer());token='test-only-token-not-a-secret'
   runner=web.AppRunner(app_for(r,token));await runner.setup();server=web.TCPSite(runner,'127.0.0.1',0);await server.start();port=server._server.sockets[0].getsockname()[1];base=f'http://127.0.0.1:{port}'
   try:
    r.enqueue(compact({'kind':'raw','payload':{'topic':'publicTrade.SOLUSDT','data':[{'i':'a','S':'Buy','p':'100','v':'2'},{'i':'b','S':'Sell','p':'101','v':'3'}]}}));await r.queue.join()
    async with ClientSession() as c:
     async with c.post(base+'/export.tar',data={'token':'wrong'}) as resp:self.assertEqual(resp.status,401)
     async with c.post(base+'/export.tar',data={'token':token}) as resp:
      self.assertEqual(resp.status,200);self.assertIn('attachment',resp.headers['Content-Disposition']);archive=await resp.read()
     with tarfile.open(fileobj=io.BytesIO(archive)) as t:
      manifest=json.load(t.extractfile('manifest.json'));self.assertEqual(len([f for f in manifest['files'] if f['name'].endswith('.jsonl.gz')]),1)
      payload=gzip.decompress(t.extractfile(manifest['files'][0]['name']).read());self.assertEqual(len(json.loads(payload)['payload']['data']),2)
     r.enqueue(compact({'kind':'after-export'}));await r.queue.join()
     async with c.get(base+'/health') as resp:self.assertEqual(resp.status,200)
     async with c.get(base+'/status') as resp:s=await resp.json();self.assertEqual(s['persisted_messages'],2);self.assertFalse(s['replay_ready'])
   finally:
    worker.cancel()
    try:await worker
    except asyncio.CancelledError:pass
    r.disk.rotate();await runner.cleanup()
 async def test_storage_failure_is_halted(self):
  with tempfile.TemporaryDirectory() as root:
   r=Recorder(root,cap=1);r.enqueue(compact({'x':1}));worker=asyncio.create_task(r.writer());await worker
   self.assertTrue(r.stop.is_set());self.assertEqual(r.state['capture'],'HALTED');self.assertIsNotNone(r.state['last_error']);r.disk.rotate()

if __name__=='__main__':
 result=unittest.main(exit=False,verbosity=2).result
 if not result.wasSuccessful():raise SystemExit(1)
 print('Offline test peak RSS MiB:',round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2))




