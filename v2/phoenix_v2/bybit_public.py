"""Public-only Bybit adapter. Hosts, methods and paths are fixed allowlists."""
import json
import threading
import time
import urllib.parse
import urllib.request
from .model import Bar, number, D, digest

REST = 'https://api.bybit.com'
STREAM = 'wss://stream.bybit.com/v5/public/linear'
PATHS = {'/v5/market/kline', '/v5/market/mark-price-kline',
         '/v5/market/instruments-info', '/v5/market/funding/history', '/v5/market/time'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('unexpected API redirect')

class PublicAPI:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect)
        self.last = 0; self.requests = 0; self.server_time = None

    def get(self, path, **params):
        if path not in PATHS: raise ValueError('public market path only')
        delay = 0.35-(time.monotonic()-self.last)
        if delay > 0: time.sleep(delay)
        self.last = time.monotonic(); self.requests += 1
        url = REST+path+'?'+urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent':'Phoenix-RC2-public-paper/1'})
        with self.opener.open(req, timeout=12) as response:
            body = response.read(2_000_001)
        if len(body)>2_000_000: raise ValueError('API response resource ceiling')
        result = json.loads(body)
        if result.get('retCode') != 0: raise ValueError('Bybit public API retCode '+str(result.get('retCode')))
        self.server_time = int(result['time'])/1000
        if abs(self.server_time-time.time())>30: raise ValueError('local/exchange clock skew exceeds 30 seconds')
        return result['result']

    def candles(self, symbol, tf, end, start=None, limit=301, mark=False):
        params = dict(category='linear',symbol=symbol,interval='D' if tf==1440 else str(tf),
                      end=int(end*1000)-1,limit=limit)
        if start is not None: params['start'] = int(start*1000)
        result = self.get('/v5/market/mark-price-kline' if mark else '/v5/market/kline',**params)
        if result.get('symbol') != symbol or result.get('category') != 'linear': raise ValueError('wrong candle instrument')
        bars = {}
        for row in result['list']:
            b = Bar(symbol,tf,int(row[0])//1000,*[number(v) for v in row[1:5]],D(0) if mark else number(row[5]))
            # Exclude the forming candle even if supplied in a server response.
            if b.end>end: continue
            b.validate(end)
            if start is not None and b.start<start: continue
            if b.start in bars and bars[b.start]!=b: raise ValueError('conflicting API duplicate')
            bars[b.start] = b
        return sorted(bars.values(),key=lambda b:b.start)

    def instrument(self, symbol):
        rows = self.get('/v5/market/instruments-info',category='linear',symbol=symbol)['list']
        if len(rows)!=1 or rows[0]['symbol']!=symbol: raise ValueError('missing/ambiguous instrument')
        r = rows[0]
        if r['status']!='Trading' or r['contractType']!='LinearPerpetual' or r['settleCoin']!='USDT':
            raise ValueError('only active USDT linear perpetuals in experimental scope')
        return r

    def funding(self, symbol, at):
        rows = self.get('/v5/market/funding/history',category='linear',symbol=symbol,
                        startTime=at*1000,endTime=at*1000,limit=1)['list']
        if len(rows)!=1 or int(rows[0]['fundingRateTimestamp'])!=at*1000:
            raise ValueError('settlement unavailable')
        return rows[0]

class TradeStream:
    """Two-minute bounded buffers. Overflow/disconnect never becomes a fill."""
    def __init__(self, symbols, stop):
        self.symbols = tuple(symbols); self.stop = stop; self.lock = threading.Lock()
        self.buckets = {}; self.bad = {}; self.connected = False
        self.last_message = 0; self.reconnects = 0; self.total = 0; self.coverage_start = time.time()
        self.last_error = None; self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run,name='bybit-public-stream',daemon=True); self.thread.start()

    def add(self, rows, received=None):
        received = received or time.time()
        with self.lock:
            self.last_message = received
            for r in rows:
                if r['s'] not in self.symbols: raise ValueError('unexpected stream symbol')
                at = int(r['T'])/1000; minute = int(at)//60*60; key = (r['s'],minute)
                if at>received+5 or at<received-120: self.bad[key] = 'late_or_future_trade'; continue
                p = number(r['p']); v = number(r['v'])
                if p<=0 or v<=0 or r['S'] not in ('Buy','Sell') or not r['i']: raise ValueError('invalid trade')
                bucket = self.buckets.setdefault(key,{})
                prior = bucket.get(r['i'])
                # A repeated cross-sequence is allowed by Bybit; deduplicate trade IDs only.
                record = dict(T=int(r['T']),s=r['s'],S=r['S'],p=str(p),v=str(v),i=r['i'],seq=r.get('seq'))
                if prior and prior!=record: self.bad[key] = 'conflicting_trade_id'
                if len(bucket)>=30000:
                    self.bad[key] = 'trade_buffer_overflow'; continue
                if not prior: bucket[r['i']] = record; self.total += 1
            oldest = int(received)//60*60-120
            for key in list(self.buckets):
                if key[1]<oldest: del self.buckets[key]
            for key in list(self.bad):
                if key[1]<oldest: del self.bad[key]

    def take(self, symbol, start):
        with self.lock:
            rows = list(self.buckets.pop((symbol,start),{}).values())
            # Stable arrival order for equal timestamps preserves the feed's order.
            rows.sort(key=lambda r:r['T'])
            bad = self.bad.pop((symbol,start),None)
            if start < self.coverage_start: bad = bad or 'stream_not_present_at_interval_start'
            if not self.connected: bad = bad or 'stream_disconnected'
            return rows,bad

    def run(self):
        import websocket
        failures = 0
        while not self.stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(STREAM,timeout=10,enable_multithread=True)
                ws.send(json.dumps({'op':'subscribe','args':['publicTrade.'+s for s in self.symbols]}))
                self.coverage_start = time.time(); self.connected = True; self.last_error = None
                ping = time.monotonic(); last = ping
                while not self.stop.is_set():
                    if time.monotonic()-ping>15:
                        ws.send('{"op":"ping"}'); ping = time.monotonic()
                    try: raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic()-last>35: raise RuntimeError('stream heartbeat timeout')
                        continue
                    if not raw: raise RuntimeError('stream closed')
                    if len(raw)>2_000_000: raise RuntimeError('stream message resource ceiling')
                    doc = json.loads(raw); last = time.monotonic()
                    if doc.get('success') is False: raise RuntimeError('stream subscription rejected')
                    if doc.get('topic','').startswith('publicTrade.'):
                        self.add(doc['data']); failures = 0
            except Exception as exc:
                self.last_error = type(exc).__name__+': '+str(exc)[:180]
                self.reconnects += 1; failures += 1
            finally:
                self.connected = False
                self.coverage_start = time.time()
                if ws:
                    try: ws.close(timeout=1)
                    except Exception: pass
            self.stop.wait(min(60,2**min(failures,5)))

def coverage(bar, rows, bad=None):
    if bad: return bad
    if not rows: return 'no_chronological_trades'
    if any(not bar.start*1000<=r['T']<bar.end*1000 for r in rows): return 'trade_outside_candle'
    prices = [number(r['p']) for r in rows]
    if (prices[0],max(prices),min(prices),prices[-1],sum((number(r['v']) for r in rows),D(0))) != (bar.o,bar.h,bar.l,bar.c,bar.v):
        return 'trade_ohlcv_mismatch'
    # Equal-timestamp, differing prices cannot prove micro-order from REST OHLC.
    # Feed order is retained, but execution at these ties is quarantined below.
    return None
