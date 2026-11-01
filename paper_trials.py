"""Bounded prospective paper research. Does not control exchange orders.

No old analyzed data is imported. Public summaries hide prospective outcomes
until the sealed chronological split is available under the frozen protocol.
"""
from collections import deque,Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone,timedelta
import copy,csv,hashlib,json,math,sqlite3,statistics,threading,os
from research_engine import atr,canonical,MINUTE,FIVE,HOUR,DAY
from paper_signals import confirmations
from paper_paths import adverse,path_step,merge,economic_key

PROTOCOL=json.loads(Path(__file__).with_name('trial_protocol.json').read_text())
HASH=hashlib.sha256(canonical(PROTOCOL).encode()).hexdigest()
VARIANTS=('baseline','limit','wide_stop','limit_through','arrival')
LEGACY_HASH='9c2822f302bc90ee694072c9e48bc3f298bd54584bd52a6887884594442d6a1d'
HOLDOUT_DELAY=28*DAY*7//10

def fresh(rows,step,at,count):
    a=[r for r in rows if r['available_ms']<=at and r['t']+step<=at][-count:]
    return a if len(a)==count and at-a[-1]['t']<=2*step+15000 and all(b['t']-a['t']==step for a,b in zip(a,a[1:])) else []
def ema(values,n):
    value=values[0]
    for x in values[1:]:value+=2/(n+1)*(x-value)
    return value
def state(stop):return dict(remaining=1.,tp1=False,tp2=False,closed=False,stop=stop,unit_gross=0.,exit_notional=0.,events=[],mfe=0.,mae=0.)
def snapshot_context(h1,m5,funding,quote,side,entry,stop,ts):
    btc=fresh(h1.get('BTCUSDT',[]),HOUR,ts,60)
    result=dict(at_ms=ts,policy_sha256=HASH,regime='unknown',btc_1h_trend=None,btc_1h_volatility=None,
        btc_available_ms=btc[-1]['available_ms'] if btc else None)
    if btc:
        closes=[x['c'] for x in btc];change=[math.log(b/a) for a,b in zip(closes[-25:],closes[-24:])]
        variation=sum(abs(b-a) for a,b in zip(closes[-25:],closes[-24:]));eff=abs(closes[-1]-closes[-25])/variation if variation else 0.
        e20,e50=ema(closes,20),ema(closes,50)
        result.update(btc_1h_trend='up' if e20>e50 else 'down' if e20<e50 else 'flat',btc_1h_volatility=statistics.stdev(change),btc_efficiency24=eff,btc_ema20=e20,btc_ema50=e50)
        result['regime']='trending_up' if eff>=.30 and e20>e50 else 'trending_down' if eff>=.30 and e20<e50 else 'choppy'
    bars=fresh(m5,FIVE,ts,21);hours=fresh(h1.get('coin',[]),HOUR,ts,60)
    a5=atr(bars) if bars else None;a1=atr(hours) if hours else None
    result.update(atr_5m=a5,atr_1h=a1,volume_ratio_5m=(float(bars[-1].get('v',0))/(sum(float(x.get('v',0)) for x in bars[:-1])/20)) if bars and sum(float(x.get('v',0)) for x in bars[:-1]) else None,
        coin_5m_available_ms=bars[-1]['available_ms'] if bars else None,coin_1h_available_ms=hours[-1]['available_ms'] if hours else None,
        stop_distance_pct=abs(entry-stop)/entry,stop_atr_5m=abs(entry-stop)/a5 if a5 else None,stop_atr_1h=abs(entry-stop)/a1 if a1 else None,
        thin_stop=abs(entry-stop)/entry<.006,utc_hour=ts%DAY/HOUR,colombo_hour=((ts+19800000)%DAY)/HOUR,
        hour_block_utc=f'{(ts//HOUR)%24//4*4:02d}-{(ts//HOUR)%24//4*4+4:02d}',
        spread_bps=None,quote_observed_ms=None,funding_rate=None,funding_observed_ms=None,funding_next_ms=None,
        missing=[],filled_in_context=any(k.get('source')=='REST_WARMUP' for k in bars+hours+btc))
    if quote and 0<=ts-quote['observed_ms']<=3000 and 0<=ts-quote['exchange_ms']<=3000:
        result.update(spread_bps=(quote['ask']-quote['bid'])/((quote['ask']+quote['bid'])/2)*10000,quote_observed_ms=quote['observed_ms'])
    if funding and 0<=ts-funding['observed_ms']<=15*MINUTE:
        result.update(funding_rate=funding['rate'],funding_observed_ms=funding['observed_ms'],funding_next_ms=funding.get('next_ms'),funding_interval_minutes=funding.get('interval_minutes',480))
    # The model funding reserve stays frozen. The current indicative rate is
    # logged independently and is not claimed to be a future settled charge.
    result['estimated_total_cost_R']=(entry*.0022+(entry+stop)*.0005)/abs(entry-stop)
    result['estimated_cost_model']='6bp entry + 6bp exit + 10bp funding reserve + 5bp each fill; before tick rounding'
    for k in ('btc_1h_trend','btc_1h_volatility','atr_5m','atr_1h','volume_ratio_5m','spread_bps','funding_rate'):
        if result[k] is None:result['missing'].append(k)
    return result

class PaperTrials:
    def __init__(self,root,symbols,now_ms,emit=None):
        self.lock=threading.RLock();self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'research_trials.sqlite';self.symbols=tuple(symbols);self.emit=emit;self.halted=None
        self.db=sqlite3.connect(self.path,check_same_thread=False)
        self.db.executescript('''PRAGMA cache_size=-4096;PRAGMA temp_store=FILE;PRAGMA synchronous=FULL;PRAGMA max_page_count=16384;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY,setup TEXT UNIQUE,s TEXT,side INTEGER,at INTEGER,bucket INTEGER,context TEXT,plan TEXT,UNIQUE(s,side,bucket));
        CREATE INDEX IF NOT EXISTS signal_time ON signals(at,id);
        CREATE TABLE IF NOT EXISTS trades(signal INTEGER,variant TEXT,status TEXT,payload TEXT,result TEXT,PRIMARY KEY(signal,variant));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at INTEGER,kind TEXT,payload TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS results(k TEXT PRIMARY KEY,v TEXT);
        ''')
        old=self.meta('protocol_hash')
        if old and old!=HASH:
            if old!=LEGACY_HASH or self.meta('phase')!='collecting':raise ValueError('FROZEN_TRIAL_PROTOCOL_CHANGED')
            first=self.db.execute('SELECT min(at) FROM signals').fetchone()[0]
            if first is not None and now_ms>=first+HOLDOUT_DELAY:raise ValueError('AMENDMENT_AFTER_HOLDOUT_FORBIDDEN')
            backup_path=self.root/'research-trials-pre-r2.sqlite'
            if backup_path.exists():raise ValueError('EXISTING_MIGRATION_BACKUP_REQUIRES_REVIEW')
            backup=sqlite3.connect(backup_path)
            try:self.db.backup(backup,pages=64)
            finally:backup.close()
            self.put('previous_protocol_hash',old);self.put('amended_ms',now_ms)
            self.put('protocol_hash',HASH);self.put('filter_lock',dict(filters=[],hash=HASH,locked_ms=now_ms))
            self.put('amendment_first_signal_id',self.db.execute('SELECT coalesce(max(id),0)+1 FROM signals').fetchone()[0])
        if not old:
            self.put('protocol_hash',HASH);self.put('started_ms',now_ms);self.put('filter_lock',dict(filters=[],hash=HASH,locked_ms=now_ms));self.put('phase','collecting')
        self.initialized=self.meta('started_ms');self.phase=self.meta('phase');self._kill_running=False
        self.started=self.db.execute('SELECT min(at) FROM signals').fetchone()[0]
        self.clock_path=self.root/'research-trial-clock.lock.json'
        if self.started is not None:self._lock_clock(self.started)
        elif self.clock_path.exists():raise ValueError('CLOCK_WITHOUT_FIRST_SIGNAL')
        self.previous=self.db.execute('SELECT hash FROM events ORDER BY id DESC LIMIT 1').fetchone();self.previous=self.previous[0] if self.previous else '0'*64
        self.event_count=self.db.execute('SELECT count(*) FROM events').fetchone()[0]
        self.signal_count=self.db.execute('SELECT count(*) FROM signals').fetchone()[0]
        self.active={};self.watches={s:[] for s in symbols};self.h1={s:deque(maxlen=240) for s in set(symbols)|{'BTCUSDT'}};self.m5={s:deque(maxlen=300) for s in symbols}
        self.minute_pending={s:deque(maxlen=5) for s in symbols};self.quotes={};self.funding={};self.instrument={};self.last_minute={}
        self.quote_history={s:deque(maxlen=512) for s in symbols};self.funding_history={s:deque(maxlen=128) for s in symbols}
        # Restarts do not rewrite outcomes or silently assume uninterrupted fills.
        for sid,var,text in self.db.execute("SELECT signal,variant,payload FROM trades WHERE status IN ('open','pending')"):
            p=json.loads(text);self.db.execute("UPDATE trades SET status='censored',result=? WHERE signal=? AND variant=?",(canonical(dict(reason='SERVICE_RESTART',at_ms=now_ms,filled=p.get('filled',p.get('status')=='open'))),sid,var))
        self.db.commit();self.event('protocol_frozen',now_ms,protocol=PROTOCOL,protocol_hash=HASH,filter_lock=self.meta('filter_lock'))
    def _lock_clock(self,first):
        value=dict(first_signal_ms=first,holdout_start_ms=first+HOLDOUT_DELAY,batch_end_ms=first+28*DAY,rule='70_PERCENT_OF_28_DAYS_BY_UTC_TIME')
        text=canonical(value);digest=hashlib.sha256(text.encode()).hexdigest()
        saved=self.meta('clock_lock')
        if saved is not None and saved!=dict(value,sha256=digest):raise ValueError('CLOCK_LOCK_MISMATCH')
        if self.clock_path.exists():
            if self.clock_path.read_text()!=text:raise ValueError('CLOCK_FILE_CHANGED')
        else:
            if saved is not None:raise ValueError('CLOCK_FILE_MISSING')
            fd=os.open(self.clock_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o444)
            with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        if saved is None:self.put('clock_lock',dict(value,sha256=digest))
        self.db.executescript("""CREATE TRIGGER IF NOT EXISTS clock_no_update BEFORE UPDATE ON meta WHEN OLD.k='clock_lock' BEGIN SELECT RAISE(ABORT,'CLOCK_IMMUTABLE'); END;
        CREATE TRIGGER IF NOT EXISTS clock_no_delete BEFORE DELETE ON meta WHEN OLD.k='clock_lock' BEGIN SELECT RAISE(ABORT,'CLOCK_IMMUTABLE'); END;""")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS clock_no_replace BEFORE INSERT ON meta WHEN NEW.k='clock_lock' AND EXISTS(SELECT 1 FROM meta WHERE k='clock_lock') BEGIN SELECT RAISE(ABORT,'CLOCK_IMMUTABLE'); END")
        self.started=first
    def verify_clock(self):
        if self.started is not None:self._lock_clock(self.started)
    def holdout_start(self):return self.started+HOLDOUT_DELAY if self.started is not None else None
    def protected_signal(self,sid):
        row=self.db.execute('SELECT at FROM signals WHERE id=?',(sid,)).fetchone()
        return self.phase!='complete' and (not row or self.started is None or row[0]>=self.holdout_start())
    def public_record(self,value):
        # Fail closed for unknown terminal IDs. Do not serialize their outcomes
        # into either public capture logs or the private event-chain log.
        if value.get('kind')=='trial_trade_terminal' and self.protected_signal(value.get('signal_id')):
            return dict(kind='trial_outcome_withheld',receive_ms=value['receive_ms'],signal_id=value.get('signal_id'),variant=value.get('variant'),protocol_sha256=HASH)
        return value
    def cluster_count(self):return self.db.execute('SELECT count(DISTINCT bucket) FROM signals').fetchone()[0]
    def privacy_audit(self):
        self.verify_clock();violations=0;checked=0
        for text, in self.db.execute("SELECT payload FROM events WHERE kind='trade_terminal'"):
            r=json.loads(text)
            if self.protected_signal(r.get('signal_id')):
                checked+=1;violations+=r.get('kind')!='trial_outcome_withheld'
        return dict(clock_verified=True,protected_terminal_events_checked=checked,exposed_protected_outcomes=violations,
            archive_guard='EVERY_CAPTURE_MEMBER_REDACTED_ON_EXPORT',trial_database_exported=self.phase=='complete',kill_checked_ms=self.meta('kill_checked_ms'))
    def meta(self,k):
        r=self.db.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone();return json.loads(r[0]) if r else None
    def put(self,k,v):self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,canonical(v)))
    def event(self,kind,ts,**data):
        if self.event_count>=100000:raise RuntimeError('TRIAL_EVENT_CAP')
        value=self.public_record(dict(kind='trial_'+kind,receive_ms=ts,protocol_sha256=HASH,**data));text=canonical(value)
        h=hashlib.sha256((self.previous+text).encode()).hexdigest();self.db.execute('INSERT INTO events(at,kind,payload,hash) VALUES(?,?,?,?)',(ts,kind,text,h));self.previous=h;self.event_count+=1
        self.db.commit()
        if self.emit:self.emit(dict(value,ledger_hash=h))
    def guarded(self,method,*args):
        if self.halted:return
        with self.lock:
            try:return getattr(self,method)(*args)
            except Exception as e:
                self.halted=type(e).__name__+': '+str(e)
    def metadata(self,name,payload,ts):
        for s in self.h1:
            if name=='warmup_1h_'+s:
                for k in sorted(payload.get('result',{}).get('list',[]),key=lambda x:int(x[0])):
                    t=int(k[0])
                    if t+HOUR<=ts:self._bar(self.h1[s],dict(t=t,o=float(k[1]),h=float(k[2]),l=float(k[3]),c=float(k[4]),v=float(k[5]),available_ms=ts,source='REST_WARMUP'))
            if name=='warmup_5m_'+s and s in self.m5:
                for k in sorted(payload.get('result',{}).get('list',[]),key=lambda x:int(x[0])):
                    t=int(k[0])
                    if t+FIVE<=ts:self._bar(self.m5[s],dict(t=t,o=float(k[1]),h=float(k[2]),l=float(k[3]),c=float(k[4]),v=float(k[5]),available_ms=ts,source='REST_WARMUP'))
            if name=='instrument_'+s:
                rows=payload.get('result',{}).get('list',[])
                if rows:self.instrument[s]=rows[0]
    @staticmethod
    def _bar(rows,b):
        # REST warmup races live recording. Merge older missing candles instead
        # of discarding the entire response after the first live candle.
        # Keep the first observation for duplicates; never backdate availability.
        if any(x['t']==b['t'] for x in rows):return
        merged=sorted([*rows,b],key=lambda x:x['t'])[-rows.maxlen:]
        rows.clear();rows.extend(merged)
    def market_message(self,p,ts):
        topic=p.get('topic','');s=topic.rsplit('.',1)[-1]
        if topic.startswith('publicTrade.'):
            self.arrival_ticks(s,p.get('data',[]),ts);return
        if topic.startswith('tickers.'):
            v=p.get('data',{})
            if 'fundingRate' in v:
                self.funding[s]=dict(rate=float(v['fundingRate']),next_ms=int(v.get('nextFundingTime',0)),observed_ms=ts,interval_minutes=int(self.instrument.get(s,{}).get('fundingInterval',480)))
            elif s in self.funding:self.funding[s]=dict(self.funding[s],observed_ms=ts)
            if s in self.funding and s in self.funding_history:
                history=self.funding_history[s]
                if not history or ts-history[-1]['observed_ms']>=1000:history.append(dict(self.funding[s]))
        elif topic.startswith(('kline.60.','kline.5.')):
            for k in p.get('data',[]):
                step=HOUR if topic.startswith('kline.60.') else FIVE
                rows=self.h1.get(s) if step==HOUR else self.m5.get(s)
                if k.get('confirm') and rows is not None and int(k['start'])+step<=ts:
                    self._bar(rows,dict(t=int(k['start']),o=float(k['open']),h=float(k['high']),l=float(k['low']),c=float(k['close']),v=float(k['volume']),available_ms=ts,source='CLOSED_EXCHANGE_KLINE'))
    def quote(self,s,q,ts):
        if q:
            self.quotes[s]=dict(q,observed_ms=ts);history=self.quote_history[s]
            if not history or ts-history[-1]['observed_ms']>=50:history.append(dict(self.quotes[s]))
    def base_event(self,r):
        kind=r.get('kind');s=r.get('symbol');ts=r['receive_ms']
        if kind=='research_boundary':self.boundary(ts,r.get('reason','SOURCE_BOUNDARY'));return
        if s not in self.watches:return
        if kind=='research_watch' and self.phase=='collecting':
            w=copy.deepcopy(r['frozen'])
            if not w.get('instrument') or not w.get('local_atr'):return
            if len(self.watches[s])>=128:raise RuntimeError('TRIAL_WATCH_CAP')
            w['_last_t']=r['price_context_5m'][-1]['t'] if r.get('price_context_5m') else None
            self.watches[s].append(w)
        if kind=='research_bar_5m':
            b=r['bar'];self._bar(self.m5[s],b)
            for w in list(self.watches[s]):
                z=w['zone'];side=w['side']
                if ts>=w['expires_ms'] or (w['_last_t'] is not None and b['t']!=w['_last_t']+FIVE) or (b['c']<z['low'] if side==1 else b['c']>z['high']):self.watches[s].remove(w);continue
                found=confirmations(w,b,r.get('footprint_levels'),ts)
                if found:
                    self.signal(s,w,b,found,ts,r.get('footprint_levels'));self.watches[s].remove(w)
                else:
                    w['last_close']=b['c'];w['_last_t']=b['t'];ref=w.get('reference')
                    if ref and side*(b['c']-ref['price'])>0:w['reference_unbroken']=False
    def signal(self,s,w,b,found,ts,levels=None):
        if self.phase!='collecting':return
        if self.signal_count>=10000:raise RuntimeError('TRIAL_SIGNAL_CAP')
        if len(self.active)+len(VARIANTS)>512:raise RuntimeError('TRIAL_ACTIVE_CAP_BEFORE_PAIRED_INSERT')
        bucket=ts//FIVE*FIVE;side=w['side'];z=w['zone'];tick=w['instrument']['tick'];a=w['local_atr']
        old=self.db.execute('SELECT id FROM signals WHERE s=? AND side=? AND bucket=?',(s,side,bucket)).fetchone()
        if old:self.event('duplicate',ts,setup=w['id'],kept_signal=old[0],symbol=s,side=side,bucket=bucket);return
        if self.started is None:self._lock_clock(ts)
        stop=(min(z['low'],b['l'])-.1*a if side==1 else max(z['high'],b['h'])+.1*a)
        stop=(math.floor(stop/tick) if side==1 else math.ceil(stop/tick))*tick
        entry=adverse(b['c'],side,tick,True)
        at_count=self.db.execute('SELECT count(*) FROM signals WHERE bucket=?',(bucket,)).fetchone()[0]+1
        # Baseline price/time is the frozen theoretical confirmation close.
        # Never attach 5 seconds of later context to that backdated benchmark.
        cutoff=b['t']+FIVE
        prior_quote=next((q for q in reversed(self.quote_history[s]) if q['observed_ms']<=cutoff),None)
        prior_funding=next((q for q in reversed(self.funding_history[s]) if q['observed_ms']<=cutoff),None)
        context=snapshot_context({'BTCUSDT':self.h1['BTCUSDT'],'coin':self.h1[s]},self.m5[s],prior_funding,prior_quote,side,entry,stop,cutoff)
        q=self.quotes.get(s)
        context.update(signal_received_ms=ts,signal_spread_bps=((q['ask']-q['bid'])/((q['ask']+q['bid'])/2)*10000) if q and 0<=ts-q['observed_ms']<=3000 else None,
            signal_spread_role='Receipt-time diagnostic only; may follow the counterfactual close entry and must not be a baseline filter.')
        context.update(concurrent_count_at_signal=at_count,cohort=bucket,confirmation_groups=['poi']+sorted(found),confirmation_evidence=found,zone=copy.deepcopy(z),instrument=copy.deepcopy(w['instrument']))
        context['incomplete']=bool(context['missing'])
        context['wide_stop_context_complete']=context['atr_5m'] is not None and context['volume_ratio_5m'] is not None
        arrival_quote=q if q and 0<=ts-q['observed_ms']<=3000 and 0<=ts-q['exchange_ms']<=3000 else None
        arrival_price=arrival_quote['ask' if side==1 else 'bid'] if arrival_quote else None
        context.update(arrival_reference_price=arrival_price,confirmation_close=b['c'],entry_delay_ms=ts-cutoff,
            arrival_quote_observed_ms=arrival_quote['observed_ms'] if arrival_quote else None,
            arrival_minus_close_price=arrival_price-b['c'] if arrival_price is not None else None,
            arrival_minus_close_bps=(arrival_price/b['c']-1)*10000 if arrival_price is not None else None)
        plan=dict(symbol=s,side=side,entry=entry,reference_entry=b['c'],stop=stop,tick=tick,entry_ms=b['t']+FIVE,signal_ms=ts,zone=z,instrument=w['instrument'],context=context)
        cur=self.db.execute('INSERT INTO signals(setup,s,side,at,bucket,context,plan) VALUES(?,?,?,?,?,?,?)',(w['id'],s,side,ts,bucket,canonical(context),canonical(plan)));sid=cur.lastrowid;self.signal_count+=1
        self.event('signal',ts,signal_id=sid,symbol=s,context=context,baseline_plan=plan)
        if levels and 'footprint' in found and self.emit:
            self.emit(dict(kind='trial_footprint_evidence',receive_ms=ts,signal_id=sid,symbol=s,protocol_sha256=HASH,levels=levels,sha256=hashlib.sha256(canonical(levels).encode()).hexdigest()))
        for variant in VARIANTS:
            p=copy.deepcopy(plan);p.update(signal_id=sid,variant=variant,status='open',entry_fee=.0006,activated_ms=plan['entry_ms'])
            if variant=='wide_stop':
                if not context['wide_stop_context_complete']:p.update(status='excluded',reason='INCOMPLETE_5M_CONTEXT')
                else:
                    distance=max(abs(entry-stop),3*context['atr_5m']);p['stop']=(math.floor((entry-side*distance)/tick) if side==1 else math.ceil((entry-side*distance)/tick))*tick
            if variant in ('limit','limit_through'):
                p.update(status='pending',entry=z['high'] if side==1 else z['low'],reference_entry=z['high'] if side==1 else z['low'],entry_fee=.0002,activated_ms=(ts//MINUTE+1)*MINUTE,expires_ms=ts+30*MINUTE)
                q=self.quotes.get(s)
                if not q or ts-q['observed_ms']>3000:p['status']='not_filled';p['reason']='POST_ONLY_QUOTE_UNKNOWN'
                elif (p['entry']>=q['ask'] if side==1 else p['entry']<=q['bid']):p['status']='not_filled';p['reason']='POST_ONLY_MARKETABLE'
                p['fill_through_ticks']=1 if variant=='limit_through' else 0
            if variant=='arrival':
                if arrival_price is None:p.update(status='not_filled',reason='ARRIVAL_QUOTE_UNKNOWN')
                else:p.update(entry=adverse(arrival_price,side,tick,True),reference_entry=arrival_price,entry_ms=ts,activated_ms=(ts//MINUTE+1)*MINUTE,partial_tick_ms=ts,partial_tick_price=arrival_price,partial_ticks=0)
            self._prepare(p)
            self.db.execute('INSERT INTO trades VALUES(?,?,?,?,NULL)',(sid,variant,p['status'],canonical(p)))
            if p['status'] in ('open','pending'):
                if len(self.active)>=512:raise RuntimeError('TRIAL_ACTIVE_CAP')
                self.active[(sid,variant)]=p
        self.db.commit();self.maybe_seal(ts)
    def arrival_ticks(self,s,rows,received):
        # Only the partly elapsed entry minute needs ticks. Later full minutes
        # retain exactly the frozen candle-bound exit logic.
        for key,p in list(self.active.items()):
            if key[1]!='arrival' or p['symbol']!=s:continue
            for r in rows:
                t=int(r['T'])
                if not p['entry_ms']<=t<p['activated_ms'] or t<p['partial_tick_ms'] or t>received:continue
                price=float(r['p']);p['states']=[path_step(z,[p['partial_tick_price'],price],t,p) if not z['closed'] else z for z in p['states']]
                p.update(partial_tick_ms=t,partial_tick_price=price,partial_ticks=p['partial_ticks']+1)
                if all(z['closed'] for z in p['states']):self._finish(key,p,'closed',self._outcome(p),received);break
    @staticmethod
    def _prepare(p):
        side=p['side'];e=p['entry'];d=side*(e-p['stop']);tick=p['tick']
        if d<=0:p.update(status='not_filled',reason='INVALID_STOP_GEOMETRY');return
        p['distance']=d
        p['tp1']=(math.ceil((e+side*1.5*d)/tick-1e-8) if side==1 else math.floor((e+side*1.5*d)/tick+1e-8))*tick
        p['tp2']=(math.ceil((e+side*3*d)/tick-1e-8) if side==1 else math.floor((e+side*3*d)/tick+1e-8))*tick
        inst=p['instrument'];p['quantity']=math.floor(min(2.5/d,1000/e)/inst['step']+1e-8)*inst['step']
        p['size_minimum_met']=p['quantity']>=inst['minimum'] and p['quantity']*e>=inst['min_notional']
        p['states']=[state(p['stop'])];p['expected_minute']=p['activated_ms'];p['quality_flags']=[]
        p['filled']=p['status']=='open'
    def record(self,r):
        kind=r.get('kind');s=r.get('symbol');ts=r['receive_ms']
        if s not in self.minute_pending:return
        if kind in ('footprint_quality_error','footprint_late_trade'):
            minute=int(r.get('minute_ms',ts//MINUTE*MINUTE))
            self.event('source_quality',ts,symbol=s,minute_ms=minute,source_kind=kind,reason=r.get('reason'))
            # A later invalidation of the exact trigger footprint retracts a
            # flow-confirmed result. Price-only MSS/sweep evidence is tagged.
            for sid,ct in self.db.execute('SELECT id,context FROM signals WHERE s=? AND at>=? AND at<?',(s,minute,minute+FIVE+MINUTE)):
                context=json.loads(ct);context.setdefault('source_quality_flags',[]).append(dict(kind=kind,minute_ms=minute,reported_ms=ts))
                self.db.execute('UPDATE signals SET context=? WHERE id=?',(canonical(context),sid))
                if 'footprint' in context['confirmation_groups']:
                    for variant in VARIANTS:
                        key=(sid,variant)
                        if key in self.active:self._finish(key,self.active[key],'censored',dict(reason='TRIGGER_FOOTPRINT_INVALIDATED'),ts)
                        else:self.db.execute("UPDATE trades SET status='censored',result=? WHERE signal=? AND variant=?",(canonical(dict(reason='TRIGGER_FOOTPRINT_INVALIDATED')),sid,variant))
            self.db.commit();return
        if kind!='candle_1m':return
        if r.get('filled_in'):
            self.event('filled_in_candle',ts,symbol=s,minute=r['candle']['t'])
            for key,p in list(self.active.items()):
                if p['symbol']==s:self._finish(key,p,'censored',dict(reason='FILLED_IN_NOT_CAUSALLY_OBSERVED'),ts)
            self.minute_pending[s].clear();return
        b={k:float(r['candle'][k]) for k in ('o','h','l','c','v')};b.update(t=int(r['candle']['t']),available_ms=ts)
        if b['t']+MINUTE>ts:raise ValueError('FUTURE_CANDLE')
        old=self.last_minute.get(s)
        if old is not None and b['t']<=old:return
        if old is not None and b['t']!=old+MINUTE:
            self.event('candle_gap',ts,symbol=s,after_minute=old,next_minute=b['t']);self.minute_pending[s].clear()
        self.last_minute[s]=b['t'];q=self.minute_pending[s];q.append(b)
        if b['t']%FIVE==4*MINUTE and len(q)==5 and q[0]['t']==b['t']-4*MINUTE:
            self._bar(self.m5[s],dict(t=q[0]['t'],o=q[0]['o'],h=max(x['h'] for x in q),l=min(x['l'] for x in q),c=b['c'],v=sum(x['v'] for x in q),available_ms=ts))
        for key,p in list(self.active.items()):
            if key[1]=='arrival' and p['symbol']==s and b['t']>=p['activated_ms'] and not p['partial_ticks']:
                self._finish(key,p,'censored',dict(reason='ARRIVAL_PARTIAL_MINUTE_UNOBSERVED'),ts);continue
            if p['symbol']!=s or b['t']<p['activated_ms']:continue
            if b['t']!=p['expected_minute']:
                self._finish(key,p,'censored',dict(reason='CANDLE_GAP',next_minute=b['t'],expected_minute=p['expected_minute']),ts);continue
            if r.get('filled_in'):
                p['quality_flags'].append('FILLED_IN_CANDLE')
                self._finish(key,p,'censored',dict(reason='FILLED_IN_NOT_CAUSALLY_OBSERVED'),ts);continue
            if p['status']=='pending':
                if b['t']>=p['expires_ms']:
                    self._finish(key,p,'not_filled',dict(reason='LIMIT_TIMEOUT'),ts);continue
                # Limit path simulation never reuses extrema before first fill.
                branches=[]
                paths=[[b['o'],b['h'],b['l'],b['c']],[b['o'],b['l'],b['h'],b['c']]]
                if b['l']<=p['tp1']<=b['h']:paths.extend([[b['o'],p['tp1'],b['h'],b['l'],b['c']],[b['o'],p['tp1'],b['l'],b['h'],b['c']]])
                for points in paths:
                    filled=self._limit_path(p,points,b['t'])
                    if filled is not None:branches.append(filled)
                if not branches:
                    if p['side']*(b['c']-p['stop'])<=0:self._finish(key,p,'not_filled',dict(reason='STOP_INVALIDATED_BEFORE_FILL'),ts);continue
                    p['expected_minute']+=MINUTE;self._save(key,p);continue
                p.update(status='open',filled=True,entry_ms=b['t'],states=merge(branches),maker_fill_assumed=True)
            else:
                branches=[]
                for z in p['states']:
                    if z['closed']:branches.append(z);continue
                    paths=[[b['o'],b['h'],b['l'],b['c']],[b['o'],b['l'],b['h'],b['c']]]
                    if not z['tp1'] and b['l']<=p['tp1']<=b['h']:paths.extend([[b['o'],p['tp1'],b['h'],b['l'],b['c']],[b['o'],p['tp1'],b['l'],b['h'],b['c']]])
                    branches.extend(path_step(z,points,b['t'],p) for points in paths)
                p['states']=merge(branches)
            if b['t']+MINUTE>=p['entry_ms']+DAY:
                from paper_paths import execute
                p['states']=[z if z['closed'] else execute(z,'TIME_24H',b['c'],b['t'],p) for z in p['states']]
            p['expected_minute']=b['t']+MINUTE
            if all(z['closed'] for z in p['states']):self._finish(key,p,'closed',self._outcome(p),ts)
            else:self._save(key,p)
        self.db.commit();self.maybe_seal(ts)
    @staticmethod
    def _limit_path(p,points,t):
        e=p['entry'];side=p['side'];current=points[0]
        if p.get('fill_through_ticks'):
            trigger=e-side*p['tick']*p['fill_through_ticks']
            if side*(current-trigger)<=1e-10:return path_step(state(p['stop']),points,t,p)
            for i,(a,b) in enumerate(zip(points,points[1:])):
                if side*(b-trigger)<=1e-10:return path_step(state(p['stop']),[trigger,b]+points[i+2:],t,p)
            return None
        if (current<=e if side==1 else current>=e):return path_step(state(p['stop']),[e]+points,t,p)
        for i,(a,b) in enumerate(zip(points,points[1:])):
            if min(a,b)<=e<=max(a,b):return path_step(state(p['stop']),[e,b]+points[i+2:],t,p)
        return None
    def _save(self,key,p):self.db.execute('UPDATE trades SET status=?,payload=? WHERE signal=? AND variant=?',(p['status'],canonical(p),*key))
    def _finish(self,key,p,status,result,ts):
        result['filled']=p.get('filled',False)
        p['status']=status;self._save(key,p)
        self.db.execute('UPDATE trades SET result=? WHERE signal=? AND variant=?',(canonical(result),*key));self.active.pop(key,None)
        # Only compact lifecycle evidence; no whole-history payload in memory.
        self.event('trade_terminal',ts,signal_id=key[0],variant=key[1],status=status,result=result)
    @staticmethod
    def _outcome(p):
        values=[]
        for z in p['states']:
            reference_exit=sum(e['fraction']*e['reference'] for e in z['events'])
            gross=p['side']*(reference_exit-p['reference_entry'])/p['distance']
            execution=p['side']*(p['entry']-p['reference_entry']+reference_exit-z['exit_notional'])/p['distance']
            fees=(p['entry']*p['entry_fee']+z['exit_notional']*.0006)/p['distance'];funding=p['entry']*.001/p['distance']
            net=z['unit_gross']/p['distance']-fees-funding
            assert abs(gross-execution-fees-funding-net)<1e-7
            values.append(dict(gross_R=gross,execution_R=execution,fees_R=fees,funding_R=funding,cost_R=execution+fees+funding,net_R=net,exit_ms=z['closed_ms'],reason=z['reason'],tp1=z['tp1'],tp2=z['tp2']))
        lo=min(values,key=lambda v:v['net_R']);hi=max(values,key=lambda v:v['net_R'])
        return dict(lower=lo,upper=hi,risk_usdt=p['quantity']*p['distance'],notional_usdt=p['quantity']*p['entry'],quality_flags=p['quality_flags'],maker_fill_assumed=p.get('maker_fill_assumed',False))
    def boundary(self,ts,reason):
        for key,p in list(self.active.items()):self._finish(key,p,'censored',dict(reason=reason),ts)
        for s in self.symbols:self.watches[s].clear();self.minute_pending[s].clear()
        self.quotes.clear();self.funding.clear();self.last_minute.clear()
        for h in self.quote_history.values():h.clear()
        for h in self.funding_history.values():h.clear()
        self.event('boundary',ts,reason=reason)
    def maybe_seal(self,ts):
        if self.phase=='collecting' and self.started is not None and ts>=self.started+28*DAY and self.cluster_count()>=150:
            cutoff=self.db.execute('SELECT max(bucket) FROM signals').fetchone()[0]+FIVE
            if ts<cutoff:return
            self.phase='sealed';self.put('phase',self.phase);self.put('sealed_ms',ts);self.event('cohort_sealed',ts,signals=self.signal_count)
        if self.phase=='sealed' and not self.active:
            self.verify_clock();self._kill_running=True
            try:report=self.report(final=True)
            finally:self._kill_running=False
            self.db.execute('INSERT OR REPLACE INTO results VALUES(?,?)',('final',canonical(report)))
            self.put('kill_checked_ms',ts);self.phase='complete';self.put('phase',self.phase);self.event('final_release',ts,kill=report['kill'])
    def status(self):
        with self.lock:
            return dict(protocol_id=PROTOCOL['id'],protocol_sha256=HASH,started_ms=self.started,initialized_ms=self.initialized,holdout_start_ms=self.holdout_start(),clock_lock=self.meta('clock_lock'),phase=self.phase,
                error=self.halted,automatic_exchange_trading=False,deduplicated_signals=self.signal_count,
                temporal_cohorts=self.cluster_count(),time_clusters=self.cluster_count(),target_time_clusters=150,
                active_paper_trades=len(self.active),outcomes_withheld=self.phase not in ('complete',),filter_lock=self.meta('filter_lock'),amendment_first_signal_id=self.meta('amendment_first_signal_id'),
                database_bytes=self.path.stat().st_size,database_limit_bytes=64*1024*1024,
                warning='Deduplicated coin/direction/5m signals are not statistically independent; same-time cohorts remain correlated.')
    def snapshot(self,path):
        with self.lock:
            target=sqlite3.connect(path)
            try:self.db.backup(target,pages=64)
            finally:target.close()
    def write_exports(self,directory):
        with self.lock:
            root=Path(directory);protocol=root/'research-trial-protocol.json';report=root/'research-trial-report.json'
            protocol.write_text(canonical(dict(protocol=PROTOCOL,protocol_sha256=HASH,status=self.status())))
            report.write_text(canonical(dict(development=self.report(False),final=self.report(True) if self.phase=='complete' else {'locked':True})))
            self.verify_clock();paths=[protocol,report]
            audit=root/'research-trial-privacy-audit.json';audit.write_text(canonical(self.privacy_audit()));paths.append(audit)
            if self.clock_path.exists():paths.append(self.clock_path)
            if self.phase!='complete':return paths
            path=root/'research_trials_export.sqlite';self.snapshot(path);paths.append(path)
            path=root/'research-trial-trades.csv'
            fields=['signal_id','symbol','side','signal_ms','utc_hour','colombo_hour','regime','cohort_count_final','cohort_count_at_signal','variant','status','thin_stop','gross_R_lower','cost_R_lower','net_R_lower','gross_R_upper','cost_R_upper','net_R_upper','context_json','plan_json','result_json']
            with path.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
                for sid,s,side,ts,bucket,ct,plan,v,status,result in self.db.execute('SELECT s.id,s.s,s.side,s.at,s.bucket,s.context,t.payload,t.variant,t.status,t.result FROM signals s JOIN trades t ON s.id=t.signal ORDER BY s.at,s.id,t.variant'):
                    c=json.loads(ct);z=json.loads(result) if result else {};n=self.db.execute('SELECT count(*) FROM signals WHERE bucket=?',(bucket,)).fetchone()[0]
                    row=dict(signal_id=sid,symbol=s,side=side,signal_ms=ts,utc_hour=c['utc_hour'],colombo_hour=c['colombo_hour'],regime=c['regime'],cohort_count_final=n,cohort_count_at_signal=c['concurrent_count_at_signal'],variant=v,status=status,thin_stop=c['thin_stop'],context_json=ct,plan_json=plan,result_json=result)
                    for bound in ('lower','upper'):
                        for k in ('gross_R','cost_R','net_R'):row[k+'_'+bound]=z.get(bound,{}).get(k)
                    writer.writerow(row)
            paths.append(path);return paths
    def report(self,final=False):
        self.verify_clock()
        if self.phase=='collecting':return dict(locked=True,reason='COHORT_COLLECTING_FIXED_TIME_HOLDOUT',status=self.status())
        if final and self.phase!='complete' and not self._kill_running:return dict(locked=True,reason='KILL_CHECK_NOT_RUN')
        if final and (self.active or self.meta('filter_lock')['hash']!=HASH):return dict(locked=True,reason='FINAL_PENDING_OR_RULE_LOCK_MISMATCH')
        test_start=self.holdout_start()
        if test_start is None:return dict(locked=True,reason='NO_FIRST_SIGNAL')
        # Fixed receipt-time boundary; 24h purge removes overlapping development trades.
        if final:where='s.at>=?';args=(test_start,)
        else:where='s.at+?<?';args=(DAY,test_start)
        groups=defaultdict(list);counts=Counter();signal_ids=set();cohorts=set()
        query='SELECT s.id,s.at,s.bucket,s.s,s.side,s.context,t.variant,t.status,t.result,t.payload FROM signals s JOIN trades t ON s.id=t.signal WHERE '+where+' ORDER BY s.at,s.id'
        for sid,at,bucket,s,side,text,variant,status,outcome,payload in self.db.execute(query,args):
            c=json.loads(text);signal_ids.add(sid);cohorts.add(bucket);counts[(variant,status)]+=1
            result=json.loads(outcome) if outcome else None
            item=dict(signal_id=sid,cohort=bucket,status=status,result=result)
            plan=json.loads(payload);width_pct=plan['distance']/plan['entry'] if plan.get('distance') else c['stop_distance_pct']
            width='under_0.6pct' if width_pct<.006 else '0.6_to_1pct' if width_pct<.01 else 'at_least_1pct'
            for dimension,key in [('all','all'),('regime',c['regime']),('direction','long' if side==1 else 'short'),('hour_block_utc',c['hour_block_utc']),('stop_width',width),('coin',s)]:groups[(variant,dimension,key)].append(item)
        metrics={}
        for (variant,dimension,key),items in groups.items():metrics.setdefault(variant,{}).setdefault(dimension,{})[key]=metrics_for(items)
        kill={}
        if final:
            for v in VARIANTS:
                m=metrics.get(v,{}).get('all',{}).get('all',{})
                kill[v]=('STOP_INCONCLUSIVE_DATA' if not m.get('closed') or m.get('censored') else 'STOP' if m['lower']['net_R']<=0 or m['lower']['gross_R']<0 else 'NO_KILL_TRIGGER')
        return dict(locked=False,partition='final_test' if final else 'purged_development',protocol_sha256=HASH,filters=self.meta('filter_lock'),test_start_ms=test_start,deduplicated_signals=len(signal_ids),independent_signals=len(cohorts),temporal_cohorts=len(cohorts),time_clusters=len(cohorts),target_time_clusters=150,metrics=metrics,kill=kill,
            caveats=['Deduplicated signals retain cross-coin/time dependence.','Overlapping independently funded probes are not a capital-constrained account.','Maker touch assumes a fill; queue and actual fee tier are unknown.','One final test compares five predeclared variants; choosing a winner still requires fresh validation.'])

def metrics_for(items):
    closed=[r for r in items if r['status']=='closed'];n=len({r['signal_id'] for r in items});c=len(closed)
    clusters=len({r['cohort'] for r in items})
    out=dict(deduplicated_signals=n,independent_signals=clusters,temporal_cohorts=clusters,time_clusters=clusters,closed=c,filled=sum(r['status'] in ('closed','open') or (r['result'] or {}).get('filled',False) for r in items),not_filled=sum(r['status']=='not_filled' for r in items),excluded=sum(r['status']=='excluded' for r in items),censored=sum(r['status']=='censored' for r in items),
        sample_warning=clusters<30 or c<30)
    out['fill_rate']=out['filled']/n if n else None
    for bound in ('lower','upper'):
        values=sorted((r['result'][bound] for r in closed),key=lambda x:x['exit_ms'])
        equity=peak=dd=0.
        for v in values:equity+=v['net_R'];peak=max(peak,equity);dd=max(dd,peak-equity)
        gain=sum(max(0,v['net_R']) for v in values);loss=sum(max(0,-v['net_R']) for v in values)
        out[bound]=dict(win_rate=sum(v['net_R']>0 for v in values)/c if c else None,profit_factor=gain/loss if loss else None,
            profit_factor_note='No losses; undefined/infinite PF' if c and not loss else None,
            expectancy=sum(v['net_R'] for v in values)/c if c else None,max_drawdown_R=dd if c else None,
            net_R=sum(v['net_R'] for v in values),gross_R=sum(v['gross_R'] for v in values),cost_R=sum(v['cost_R'] for v in values),
            fees_R=sum(v['fees_R'] for v in values),funding_R=sum(v['funding_R'] for v in values),execution_R=sum(v['execution_R'] for v in values))
    return out
