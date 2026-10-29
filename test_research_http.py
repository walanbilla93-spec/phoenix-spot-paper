import asyncio
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from aiohttp import ClientSession, web
from recorder import Recorder, app_for, compact
from research_replay import audit
from test_research_engine import ready_plan, quote, S


class ResearchHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_drains_causal_queue_at_one_capture_cutoff(self):
        import gzip
        with tempfile.TemporaryDirectory() as td:
            rec=Recorder(td,symbols=[S]); writer=asyncio.create_task(rec.writer())
            runner=web.AppRunner(app_for(rec,'test'));await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            base=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
            try:
                async with ClientSession() as session:
                    await rec.capture_lock.acquire()
                    async def download():
                        async with session.post(base+'/export.tar',data=dict(token='test',scope='all')) as response:return await response.read()
                    request=asyncio.create_task(download());await asyncio.sleep(.01)
                    self.assertFalse(request.done())
                    async with session.get(base+'/health') as response:self.assertEqual(response.status,200)
                    rec.research._event('research_check',100,S,evidence='atomic-cutoff')
                    await rec.enqueue_selected(rec.research_outbox);rec.research_outbox.clear()
                    rec.capture_lock.release()
                    archive=await request
                    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                        manifest=json.load(tar.extractfile('manifest.json'))
                        self.assertEqual(manifest['research_snapshot']['ledger_events'],1)
                        found=False
                        for member in tar:
                            if member.name.endswith('.jsonl.gz'):
                                found |= b'atomic-cutoff' in gzip.decompress(tar.extractfile(member).read())
                        self.assertTrue(found)
            finally:
                if rec.capture_lock.locked():rec.capture_lock.release()
                writer.cancel()
                try:await writer
                except asyncio.CancelledError:pass
                await runner.cleanup();rec.disk.rotate();rec.research.db.close()

    async def test_dashboard_to_authenticated_archive_and_ledger_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); rec=Recorder(root,symbols=[S])
            rec.research.boundary(1,'HTTP_TEST_BOUNDARY')
            rec.disk.write([compact(dict(kind='boot',receive_ms=1))])
            runner=web.AppRunner(app_for(rec,'private-test-token'))
            await runner.setup(); site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
            base=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
            try:
                async with ClientSession() as session:
                    async with session.get(base) as response:
                        html=await response.text()
                        self.assertIn('name="scope"',html); self.assertIn('value="research" selected',html)
                        self.assertIn('Download research archive',html); self.assertIn('renderResearch(v)',html)
                    async with session.post(base+'/export.tar',data=dict(token='wrong',scope='research')) as response:
                        self.assertEqual(response.status,401)
                    async with session.post(base+'/export.tar',data=dict(token='private-test-token',scope='research')) as response:
                        self.assertEqual(response.status,200); archive=await response.read()
                    async with session.get(base+'/status') as response:
                        status=await response.json(); self.assertIsNone(status['research']['win_rate_pct'])
                    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                        manifest=json.load(tar.extractfile('manifest.json'))
                        self.assertEqual(manifest['export_scope'],'research')
                        self.assertEqual(manifest['research_snapshot']['ledger_events'],1)
                        self.assertEqual([p['name'] for p in manifest['files']],['research-export.sqlite'])
                        snapshot=root/'verified.sqlite'; snapshot.write_bytes(tar.extractfile('research-export.sqlite').read())
                    self.assertTrue(audit(snapshot)['audit_ok'])
                    self.assertEqual(manifest['research_snapshot']['ledger_hash'],audit(snapshot)['ledger_hash'])
                    self.assertEqual(status['storage']['total_bytes'],status['storage']['sealed_bytes']+status['storage']['active_segment_bytes']+status['storage']['research_bytes'])
            finally:
                await runner.cleanup(); rec.disk.rotate(); rec.research.db.close()

    async def test_selective_retention_and_quote_evidence_continue_after_raw_window(self):
        with tempfile.TemporaryDirectory() as td:
            rec=Recorder(td,symbols=[S]); t=180000000
            footprint=dict(kind='footprint_1m',symbol=S,receive_ms=t,minute_ms=t-60000,
                available_ms=t,complete_observation=True,quality_flags=[],buy_volume='1',sell_volume='0',levels=[['100','1','0',1,0]])
            data=compact(dict(kind='raw',symbol=S,receive_ms=t,payload={}))
            retained=rec.process_research(S,{'topic':'publicTrade.'+S},[compact(footprint),data],t,data)
            kinds=[json.loads(r)['kind'] for r in retained]
            self.assertIn('footprint_receipt_1m',kinds); self.assertNotIn('raw',kinds)
            ready_plan(rec.research,t+1000); rec.research.quote(S,quote(103,t+1250),t+1250)
            self.assertIsNotNone(rec.research.position)
            # The old fixed ten-minute raw window is inactive. Active paper
            # campaigns still retain the complete new quote/trade messages.
            self.assertFalse(rec.filter.until[S])
            retained=rec.process_research(S,{'topic':'publicTrade.'+S},[],t+1500,data)
            self.assertIn(data,retained)
            rec.research.db.close()


if __name__=='__main__': unittest.main()
