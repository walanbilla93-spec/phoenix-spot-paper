"""Run recorder export under populated bounded caches in an isolated process."""
import json,subprocess,sys,unittest

SCENARIO=r'''
import asyncio,gzip,json,resource,tempfile
from pathlib import Path
from aiohttp import web,ClientSession
from recorder import Recorder,app_for
async def main():
 with tempfile.TemporaryDirectory() as td:
  r=Recorder(td)
  for s in r.symbols:
   for i in range(60000):r.filter.exact.ids[s][f'{s}-{i:024d}']=i
   r.filter.buffers[s].add(1,0,b'x'*1999900)
  # 128MiB history traversed through bounded per-member disk spools.
  line=json.dumps({'kind':'public_input','payload':'x'*32700}).encode()+b'\n'
  for i in range(8):
   with gzip.open(Path(td)/f'{i}-{r.disk.boot}-{i:06d}.jsonl.gz','wb',compresslevel=1) as f:
    for j in range(512):f.write(line)
  runner=web.AppRunner(app_for(r,'token'));await runner.setup()
  site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
  url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
  checks=0;bytes_read=0
  async with ClientSession() as client:
   async def export():
    nonlocal bytes_read
    async with client.post(url+'/export.tar',data={'token':'token','scope':'all'}) as response:
     assert response.status==200
     async for chunk in response.content.iter_chunked(65536):bytes_read+=len(chunk)
   task=asyncio.create_task(export())
   while not task.done():
    async with client.get(url+'/health') as response:assert response.status==200;checks+=1
    await asyncio.sleep(.01)
   await task
  peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
  print(json.dumps({'peak_rss_mib':peak,'health_checks_during_export':checks,'download_bytes':bytes_read,'trade_ids':16*60000,'prebuffer_payload_bytes':16*1999900}))
  await runner.cleanup();r.trials.db.close();r.research.db.close()
asyncio.run(main())
'''

class Memory(unittest.TestCase):
    def test_export_under_512_mib_and_health_remains_responsive(self):
        p=subprocess.run([sys.executable,'-c',SCENARIO],capture_output=True,text=True,timeout=60,check=True)
        result=json.loads(p.stdout.strip().splitlines()[-1])
        self.assertLess(result['peak_rss_mib'],512)
        self.assertGreater(result['health_checks_during_export'],1)
        self.assertGreater(result['download_bytes'],0)
        print('Recorder export memory audit:',json.dumps(result,sort_keys=True))

if __name__=='__main__':unittest.main()
