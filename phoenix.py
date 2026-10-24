"""PHOENIX SPOT: deterministic PAPER_ONLY engine. No private exchange interfaces."""
import copy
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

VERSION = 'phoenix-paper-1'
SYMBOLS = ('SOLUSDT', 'AVAXUSDT', 'ALGOUSDT', 'HBARUSDT', 'XMRUSDT')
# Original dated top-25 research cohort: 22 acquired pairs and 3 unresolved names.
RESEARCH_COHORT = tuple(sorted(('ADAUSDT','ATOMUSDT','AVAXUSDT','BCHUSDT','BNBUSDT','BTCUSDT',
    'DOGEUSDT','DOTUSDT','ETHUSDT','FILUSDT','ICPUSDT','LINKUSDT','LTCUSDT','MATICUSDT',
    'NEARUSDT','OPUSDT','SHIBUSDT','SOLUSDT','TRXUSDT','UNIUSDT','XLMUSDT','XRPUSDT',
    'TONUSDT','LEOUSDT','OKBUSDT')))
LADDER = (7,) * 8 + (10,) * 3 + (16,) * 4
FEE = .001  # frozen assumed base fee; not account-specific
MAX_AGE = 2000
MAX_SLIP = .0015  # safety execution bound vs best quote, not optimized
MINUTE = 60000
HOUR = 3600000

def now_ms():
    return time.time_ns() // 1000000

def floor(value, step):
    return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))

def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), allow_nan=False)

CONFIG = dict(version=VERSION, mode='PAPER_ONLY', symbols=SYMBOLS, ladder=LADDER,
              fee=FEE, initial_cash=1000, reserve=250, coin_basis_cap=150,
              total_basis_cap=750, max_slots=5, max_quote_age_ms=MAX_AGE,
              max_slippage=MAX_SLIP, hourly_visibility_delay_ms=300000,
              entry='prior24h_low <= completed_hour_close <= low*1.01; close>=previous_close',
              dca='last_fill-max(.75*ATR14, .01*last_fill); old_trough*1.015',
              tp='fee_inclusive_average*1.016; later old_peak*.998',
              partial_buy='consume one scheduled rung; never top up',
              partial_sell='retain cycle and proportional basis until marketable remainder below minimum')
CONFIG_HASH = hashlib.sha256(canonical(CONFIG).encode()).hexdigest()

class PublicSpot:
    """The only outbound exchange transport: fixed HTTPS host, GET allowlist, spot only."""
    def __init__(self,symbols=SYMBOLS):
        if not set(symbols)<=set(SYMBOLS+RESEARCH_COHORT):
            raise ValueError('outside approved spot research universe')
        self.symbols=tuple(symbols)
    def get(self, endpoint, symbol, **params):
        if endpoint not in ('instruments-info', 'orderbook', 'kline') or (symbol not in self.symbols and not (endpoint=='instruments-info' and symbol is None)):
            raise ValueError('public spot allowlist')
        params.update(category='spot')
        if symbol is not None: params['symbol']=symbol
        url = 'https://api.bybit.com/v5/market/' + endpoint + '?' + urllib.parse.urlencode(params)
        start = now_ms()
        req = urllib.request.Request(url, headers={'User-Agent': 'Phoenix-Paper/1', 'Cache-Control': 'no-cache'})
        # Redirects could change the public-data-only destination. Refuse them.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('redirect refused')
        with urllib.request.build_opener(NoRedirect).open(req, timeout=8) as r:
            data = json.load(r)
        end = now_ms()
        if data.get('retCode') != 0:
            raise ValueError('public API error: '+str(data.get('retCode'))+' '+str(data.get('retMsg',''))[:160])
        result = data['result']
        if result.get('category', 'spot') != 'spot':
            raise ValueError('non-spot result')
        return result, dict(request_at=start, received_at=end, rtt_ms=end-start, server_at=data.get('time'))

def parse_rule(result, symbol, received_at):
    items = [x for x in result.get('list', []) if x.get('symbol') == symbol and x.get('status') == 'Trading'
             and x.get('quoteCoin') == 'USDT' and x.get('baseCoin') == symbol[:-4]]
    if len(items) != 1:
        raise ValueError('genuine trading spot instrument unavailable')
    x = items[0]
    lot = x['lotSizeFilter']
    rule = dict(step=float(lot['basePrecision']), quote_step=float(lot['quotePrecision']),
                minimum=float(lot['minOrderAmt']), maximum=float(lot['maxMarketOrderQty']),
                tick=float(x['priceFilter']['tickSize']), at=received_at, raw=x)
    if any(not math.isfinite(rule[k]) or rule[k] <= 0 for k in ('step','quote_step','minimum','maximum','tick')):
        raise ValueError('incomplete instrument rules')
    return rule

def valid_book(book, at, symbol=None):
    if symbol and book.get('s') != symbol:
        raise ValueError('book symbol mismatch')
    for t in (book.get('ts'), book.get('received_at')):
        if not isinstance(t, (int, float)) or not 0 <= at-t <= MAX_AGE:
            raise ValueError('stale or future quote')
    if not book.get('a') or not book.get('b'):
        raise ValueError('missing depth')
    for side in ('a','b'):
        prev = None
        for row in book[side]:
            p,q = map(float,row)
            if not all(math.isfinite(v) and v>0 for v in (p,q)):
                raise ValueError('invalid depth')
            if prev is not None and ((side=='a' and p<=prev) or (side=='b' and p>=prev)):
                raise ValueError('unordered depth')
            prev=p
    bid, ask = float(book['b'][0][0]), float(book['a'][0][0])
    if bid >= ask:
        raise ValueError('crossed book')
    return bid,ask

def marketable(book, rule, side, amount, at):
    """Deterministic visible-depth IOC hypothetical, bounded to 15bp of top.
    Buy amount is quote budget; sell amount is base quantity. No candle-volume proxy.
    Displayed depth is not a promise that an actual exchange would fill.
    """
    bid,ask=valid_book(book,at)
    levels=book['a' if side=='buy' else 'b']
    best=ask if side=='buy' else bid
    target=floor(amount/best,rule['step']) if side=='buy' else floor(amount,rule['step'])
    target=min(target,floor(rule['maximum'],rule['step']))
    if target*best < rule['minimum']:
        return dict(qty=0,notional=0,status='rejected_minimum',price=0)
    remain=target; qty=notional=0.
    budget=floor(amount,rule['quote_step']) if side=='buy' else math.inf
    for p,q in levels:
        p,q=float(p),float(q)
        if (p>best*(1+MAX_SLIP) if side=='buy' else p<best*(1-MAX_SLIP)):
            break
        take=floor(min(remain,q,(budget-notional)/p),rule['step'])
        if take<=0:
            continue
        qty+=take; notional+=take*p; remain=max(0,target-qty)
        if remain < rule['step']*.5:
            break
    return dict(qty=qty,notional=notional,price=notional/qty if qty else 0,
                status=('filled' if remain<rule['step']*.5 else 'partial') if qty else 'rejected_depth',
                requested_qty=target,unfilled_qty=remain,bid=bid,ask=ask,
                spread_bps=(ask-bid)/((ask+bid)/2)*10000,
                quote_at=book['ts'],quote_age_ms=at-book['ts'],received_at=book['received_at'])

def feature(rows, at):
    # rows are UTC hour-start OHLC. Only a completed hour plus frozen 5m delay is visible.
    rows=sorted((r for r in rows if r[0]+HOUR+300000<=at),key=lambda r:r[0])
    history=[]; atr=None; trs=[]; prev=None; pt=None
    for t,o,h,l,c,*_ in rows:
        if pt is not None and t-pt != HOUR:
            history=[];atr=None;trs=[];prev=None
        if not (0<l<=min(o,c)<=max(o,c)<=h) or t%HOUR:
            raise ValueError('invalid hourly OHLC')
        tr=max(h-l,abs(h-prev),abs(l-prev)) if prev else h-l
        trs.append(tr)
        if len(trs)==14: atr=sum(trs)/14
        elif len(trs)>14: atr=(13*atr+tr)/14
        low=min(r[3] for r in history[-24:]) if len(history)>=24 else 0
        dip=bool(low and low<=c<=low*1.01 and c>=prev)
        history.append([t,o,h,l,c]);prev=c;pt=t
    if len(history)<240 or atr is None:
        raise ValueError('240 contiguous completed-hour warmup required')
    if at-(pt+HOUR+300000)>=HOUR:
        raise ValueError('stale hourly features')
    return dict(at=pt+HOUR+300000,atr=atr,dip=dip,close=prev,prior_low=low,hours=len(history))

def position():
    return dict(qty=0.,basis=0.,active=False,purchases=0,entry_at=0,last_exit=0,
                peak=None,trough=None,threshold=0.,last_fill=0.,pending=None,
                cycles=0,underwater_since=None,max_underwater_ms=0,mark=None,mark_at=None,last_bar=None,
                last_observation_bar=None,last_observation_at=None)

def initial():
    return dict(config_hash=CONFIG_HASH,mode='PAPER_ONLY',cash=1000.,realized=0.,fees=0.,
                positions={s:position() for s in SYMBOLS},peak_equity=1000.,max_drawdown=0.,
                observations=0,started_at=now_ms(),last_at=None)

def audit(state):
    if state['mode']!='PAPER_ONLY' or state['config_hash']!=CONFIG_HASH:
        raise ValueError('immutable PAPER_ONLY/config mismatch')
    ps=list(state['positions'].values())
    assert state['cash']>=250-1e-8
    assert sum(p['basis'] for p in ps)<=750+1e-8
    assert sum(p['active'] for p in ps)<=5
    assert all(0<=p['purchases']<=15 and -1e-9<=p['basis']<=150+1e-8 and p['qty']>=-1e-9 for p in ps)
    # Accounting identity independent of mark quality.
    assert abs(state['cash']+sum(p['basis'] for p in ps)-1000-state['realized'])<1e-6

def signal(p,bar,f,observed,force_entry=False):
    t,o,h,l,c=bar[:5];close_at=t+MINUTE
    if t%MINUTE or close_at>observed or observed-close_at>15000:
        raise ValueError('incomplete or stale signal bar')
    if not (0<l<=min(o,c)<=max(o,c)<=h):
        raise ValueError('invalid minute OHLC')
    if p['last_bar'] is not None and t<=p['last_bar']:
        return None
    if p['last_bar'] is not None and t-p['last_bar']!=MINUTE:
        p['peak']=p['trough']=None # gaps invalidate unseen extrema chronology
    p['last_bar']=t
    if p['pending']:
        return None
    side=None;reason=None
    if p['active']:
        oldpeak,oldtrough=p['peak'],p['trough']
        sell=oldpeak is not None and l<=oldpeak*.998
        target=p['basis']/p['qty']*1.016
        if oldpeak is not None or h>=target:
            p['peak']=max(oldpeak or 0,h)
        buy=False
        if p['purchases']<15:
            if oldtrough is not None: p['trough']=min(oldtrough,l)
            elif l<=p['threshold']: p['trough']=l
            buy=oldtrough is not None and l>=oldtrough and h>=oldtrough*1.015
        if sell: side,reason='sell','old_peak_callback'
        elif buy: side,reason='buy','old_trough_rebound'
    elif t>p['last_exit'] and (f['dip'] or force_entry):
        side,reason='buy','weekly_slot_fill' if force_entry else 'discounted_entry'
    if side:
        # Next actionable minute begins at close_at; observation and execution must follow.
        p['pending']=dict(side=side,reason=reason,signal_at=close_at,observed_at=observed,
                          actionable_at=close_at,expires_at=close_at+MINUTE,atr=f['atr'])
    return p['pending']

def advance(state,symbol,bar,f,book,rule,at,allow_buy=True,allow_entry=True,force_entry=False):
    """One atomic observation: prior instruction first, then latest completed-minute signal."""
    p=state['positions'][symbol];events=[]
    bid,ask=valid_book(book,at,symbol)
    if at-rule['at']>HOUR or rule['at']>at:
        raise ValueError('stale instrument rules')
    order=p['pending'];executed=False
    events.append(dict(type='observation',symbol=symbol,at=at,bar=bar,feature=f,book=book,rule=rule,allow_buy=allow_buy,allow_entry=allow_entry,force_entry=force_entry))
    if order and at>order['observed_at'] and at>=order['actionable_at'] and at//MINUTE>order.get('last_attempt_bar',-1):
        order['last_attempt_bar']=at//MINUTE
        side=order['side']
        # Never turn a delayed buy into a stale instruction; sells retry with a new snapshot.
        if side=='buy' and (at>=order['expires_at'] or not allow_buy or (not p['active'] and not allow_entry)):
            events.append(dict(type='expiry',order=order,reason='expired_or_data_halt'));p['pending']=None
        else:
            budget=LADDER[p['purchases']] if side=='buy' and p['purchases']<15 else 0
            permitted=(side=='sell' or (allow_buy and budget and state['cash']-budget>=250-1e-8
                       and p['basis']+budget<=150+1e-8
                       and sum(x['basis'] for x in state['positions'].values())+budget<=750+1e-8
                       and (p['active'] or sum(x['active'] for x in state['positions'].values())<5)))
            fill=marketable(book,rule,side,budget if side=='buy' else p['qty'],at) if permitted else dict(qty=0,notional=0,status='rejected_risk',price=0)
            event=dict(type='attempt',symbol=symbol,side=side,at=at,order=copy.deepcopy(order),
                       latency_ms=at-order['signal_at'],observation_latency_ms=order['observed_at']-order['signal_at'],
                       fill=fill,budget=budget,rule=rule,book=book)
            if fill['qty']>0:
                q,cost=fill['qty'],fill['notional'];fee=cost*FEE
                if side=='buy':
                    if not p['active']:
                        p.update(active=True,purchases=0,entry_at=at)
                    p['qty']+=q*(1-FEE);p['basis']+=cost;p['purchases']+=1
                    state['cash']-=cost
                    p['last_fill']=fill['price']
                    p['threshold']=fill['price']-max(.75*order['atr'],.01*fill['price'])
                    p['peak']=p['trough']=None
                    event['buy_number']=p['purchases']
                else:
                    removed=p['basis']*q/p['qty'];proceeds=cost-fee
                    p['qty']=max(0,p['qty']-q);p['basis']=max(0,p['basis']-removed)
                    state['cash']+=proceeds;state['realized']+=proceeds-removed
                    event.update(removed_basis=removed,realized=proceeds-removed)
                    # Retain unsellable dust exactly; a completed cycle does not erase it.
                    if floor(p['qty'],rule['step'])*bid<rule['minimum']:
                        p.update(active=False,purchases=0,last_exit=at,peak=None,trough=None,cycles=p['cycles']+1)
                state['fees']+=fee;event['fee']=fee;executed=True
                p['last_bar']=max(p['last_bar'] or 0,bar[0])
            events.append(event)
            if side=='buy' or not p['active']:
                p['pending']=None
    # No signals from a bar that overlapped a just-executed fill.
    if not executed and not p['pending'] and (p['active'] or (allow_buy and allow_entry)) and 0<=at-(bar[0]+MINUTE)<=15000:
        order=signal(p,bar,f,at,force_entry=force_entry)
        if order: events.append(dict(type='signal',symbol=symbol,order=copy.deepcopy(order)))
    p['mark']=bid;p['mark_at']=book['ts']
    underwater=p['qty']*bid<p['basis']-1e-9
    if underwater and p['underwater_since'] is None: p['underwater_since']=at
    if p['underwater_since'] is not None:
        p['max_underwater_ms']=max(p['max_underwater_ms'],at-p['underwater_since'])
        if not underwater: p['underwater_since']=None
    state['observations']+=1;state['last_at']=at
    p['last_observation_bar']=bar[0];p['last_observation_at']=at
    equity=state['cash']+sum(x['qty']*(x['mark'] or 0) for x in state['positions'].values())
    state['peak_equity']=max(state['peak_equity'],equity)
    state['max_drawdown']=max(state['max_drawdown'],1-equity/state['peak_equity'])
    audit(state)
    return events

class Store:
    """SQLite state + hash-chained append-only events in the same FULL-sync transaction.
    An exclusive process lock avoids two replicas. Event observation keys deduplicate retries.
    """
    def __init__(self,path,factory=initial,validator=audit,schema=None):
        self.validator=validator
        if str(path).startswith(('postgresql://','postgres://')):
            self.db=Postgres(path,schema=schema)
        else:
            self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, observation TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TABLE IF NOT EXISTS hours (symbol TEXT, t INTEGER, payload TEXT NOT NULL, PRIMARY KEY(symbol,t));''')
        row=self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        self.state=json.loads(row[0]) if row else factory();self.validator(self.state)
        if not row: self.db.execute('INSERT INTO state VALUES(1,?)',(canonical(self.state),))
        self.verify()

    def verify(self):
        prev='0'*64
        last=None
        for key,payload,previous,digest in self.db.execute('SELECT observation,payload,previous,hash FROM events ORDER BY id'):
            if previous!=prev or hashlib.sha256((prev+key+payload).encode()).hexdigest()!=digest:
                raise ValueError('ledger integrity failure')
            prev=digest;last=json.loads(payload)
        if last and last['after']!=self.state:
            raise ValueError('ledger/state reconciliation failed')
        return prev

    def apply(self,key,fn):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM events WHERE observation=?',(key,)).fetchone():
                self.db.execute('ROLLBACK');return False
            state=copy.deepcopy(self.state);events=fn(state);self.validator(state)
            payload=canonical(dict(events=events,after=state))
            prev=self.db.execute('SELECT hash FROM events ORDER BY id DESC LIMIT 1').fetchone()
            prev=prev[0] if prev else '0'*64
            digest=hashlib.sha256((prev+key+payload).encode()).hexdigest()
            self.db.execute('INSERT INTO events(observation,payload,previous,hash) VALUES(?,?,?,?)',(key,payload,prev,digest))
            self.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
            self.db.execute('COMMIT');self.state=state;return True
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def hourly(self,symbol,rows):
        for row in rows:
            self.db.execute('INSERT INTO hours VALUES(?,?,?) ON CONFLICT(symbol,t) DO NOTHING',(symbol,int(row[0]),canonical(row)))
        return [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM hours WHERE symbol=? ORDER BY t',(symbol,))]

    def ledger(self,limit=50):
        return [dict(id=i,key=k,hash=h,previous=prev,**json.loads(p)) for i,k,p,prev,h in
                self.db.execute('SELECT id,observation,payload,previous,hash FROM events ORDER BY id DESC LIMIT ?',(min(limit,200),))]

class Postgres:
    """Same transaction/state contract on Northflank's included private PostgreSQL addon."""
    def __init__(self,url,schema=None):
        import psycopg
        self.connection=psycopg.connect(url,autocommit=True,connect_timeout=10)
        if schema not in (None,'phoenix_weekly','phoenix_weekly_five'):
            self.connection.close();raise ValueError('unapproved storage namespace')
        lock=3 if schema=='phoenix_weekly_five' else 2 if schema else 1
        if not self.connection.execute('SELECT pg_try_advisory_lock(50484,%s)',(lock,)).fetchone()[0]:
            self.connection.close()
            raise ValueError('another paper worker owns the database')
        # Managed addon users can create tables in public without CREATE DATABASE/SCHEMA.
        # Fixed keeps its original names; weekly uses distinct tables and trigger function.
        self.prefix='five_' if schema=='phoenix_weekly_five' else 'weekly_' if schema else ''
        self.connection.execute('SET synchronous_commit=on')
        self.execute('''CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,observation TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,previous TEXT NOT NULL,hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hours(symbol TEXT,t BIGINT,payload TEXT NOT NULL,PRIMARY KEY(symbol,t));''')
        self.execute('''CREATE OR REPLACE FUNCTION phoenix_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'append only'; END; $$;
          DROP TRIGGER IF EXISTS immutable_events ON events;
          CREATE TRIGGER immutable_events BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION phoenix_append_only();''')
    def execute(self,sql,params=()):
        if sql=='BEGIN IMMEDIATE': sql='BEGIN'
        if self.prefix:
            import re
            sql=re.sub(r'\b(state|events|hours|selection_inputs|phoenix_append_only)\b',lambda m:self.prefix+m.group(0),sql)
        # A nonempty parameter tuple selects extended-query protocol; schema setup has multiple statements.
        return self.connection.execute(sql.replace('?','%s'),params) if params else self.connection.execute(sql.replace('?','%s'))
    def close(self):self.connection.close()
