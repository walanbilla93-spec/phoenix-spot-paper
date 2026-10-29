import gzip,io,json,tarfile,tempfile,unittest
from pathlib import Path
from aiohttp import web,ClientSession
from recorder import Recorder,app_for


class PartsTest(unittest.IsolatedAsyncioTestCase):
    async def test_ten_file_parts_preserve_old_files_and_validate_requests(self):
        with tempfile.TemporaryDirectory() as td:
            rec=Recorder(td,symbols=['SOLUSDT'])
            for i in range(12):
                with gzip.open(Path(td)/f'{i:013d}-{rec.disk.boot}-{i:06d}.jsonl.gz','wt') as f:
                    f.write('{"kind":"sample"}\n')
            old=Path(td)/'0000000000000-older-000001.jsonl.gz'
            with gzip.open(old,'wt') as f:f.write('{"kind":"old"}\n')
            runner=web.AppRunner(app_for(rec,'token'));await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/export.tar'
            try:
                async with ClientSession() as c:
                    for part,count,next_part in [(0,10,1),(1,2,None)]:
                        async with c.post(url,data=dict(token='token',scope='current_boot_part',part=str(part))) as r:
                            self.assertEqual(r.status,200);raw=await r.read()
                        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                            manifest=json.load(tar.extractfile('manifest.json'))
                            self.assertEqual(len([f for f in manifest['files'] if f['name'].endswith('.jsonl.gz')]),count)
                            self.assertEqual(manifest['next_part'],next_part)
                            self.assertEqual(manifest['total_scope_files'],12)
                            self.assertNotIn(old.name,tar.getnames())
                    for bad in ('-1','nonsense','2'):
                        async with c.post(url,data=dict(token='token',scope='current_boot_part',part=bad)) as r:
                            self.assertEqual(r.status,400)
                    async with c.post(url,data=dict(token='wrong',scope='current_boot_part')) as r:
                        self.assertEqual(r.status,401)
                self.assertTrue(old.exists())
            finally:await runner.cleanup()

