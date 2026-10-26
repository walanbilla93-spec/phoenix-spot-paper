"""Read-only Bybit recorder. No private endpoints, keys, orders, or strategy execution."""
from collections import deque
import math
import asyncio, contextlib, gzip, hashlib, json, os, secrets, tarfile, time, uuid, signal, threading
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

VERSION='market-recorder-0.3-selective'
DEFAULT_SYMBOLS='SOLUSDT,XRPUSDT,DOGEUSDT,AVAXUSDT,SUIUSDT,SEIUSDT,NEARUSDT,INJUSDT,APTUSDT,ARBUSDT,OPUSDT,WIFUSDT,1000PEPEUSDT,TNSRUSDT,PIPPINUSDT'
def now(): return time.time_ns()//1000000
def compact(v): return json.dumps(v,separators=(',',':')).encode()+b'\n'

class Capacity(Exception): pass
def synchronized(fn):
    def run(self,*a,**kw):
        with self.lock:return fn(self,*a,**kw)
    return run

class Disk:
    def __init__(self, root, cap=2_000_000_000, max_files=10000):
        self.lock=threading.RLock()
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.cap=cap; self.max_files=max_files; self.f=None; self.raw=0; self.opened=0
        self.used=sum(p.stat().st_size for p in self.root.glob('*.jsonl.gz'))
        self.count=sum(1 for _ in self.root.glob('*.jsonl.gz'))
        self.boot=uuid.uuid4().hex; self.seq=0
    @synchronized
    def rotate(self):
        if self.f:
            self.f.close(); self.used+=self.path.stat().st_size;self.f=None
        self.raw=0
    @synchronized
    def write(self, records):
        if not self.f or self.raw>=16_000_000 or time.monotonic()-self.opened>=300:
            self.rotate()
            if self.count>=self.max_files:raise Capacity('FILE_LIMIT')
            self.seq+=1;self.path=self.root/f'{now()}-{self.boot}-{self.seq:06d}.jsonl.gz'
            self.f=gzip.open(self.path,'xb',compresslevel=1);self.count+=1;self.opened=time.monotonic()
        for data in records:
            # Reserve worst-case gzip overhead before writing; never silently delete old evidence.
            current=self.path.stat().st_size
            if self.used+current+len(data)+65536>self.cap:raise Capacity('STORAGE_LIMIT')
            self.f.write(data); self.raw+=len(data)
        self.f.flush()
    @synchronized
    def snapshot(self):
        self.rotate()
        # Bounded by the explicit 10,000-file retention cap. Payload bytes never accumulate.
        paths=sorted(self.root.glob('*.jsonl.gz'))
        if len(paths)>self.max_files:raise Capacity('FILE_LIMIT')
        return [(p,p.stat().st_size) for p in paths]

class SetupFilter:
    """Provisional causal POIs, not validated trader entries. All limits are global/bounded."""
    def __init__(self,symbols):
        self.bars={s:deque(maxlen=32) for s in symbols};self.zones={s:deque(maxlen=8) for s in symbols}
        self.buffer=deque();self.buffer_bytes=0;self.evicted_at=0
        self.until={s:0 for s in symbols};self.cooldown={s:0 for s in symbols};self.near={s:False for s in symbols}
        self.flow={s:None for s in symbols}
        self.windows=0;self.received=0;self.saved=0;self.last_saved={s:0 for s in symbols}
    def candle(self,s,c):
        bars=self.bars[s]
        if bars and c['t']<=bars[-1]['t']:return []
        self.zones[s]=deque((z for z in self.zones[s] if c['t']-z['formed_ms']<86400000 and not (c['c']<z['low'] if z['side']=='bull' else c['c']>z['high'])),maxlen=8)
        created=[]
        if len(bars)>=2:
            a=bars[-2]
            if c['l']>a['h']*1.0005:created.append(('FVG','bull',a['h'],c['l']))
            if c['h']<a['l']*.9995:created.append(('FVG','bear',c['h'],a['l']))
        if len(bars)>=20:
            recent=list(bars)[-20:];side='bull' if c['c']>max(b['h'] for b in recent) else 'bear' if c['c']<min(b['l'] for b in recent) else None
            if side and abs(c['c']-c['o'])>0.001*c['c']:
                opposite=next((b for b in reversed(recent[-5:]) if (b['c']<b['o'] if side=='bull' else b['c']>b['o'])),None)
                if opposite:created.append(('OB',side,opposite['l'],opposite['h']))
        bars.append(c)
        out=[]
        for kind,side,lo,hi in created:
            z={'kind':kind,'side':side,'low':lo,'high':hi,'formed_ms':c['t'],'available_ms':c['t']+60000,'timeframe':'1m','definition':'PROVISIONAL_V1'}
            self.zones[s].append(z);out.append(z)
        return out
    def process(self,s,payload,data,ts):
        out=[];topic=payload.get('topic','');self.received+=1
        while self.buffer and (ts-self.buffer[0][0]>120000 or self.buffer_bytes+len(data)>8000000):
            t,_,d=self.buffer.popleft();self.buffer_bytes-=len(d);self.evicted_at=max(self.evicted_at,t)
        if ts>self.until[s]:
            if self.until[s]:out.append(compact({'kind':'capture_window_end','symbol':s,'receive_ms':ts,'end_ms':self.until[s],'reason':'FIXED_10_MINUTE_LIMIT'}));self.until[s]=0
        price=None
        if topic.startswith('publicTrade.') and payload.get('data'):
            price=float(payload['data'][-1]['p'])
            for trade in payload['data']:
                minute=int(trade['T'])//60000*60000;f=self.flow[s]
                if f and minute<f['minute_ms']:continue
                if f and minute>f['minute_ms']:
                    out.append(compact({'kind':'flow_summary_1m','symbol':s,'receive_ms':ts,**f}));f=None
                if f is None:
                    f={'minute_ms':minute,'log_price_step':0.0005,'bins':{},'overflow_buy':0.,'overflow_sell':0.,'approximate':True};self.flow[s]=f
                bucket=str(math.floor(math.log(float(trade['p']))/0.0005));side=0 if trade['S']=='Buy' else 1;volume=float(trade['v'])
                if bucket not in f['bins'] and len(f['bins'])>=256:f['overflow_buy' if side==0 else 'overflow_sell']+=volume
                else:
                    if bucket not in f['bins']:f['bins'][bucket]=[0.,0.]
                    f['bins'][bucket][side]+=volume
        if topic.startswith('kline.'):
            for k in payload.get('data',[]):
                if k.get('confirm'):
                    c={'t':int(k['start']),'o':float(k['open']),'h':float(k['high']),'l':float(k['low']),'c':float(k['close']),'v':k['volume'],'turnover':k['turnover']}
                    if self.bars[s] and c['t']<=self.bars[s][-1]['t']:continue
                    for z in self.candle(s,c):out.append(compact({'kind':'candidate_zone','symbol':s,'receive_ms':ts,'zone':z}))
                    out.append(compact({'kind':'candle_1m','symbol':s,'receive_ms':ts,'candle':c}));price=c['c']
        if price:
            matches=[z for z in self.zones[s] if z['available_ms']<=ts and ts-z['formed_ms']<86400000 and z['low']*.999<=price<=z['high']*1.001]
            approaching=bool(matches) and not self.near[s];self.near[s]=bool(matches)
            if approaching and ts>=self.cooldown[s] and not self.until[s]:
                self.windows+=1;self.until[s]=ts+600000;self.cooldown[s]=ts+1800000
                out.append(compact({'kind':'capture_window_start','symbol':s,'receive_ms':ts,'end_ms':self.until[s],'zones':matches,'policy':'PROVISIONAL_POI_NOT_ENTRY','prebuffer_requested_ms':120000,'prebuffer_globally_truncated':self.evicted_at>=ts-120000,'previous_saved_ms':self.last_saved[s]}))
                for t,sym,d in self.buffer:
                    if sym==s and t>self.last_saved[s]:out.append(d);self.last_saved[s]=t
        if self.until[s] and topic.startswith(('publicTrade.','orderbook.1.')):
            out.append(data);self.last_saved[s]=ts;self.saved+=1
        if topic.startswith(('publicTrade.','orderbook.1.')) and len(data)<=8000000:
            self.buffer.append((ts,s,data));self.buffer_bytes+=len(data)
        return out
    def reset_gap(self):
        self.buffer.clear();self.buffer_bytes=0
        for s in self.bars:self.flow[s]=None;self.bars[s].clear();self.zones[s].clear();self.until[s]=0;self.near[s]=False
    def status(self):
        return {'mode':'SELECTIVE_PROVISIONAL_POI','context_flow':'minute buy/sell volume in approx 5bp log-price bins, max 256 bins; late previous-minute trades omitted; gaps discard partial minute','timeframe':'1m','zone_limit_per_coin':8,'zone_expiry_hours':24,'proximity_bps':10,'prebuffer_seconds':120,'prebuffer_global_cap_bytes':8000000,'prebuffer_bytes':self.buffer_bytes,'detail_window_minutes':10,'minimum_trigger_spacing_minutes':30,'windows_started':self.windows,'active_windows':{s:t for s,t in self.until.items() if t>now()},'raw_messages_considered':self.received,'raw_live_messages_saved':self.saved,'zones':{s:list(z) for s,z in self.zones.items()},'note':'Candles continuous; raw flow selective. Elliott/inducement/MSS not automatically confirmed. Gaps invalidate continuity; omitted flow cannot reconstruct full session footprint or SVP.'}

class Recorder:
    def __init__(self,root,cap=2_000_000_000,queue_bytes=8_000_000,symbols=None):
        self.symbols=tuple(dict.fromkeys(symbols or DEFAULT_SYMBOLS.split(',')))
        if not 1<=len(self.symbols)<=15 or any(not v.isalnum() or not v.endswith('USDT') or v!=v.upper() for v in self.symbols):raise ValueError('Use 1 to 15 uppercase USDT symbols')
        self.per_symbol={s:{'last_quote_at':None,'last_trade_at':None,'last_candle_at':None,'messages':0} for s in self.symbols}
        self.filter=SetupFilter(self.symbols)
        self.disk=Disk(root,cap); self.queue=asyncio.Queue(maxsize=1024); self.limit=queue_bytes;self.queued=0
        self.state={'version':VERSION,'boot_id':self.disk.boot,'symbols':self.symbols,'capture':'STARTING','connected':False,'received_messages':0,'persisted_messages':0,'overflow_messages':0,'reconnects':0,'last_quote_at':None,'last_trade_at':None,'last_error':None,'gap_count':0}
        self.store_lock=asyncio.Lock();self.export_lock=asyncio.Lock();self.stop=asyncio.Event()
    def topics(self):
        return [f'{kind}.{s}' for s in self.symbols for kind in ('publicTrade','orderbook.1','kline.1','tickers')]
    def enqueue(self,data):
        if self.queued+len(data)>self.limit or self.queue.full():
            self.state['overflow_messages']+=1;raise Capacity('QUEUE_OVERFLOW')
        self.queue.put_nowait(data);self.queued+=len(data)
    async def event(self,kind,**fields):
        data=compact({'kind':kind,'receive_ms':now(),'boot_id':self.disk.boot,**fields})
        async with self.store_lock:await asyncio.to_thread(self.disk.write,[data])
    async def writer(self):
        while True:
            data=await self.queue.get();batch=[data]
            while len(batch)<32 and not self.queue.empty():batch.append(self.queue.get_nowait())
            try:
                async with self.store_lock:await asyncio.to_thread(self.disk.write,batch)
                self.state['persisted_messages']+=len(batch)
            except Exception as e:
                self.state.update(capture='HALTED',last_error=type(e).__name__+': '+str(e));self.stop.set()
            finally:
                self.queued-=sum(map(len,batch))
                for _ in batch:self.queue.task_done()
            if self.stop.is_set():
                while not self.queue.empty():
                    data=self.queue.get_nowait();self.queued-=len(data);self.queue.task_done();self.state['overflow_messages']+=1
                return
    async def warmup(self,session):
        # Public inputs only. Failure remains explicit and cannot make an interval replay-ready.
        reqs=[(label+'_'+symbol,path.replace('SOLUSDT',symbol)) for symbol in self.symbols for label,path in [('instrument','/v5/market/instruments-info?category=linear&symbol=SOLUSDT'),('warmup_1h','/v5/market/kline?category=linear&symbol=SOLUSDT&interval=60&limit=200'),('funding','/v5/market/funding/history?category=linear&symbol=SOLUSDT&limit=10')]]
        for label,path in reqs:
            try:
                async with session.get('https://api.bybit.com'+path,timeout=ClientTimeout(total=15)) as r:
                    payload=await r.json()
                    if r.status!=200 or payload.get('retCode')!=0:raise ValueError('PUBLIC_METADATA_FAILED')
                    await self.event('public_metadata',name=label,payload=payload)
            except Exception as e:await self.event('metadata_unavailable',name=label,error=type(e).__name__)
    async def capture(self):
        await self.event('boot',selection=self.filter.status(),version=VERSION,raw_feed='Bybit linear public',quality='NOT_YET_REPLAY_READY',note='Startup/restart is a gap. Sequence jumps alone cannot prove missing public trades.')
        async with ClientSession(timeout=ClientTimeout(total=None)) as session:
            metadata=asyncio.create_task(self.warmup(session));backoff=1
            try:
                while not self.stop.is_set():
                    connection=uuid.uuid4().hex;started=now()
                    try:
                        async with session.ws_connect('wss://stream.bybit.com/v5/public/linear',heartbeat=20,receive_timeout=35,max_msg_size=2_000_000,timeout=15) as ws:
                            await ws.send_json({'op':'subscribe','args':self.topics()})
                            self.state.update(connected=True,capture='SUBSCRIBING');seen={s:set() for s in self.symbols};last_ping=time.monotonic();last_quote={s:time.monotonic() for s in self.symbols}
                            await self.event('connection_open',connection=connection)
                            while not self.stop.is_set():
                                try:msg=await asyncio.wait_for(ws.receive(),timeout=1)
                                except asyncio.TimeoutError:msg=None
                                if time.monotonic()-last_ping>=20:await ws.send_json({'op':'ping'});last_ping=time.monotonic()
                                if any(time.monotonic()-v>10 for v in last_quote.values()):raise TimeoutError('QUOTE_STREAM_STALE')
                                if msg is None:continue
                                if msg.type in (WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR):raise ConnectionError('WEBSOCKET_CLOSED')
                                if msg.type!=WSMsgType.TEXT:continue
                                received=now();payload=json.loads(msg.data)
                                if payload.get('op')=='subscribe' and payload.get('success') is not True:raise ValueError('SUBSCRIPTION_REJECTED')
                                topic=payload.get('topic','');symbol=topic.rsplit('.',1)[-1]
                                if topic and symbol not in seen:raise ValueError('UNEXPECTED_SYMBOL')
                                if topic:self.per_symbol[symbol]['messages']+=1
                                if topic.startswith('orderbook.1.'):
                                    if payload.get('type')!='snapshot':raise ValueError('UNEXPECTED_L1_DELTA')
                                    last_quote[symbol]=time.monotonic();self.state['last_quote_at']=received;self.per_symbol[symbol]['last_quote_at']=received;seen[symbol].add('quotes')
                                if topic.startswith('publicTrade.'):
                                    self.state['last_trade_at']=received;self.per_symbol[symbol]['last_trade_at']=received;seen[symbol].add('trades')
                                if topic.startswith('kline.'):
                                    self.per_symbol[symbol]['last_candle_at']=received;seen[symbol].add('candles')
                                # Raw message is retained, including every member of a multi-trade batch,
                                # exchange timestamps, seq, trade IDs, RPI/block flags and ticker deltas.
                                prefix=json.dumps({'kind':'raw','receive_ms':received,'receive_monotonic_ns':time.monotonic_ns(),'boot_id':self.disk.boot,'connection':connection},separators=(',',':'))[:-1]
                                data=(prefix+',"payload":'+msg.data+'}\n').encode()
                                for selected in self.filter.process(symbol,payload,data,received) if topic else []:self.enqueue(selected)
                                self.state['received_messages']+=1
                                if all(v=={'quotes','trades','candles'} for v in seen.values()):
                                    self.state['capture']='RECORDING';backoff=1
                    except asyncio.CancelledError:raise
                    except Exception as e:
                        self.filter.reset_gap()
                        self.state.update(connected=False,capture='GAP',last_error=type(e).__name__+': '+str(e));self.state['gap_count']+=1
                        await self.queue.join()
                        if not self.stop.is_set():await self.event('gap',connection=connection,since_ms=started,until_ms=now(),error=self.state['last_error'],replay_policy='CONSERVATIVELY_INVALIDATE_CONNECTION_INTERVAL')
                        self.state['reconnects']+=1
                        try:await asyncio.wait_for(self.stop.wait(),timeout=backoff)
                        except asyncio.TimeoutError:pass
                        backoff=min(backoff*2,60)
            finally:
                metadata.cancel()
                with contextlib.suppress(asyncio.CancelledError):await metadata
    def status(self):
        s=dict(self.state);s['selection']=self.filter.status();s['per_symbol']={k:dict(v) for k,v in self.per_symbol.items()};s.update(queued_bytes=self.queued,queue_byte_limit=self.limit,sealed_bytes=self.disk.used,storage_limit=self.disk.cap,replay_ready=False,replay_note='Must validate complete UTC session, metadata, timestamps and gap-free segments offline. Raw trades are not automatically deduplicated. L1 quotes are not depth/queue-fill evidence.')
        return s

PAGE='''<!doctype html><meta charset="utf-8"><title>Market Data Recorder</title><style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:20px}pre{background:#eee;padding:20px;white-space:pre-wrap}input,button{padding:12px}</style><h1>Market Data Recorder</h1><p>Selective public Bybit capture. Continuous 1-minute candles; detailed flow near provisional OB/FVG zones. No trading or AI calls.</p><pre id="s">Loading status…</pre><form method="post" action="/export.tar"><label>Export token <input type="password" name="token" required autocomplete="off"></label> <button>Download sealed data archive</button></form><p>Download streams from disk. Disconnects, restarts and overflow are recorded as quality problems; recording does not automatically mean replay-ready.</p><script>async function update(){try{s.textContent=JSON.stringify(await(await fetch('/status')).json(),null,2)}catch(e){s.textContent='Status unavailable'}}update();setInterval(update,10000)</script>'''

def app_for(rec,token):
    app=web.Application(client_max_size=4096)
    async def home(req):return web.Response(text=PAGE,content_type='text/html')
    async def health(req):return web.json_response({'alive':True})
    async def status(req):return web.json_response(rec.status())
    async def export(req):
        form=await req.post()
        if not token or not secrets.compare_digest(str(form.get('token','')),token):raise web.HTTPUnauthorized()
        if rec.export_lock.locked():raise web.HTTPTooManyRequests(text='One export at a time')
        async with rec.export_lock:
            async with rec.store_lock:files=await asyncio.to_thread(rec.disk.snapshot)
            response=web.StreamResponse(headers={'Content-Type':'application/x-tar','Content-Disposition':'attachment; filename="market-data.tar"','Cache-Control':'no-store'})
            await response.prepare(req)
            manifest=compact({'version':VERSION,'export_ms':now(),'quality':rec.status(),'files':[{'name':p.name,'bytes':n} for p,n in files],'checksum_policy':'gzip CRC32 per member; verify all members offline','archive_payload':'mixed legacy raw and selective POI windows; consult boot version and selection metadata'})
            async def header(name,size):
                info=tarfile.TarInfo(name);info.size=size;info.mtime=int(time.time());await response.write(info.tobuf(format=tarfile.USTAR_FORMAT))
            await header('manifest.json',len(manifest));await response.write(manifest);await response.write(b'\0'*((-len(manifest))%512))
            for path,size in files:
                await header(path.name,size)
                with path.open('rb') as f:
                    remaining=size
                    while remaining:
                        chunk=await asyncio.to_thread(f.read,min(65536,remaining))
                        if not chunk:raise IOError('Export member unexpectedly truncated')
                        await response.write(chunk);remaining-=len(chunk)
                await response.write(b'\0'*((-size)%512))
            await response.write(b'\0'*1024);await response.write_eof();return response
    app.router.add_get('/',home);app.router.add_get('/health',health);app.router.add_get('/status',status);app.router.add_post('/export.tar',export)
    return app

async def main():
    if os.environ.get('REQUIRE_PERSISTENT_MOUNT','true')=='true' and not os.path.ismount('/data'):
        raise RuntimeError('Persistent /data mount required; refusing ephemeral capture')
    rec=Recorder(os.environ.get('DATA_DIR','/data/sol-recorder'),int(os.environ.get('MAX_STORAGE_BYTES','2000000000')),symbols=os.environ.get('SYMBOLS',DEFAULT_SYMBOLS).split(','))
    token=os.environ.get('EXPORT_TOKEN','')
    if len(token)<24:raise RuntimeError('Set EXPORT_TOKEN to at least 24 characters; never print it in logs')
    runner=web.AppRunner(app_for(rec,token));await runner.setup();await web.TCPSite(runner,'0.0.0.0',int(os.environ.get('PORT','8080'))).start()
    writer=asyncio.create_task(rec.writer());capture=asyncio.create_task(rec.capture())
    shutdown=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,shutdown.set)
    stopping=asyncio.create_task(shutdown.wait())
    try:
        done,_=await asyncio.wait([writer,capture,stopping],return_when=asyncio.FIRST_COMPLETED)
        if stopping in done:return
        for task in done:
            if not task.cancelled() and task.exception():rec.state.update(capture='HALTED',last_error=type(task.exception()).__name__)
        rec.stop.set();capture.cancel()
        with contextlib.suppress(asyncio.CancelledError):await capture
        # Keep status/export alive after failure; do not restart-loop or conceal data loss.
        rec.state['capture']='HALTED';rec.state['connected']=False
        await shutdown.wait()
    finally:
        rec.stop.set();capture.cancel()
        with contextlib.suppress(asyncio.CancelledError):await capture
        if not writer.done():await rec.queue.join()
        writer.cancel();stopping.cancel()
        with contextlib.suppress(asyncio.CancelledError):await writer
        await runner.cleanup();rec.disk.rotate()

if __name__=='__main__':asyncio.run(main())

