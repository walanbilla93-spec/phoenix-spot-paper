"""Public-only Bybit adapter. Hosts, methods and paths are fixed allowlists."""
import json
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from .model import Bar, number, D, digest

REST = 'https://api.bybit.com'
STREAM = 'wss://stream.bybit.com/v5/public/linear'
TRADE_BUFFER_LIMIT = 120000
TRADE_BUCKET_LIMIT = 60000
PATHS = {'/v5/market/kline', '/v5/market/mark-price-kline',
         '/v5/market/instruments-info', '/v5/market/funding/history', '/v5/market/time',
         '/v5/market/tickers','/v5/market/orderbook'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('unexpected API redirect')

class PublicAPI:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect)
        self.last = 0; self.requests = 0; self.server_time = None
        self.rate_limits=0;self.errors=0;self.by_path={};self.elapsed=0;self.cooldown_until=0

    def get(self, path, **params):
        if path not in PATHS: raise ValueError('public market path only')
        if time.monotonic()<self.cooldown_until:raise ValueError('public API cooling down after rate limit')
        delay = 0.7-(time.monotonic()-self.last)
        if delay > 0: time.sleep(delay)
        self.last = time.monotonic(); self.requests += 1
        self.by_path[path]=self.by_path.get(path,0)+1
        url = REST+path+'?'+urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent':'Phoenix-RC2-public-paper/1'})
        started=time.monotonic()
        try:
            with self.opener.open(req, timeout=12) as response:body = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            self.errors+=1
            if exc.code in (403,429):
                # Public IP bans require a ten-minute quiet period. Do not
                # repeatedly retry a shared deployment IP during that ban.
                self.cooldown_until=time.monotonic()+(600 if exc.code==403 else 60)
                if exc.code==429:self.rate_limits+=1
            raise
        except Exception:
            self.errors+=1;raise
        finally:self.elapsed+=time.monotonic()-started
        if len(body)>2_000_000: raise ValueError('API response resource ceiling')
        result = json.loads(body)
        if result.get('retCode') != 0:
            self.errors+=1
            if result.get('retCode')==10006:
                self.rate_limits+=1;self.cooldown_until=time.monotonic()+60
            raise ValueError('Bybit public API retCode '+str(result.get('retCode')))
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

    def tickers(self):
        result=self.get('/v5/market/tickers',category='linear')
        if result['category']!='linear':raise ValueError('wrong ticker category')
        return {x['symbol']:x for x in result['list']}

    def orderbook(self,symbol):
        return self.get('/v5/market/orderbook',category='linear',symbol=symbol,limit=50)

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
        self.buffered=0;self.peak_buffered=0;self.overflow=0
        self.received_by_symbol={s:0 for s in symbols}
        self.native={};self.sequence_latest={};self.sequence_regressions=0
        self.sealed_until={};self.late=[];self.late_overflow=0
        self.confirmed_received=0;self.subscription_ack_at=None
        self.evicted_trades=0;self.evicted_native=0;self.late_total=0;self.max_trade_lag_seconds=0

    def start(self):
        self.thread = threading.Thread(target=self.run,name='bybit-public-stream',daemon=True); self.thread.start()

    def add(self, rows, received=None):
        received = received or time.time()
        with self.lock:
            self.last_message = received
            for r in rows:
                if r['s'] not in self.symbols: raise ValueError('unexpected stream symbol')
                at = int(r['T'])/1000; minute = int(at)//60*60; key = (r['s'],minute)
                self.max_trade_lag_seconds=max(self.max_trade_lag_seconds,received-at)
                if minute<self.sealed_until.get(r['s'],0):
                    self.late_total+=1
                    if len(self.late)<1000:self.late.append(dict(trade=dict(r),received_at=received,reason='received_after_minute_sealed'))
                    else:self.late_overflow+=1
                    continue
                if at>received+5 or at<received-120: self.bad[key] = 'late_or_future_trade'; continue
                p = number(r['p']); v = number(r['v'])
                if p<=0 or v<=0 or r['S'] not in ('Buy','Sell') or not r['i']: raise ValueError('invalid trade')
                bucket = self.buckets.setdefault(key,{})
                prior = bucket.get(r['i'])
                # A repeated cross-sequence is allowed by Bybit; deduplicate trade IDs only.
                record = (int(r['T']),r['S'],str(p),str(v),r['i'],r.get('seq'),r.get('BT'),r.get('RPI'),received)
                if prior and prior[:-1]!=record[:-1]: self.bad[key] = 'conflicting_trade_id'
                if not prior and (len(bucket)>=TRADE_BUCKET_LIMIT or self.buffered>=TRADE_BUFFER_LIMIT):
                    self.bad[key] = 'trade_buffer_overflow';self.overflow+=1;continue
                if not prior:
                    seq=r.get('seq');last=self.sequence_latest.get(r['s'])
                    if seq is not None:
                        seq=int(seq)
                        if last is not None and seq<last:
                            self.bad.setdefault(key,'trade_sequence_regression');self.sequence_regressions+=1
                        self.sequence_latest[r['s']]=max(seq,last if last is not None else seq)
                    bucket[r['i']] = record;self.total+=1;self.buffered+=1
                    self.received_by_symbol[r['s']]+=1;self.peak_buffered=max(self.peak_buffered,self.buffered)
            oldest = int(received)//60*60-180
            for key in list(self.buckets):
                if key[1]<oldest:
                    self.evicted_trades+=len(self.buckets[key]);self.buffered-=len(self.buckets[key]);del self.buckets[key]
            for key in list(self.bad):
                if key[1]<oldest: del self.bad[key]
            for key in list(self.native):
                if key[1]<oldest:self.evicted_native+=1;del self.native[key]

    def add_candles(self,topic,rows,received=None):
        received=time.time() if received is None else received
        symbol=topic.split('.',2)[2]
        if symbol not in self.symbols or not topic.startswith('kline.1.'):raise ValueError('unexpected native candle topic')
        with self.lock:
            oldest=int(received)//60*60-180
            for key in list(self.native):
                if key[1]<oldest:self.evicted_native+=1;del self.native[key]
            for row in rows:
                if row.get('confirm') is not True:continue
                start=int(row['start']);end=int(row['end'])
                if start%60000 or end!=start+59999 or end/1000>received or str(row['interval'])!='1':
                    raise ValueError('invalid confirmed candle boundary')
                b=Bar(symbol,1,start//1000,*[number(row[k]) for k in ('open','high','low','close','volume')]);b.validate(received)
                if start//1000<oldest:self.evicted_native+=1;continue
                key=(symbol,start//1000);old=self.native.get(key)
                if old and old['row']!=row:self.bad.setdefault(key,'conflicting_confirmed_candle')
                elif old is None:
                    self.native[key]=dict(symbol=symbol,source_topic=topic,received_at=received,row=dict(row))
                    self.confirmed_received+=1

    def take_native(self,symbol,start):
        with self.lock:return self.native.pop((symbol,start),None)

    def take_late(self):
        with self.lock:
            rows=self.late;self.late=[]
            return rows

    def integrity(self):
        with self.lock:return dict(coverage_start=self.coverage_start,connected=self.connected,
            subscription_ack_at=self.subscription_ack_at,sequence_regressions=self.sequence_regressions,
            confirmed_candles_received=self.confirmed_received,late_records=self.late_total,
            late_overflow=self.late_overflow,evicted_unprocessed_trades=self.evicted_trades,
            evicted_native_candles=self.evicted_native,max_trade_lag_seconds=self.max_trade_lag_seconds)

    def take(self, symbol, start):
        with self.lock:
            raw=list(self.buckets.pop((symbol,start),{}).values());self.buffered-=len(raw)
            rows=[]
            for t,side,p,v,i,seq,bt,rpi,received_at in raw:
                row=dict(T=t,s=symbol,S=side,p=p,v=v,i=i,seq=seq)
                row['received_at']=received_at
                if bt is not None:row['BT']=bt
                if rpi is not None:row['RPI']=rpi
                rows.append(row)
            self.sealed_until[symbol]=max(start+60,self.sealed_until.get(symbol,0))
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
                ws.send(json.dumps({'op':'subscribe','args':[p+s for s in self.symbols for p in ('publicTrade.','kline.1.')]}))
                self.connected=False;self.last_error=None
                self.sequence_latest={}
                ping = time.monotonic(); last = ping
                subscribe_deadline=ping+15
                while not self.stop.is_set():
                    if not self.connected and time.monotonic()>subscribe_deadline:
                        raise RuntimeError('stream subscription acknowledgment timeout')
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
                    if doc.get('op')=='subscribe' and doc.get('success') is True:
                        self.coverage_start=time.time();self.subscription_ack_at=self.coverage_start;self.connected=True
                    if doc.get('topic','').startswith('publicTrade.'):
                        self.add(doc['data']); failures = 0
                    elif doc.get('topic','').startswith('kline.1.'):
                        self.add_candles(doc['topic'],doc['data'])
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

FEED_VERSION='native-confirmed-anchor-3-receipt-evidence'

def reconciliation(bar,rows,bad=None,previous_close=None,confirmed=None):
    result=dict(version=FEED_VERSION,mode=None,reason=bad)
    if bad:return result
    if not rows:return dict(result,reason='no_chronological_trades')
    if any(not bar.start*1000<=r['T']<bar.end*1000 for r in rows):return dict(result,reason='trade_outside_candle')
    prices = [number(r['p']) for r in rows]
    volume=sum((number(r['v']) for r in rows),D(0));observed=(prices[0],max(prices),min(prices),prices[-1],volume)
    native=(bar.o,bar.h,bar.l,bar.c,bar.v)
    result['mismatched_fields']=[k for k,a,b in zip('ohlcv',observed,native) if a!=b]
    if confirmed is not None:
        c=confirmed['row']
        if (confirmed['symbol']!=bar.symbol or c.get('confirm') is not True or
            confirmed['received_at']<bar.end or str(c['interval'])!='1' or
            int(c['start'])!=bar.start*1000 or int(c['end'])!=bar.end*1000-1 or
            tuple(number(c[k]) for k in ('open','high','low','close','volume'))!=native):
            return dict(result,reason='confirmed_native_rest_disagreement')
    if observed==native:return dict(result,mode='first_trade_exact')
    if previous_close is None:return dict(result,reason='trade_ohlcv_mismatch')
    anchor=number(previous_close)
    rebuilt=(anchor,max([anchor]+prices),min([anchor]+prices),prices[-1],volume)
    result['previous_close']=str(anchor)
    result['anchored_ohlcv']=dict(zip('ohlcv',map(str,rebuilt)))
    if rebuilt!=native:return dict(result,reason='trade_ohlcv_mismatch')
    if confirmed is None:return dict(result,reason='native_candle_confirmation_missing')
    return dict(result,mode='previous_close_confirmed')

def coverage(bar,rows,bad=None,previous_close=None,confirmed=None):
    return reconciliation(bar,rows,bad,previous_close,confirmed)['reason']
