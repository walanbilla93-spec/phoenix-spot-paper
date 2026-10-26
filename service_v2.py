"""Three independent PAPER_ONLY portfolios; all public HTTP endpoints are read-only."""
import copy
import concurrent.futures
import gzip
import tempfile
import re
from settings_model import SETTING_FIELDS,change_settings
from settings_auth import check_admin
import os
import pathlib
import shutil
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from phoenix import *
from rotation import *
from selector import scan
from refill import entry_pool,recent_setup,refill_inputs,apply_refill_allocation,daily_activity,install_activity_policy,ACTIVITY_HASH,ACTIVITY_POLICY

ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path(os.environ.get('DATA_DIR','/data'))
LOCK=threading.RLock()
DBS={'fixed':None,'weekly':None,'weekly_five':None}
STATUSES={mode:dict(mode='PAPER_ONLY',portfolio=mode,real_orders_enabled=False,
    account_eligibility='UNVERIFIED',ready=False,persistence=False,symbols={},last_poll=None,
    errors=0,version='phoenix-paper-6-memory') for mode in DBS}
SELECTOR=dict(status='warming',completed=0,total=len(RESEARCH_COHORT),selection_hash=SELECTION_HASH)
MARKET=dict(status='warming',symbols={},last_success_at=None)
DISPLAY_MAX_AGE=30000
SETUPS={}
# Only one history response may be built or sent at a time. A slow export
# must not multiply buffers across the HTTP server's otherwise unbounded threads.
READ_RESPONSE=threading.BoundedSemaphore(1)

def memory_usage():
    try:
        fields=(pathlib.Path('/proc/self/status').read_text()).splitlines()
        return {line.split(':')[0]:int(line.split()[1])*1024 for line in fields if line.startswith(('VmRSS:','VmHWM:'))}
    except (OSError,ValueError):return {}

def trade_evidence(db,record):
    """Fetch immutable proof for one record; never retain books in the journal."""
    payload=db.db.execute('SELECT payload FROM events WHERE id=?',(record['ledger_id'],)).fetchone()
    if payload is None:raise ValueError('missing execution evidence')
    index=int(record['record_id'].rsplit(':',1)[1])
    return dict(record,evidence=json.loads(payload[0])['events'][index])

def response_chunks(result,evidence_db=None):
    """Encode incrementally, including full-history evidence exports."""
    encoder=json.JSONEncoder(sort_keys=True,separators=(',',':'),allow_nan=False)
    if evidence_db is None:
        yield from encoder.iterencode(result);return
    yield '{'
    for index,(key,value) in enumerate(result.items()):
        if index:yield ','
        yield from encoder.iterencode(key);yield ':'
        if key!='records':yield from encoder.iterencode(value);continue
        yield '['
        for n,record in enumerate(value):
            if n:yield ','
            with LOCK:expanded=trade_evidence(evidence_db,record)
            yield from encoder.iterencode(expanded)
            del expanded
        yield ']'
    yield '}'

def setup_worker():
    client=PublicSpot(tuple(set(SYMBOLS+RESEARCH_COHORT)));updated={}
    while True:
        with LOCK:
            fixed=DBS['fixed'];weekly=DBS['weekly']
            pool=set(entry_pool(fixed.state,'fixed')) if fixed else set(CORE)
            if weekly and weekly.state.get('selection'):
                pool.update(r['symbol'] for r in ranking(weekly.state['selection']['metrics']))
        at=now_ms();needed=[s for s in pool if updated.get(s)!=at//MINUTE]
        activity={}
        if needed:
            try:
                result,meta=client.get('tickers',None);activity=daily_activity(result,meta)
            except Exception:pass
        def acquire(s):
            result,meta=client.get('kline',s,interval=1,limit=32)
            return recent_setup(rows(result),meta['received_at'],activity.get(s))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            jobs={executor.submit(acquire,s):s for s in needed}
            for future in concurrent.futures.as_completed(jobs):
                s=jobs[future]
                try:
                    plan=future.result()
                    with LOCK:SETUPS[s]=plan
                    updated[s]=(plan['bar_at']+MINUTE)//MINUTE
                except Exception:
                    with LOCK:SETUPS[s]=dict(ready=False,reason='Waiting for fresh candidate data')
        time.sleep(3)

def parse_tickers(result,meta):
    """Display-only spot snapshots. Never used for signals, marks or execution."""
    if result.get('category')!='spot':raise ValueError('non-spot ticker result')
    received=meta['received_at'];server=meta.get('server_at')
    if not isinstance(server,(int,float)) or not math.isfinite(server) or not 0<=received-server<=DISPLAY_MAX_AGE:
        raise ValueError('missing/stale/future ticker snapshot time')
    allowed=set(SYMBOLS+RESEARCH_COHORT);output={}
    for x in result.get('list',[]):
        s=x.get('symbol')
        if s not in allowed:continue
        if s in output:raise ValueError('duplicate spot ticker')
        try:
            last,previous,change=(float(x[k]) for k in ('lastPrice','prevPrice24h','price24hPcnt'))
            if not all(math.isfinite(v) for v in (last,previous,change)) or min(last,previous)<=0:raise ValueError()
            output[s]=dict(last_price=last,previous_price_24h=previous,change_24h_pct=change*100,
                snapshot_at=server,received_at=received,status='ok')
        except (ValueError,KeyError,TypeError):output[s]=dict(status='unavailable')
    if not output:raise ValueError('no approved spot tickers')
    return output

def market_view(snapshot,symbols,at):
    result={}
    for s in symbols:
        q=copy.deepcopy(snapshot['symbols'].get(s,dict(status='unavailable')))
        q['snapshot_age_ms']=at-q['snapshot_at'] if q.get('snapshot_at') is not None else None
        q['stale']=snapshot['status']!='ok' or q['snapshot_age_ms'] is None or not 0<=q['snapshot_age_ms']<=DISPLAY_MAX_AGE
        result[s]=q
    return result

def market_worker():
    client=PublicSpot(tuple(set(SYMBOLS+RESEARCH_COHORT)))
    while True:
        try:
            result,meta=client.get('tickers',None);tickers=parse_tickers(result,meta)
            with LOCK:MARKET.update(status='ok',symbols=tickers,last_success_at=meta['received_at'])
        except Exception as exc:
            with LOCK:MARKET.update(status='unavailable')
            print(canonical(dict(type='display_ticker_failure',error=type(exc).__name__,mode='PAPER_ONLY')),flush=True)
        time.sleep(10)

def rows(result):return sorted([[int(r[0])]+[float(v) for v in r[1:7]] for r in result['list']],key=lambda r:r[0])

def active_symbols(st,mode):
    if mode=='fixed':return list(CORE)+([st['replacement']['symbol']] if st.get('replacement') else [])
    selected=selected_symbols(st,mode)
    legacy=[s for s,p in st['positions'].items() if p['active'] or p['pending'] or p['qty']>0]
    return sorted(set(selected+legacy))

def selected_symbols(st,mode):
    if mode=='fixed':return list(CORE)+([st['replacement']['symbol']] if st.get('replacement') else [])
    if mode=='weekly_five' and st.get('cycle_entry_policy_hash')==CYCLE_ENTRY_HASH:
        return st.get('refill_allocation',{}).get('selected',[])
    return st['selection']['selected'] if st.get('selection') else []

def summary(mode='fixed'):
    at=now_ms()
    with LOCK:
        db=DBS[mode];state=copy.deepcopy(db.state) if db else (initial() if mode=='fixed' else weekly_initial(mode))
        health=copy.deepcopy(STATUSES[mode]);selector=copy.deepcopy(SELECTOR);market=copy.deepcopy(MARKET);setups=copy.deepcopy(SETUPS)
    equity=state['cash'];basis=0;selected=selected_symbols(state,mode)
    for s,p in state['positions'].items():
        p['mark_age_ms']=at-p['mark_at'] if p['mark_at'] else None
        p['mark_stale']=p['mark_age_ms'] is None or p['mark_age_ms']>MAX_AGE
        p['marked_value']=p['qty']*(p['mark'] or 0);p['floating_pnl']=p['marked_value']-p['basis']
        p['cycle_age_ms']=max(0,at-p['entry_at']) if p['active'] else 0
        p['cycle_age_days']=p['cycle_age_ms']/86400000
        p['underwater_days']=(at-p['underwater_since'])/86400000 if p['underwater_since'] else 0
        p['ladder_exhausted']=p['purchases']>=15;p['selected']=s in selected
        p['entry_reason']=('Vacant slot; waiting for a fresh ask within 1% of the prior 24h low' if mode=='weekly_five' and not p['active'] and s in selected and not health.get('new_buys_halted',True) else
            'Replaced by '+state['replacement']['symbol']+'; no spot instrument' if s=='XMRUSDT' and state.get('replacement')
            else entry_reason(p,health['symbols'].get(s,{}),s in selected,health.get('new_buys_halted',True)))
        if state.get('cycle_entry_policy_hash')==CYCLE_ENTRY_HASH and not p['active']:
            plan=setups.get(s,{})
            p['entry_reason']=('New entries paused: execution data unavailable' if health.get('new_buys_halted',True) else
                'Fresh-cycle buy queued; checking executable asks' if p['pending'] else
                'New cycles paused in Settings' if not account_settings(state)['entries_enabled'] else
                'Post-exit cooldown: two minutes' if p['last_exit'] and at<p['last_exit']+2*MINUTE else
                'Outside current vacancy allocation' if s not in selected else plan.get('reason','Waiting for fresh current-minute setup'))
        equity+=p['marked_value'];basis+=p['basis']
    health['ready']=bool(health['ready'] and health['last_poll'] and at-health['last_poll']<30000)
    if mode!='fixed' and (not state.get('selection') or state['selection']['boundary']!=week_boundary(at)):
        health['ready']=False;health['new_buys_halted']=True
    for telem in health['symbols'].values():
        if telem.get('quote_at'):telem['quote_age_ms']=at-telem['quote_at']
    return dict(health=health,state=state,portfolio=mode,portfolios=list(DBS),
        market=market_view(market,state['positions'],at),market_status=market['status'],
        market_notice='Bybit spot last-traded price and rolling 24h change; display only, never an executable fill quote.',
        portfolio_notice='Independent simulated accounts: 1000 USDT each. View selection never transfers funds or resets positions.',
        selected_symbols=selected,selector=selector,selection_config=SELECTION_CONFIG,
        cycle_entry_policy=CYCLE_ENTRY_POLICY,cycle_entry_policy_hash=state.get('cycle_entry_policy_hash'),
        activity_policy=ACTIVITY_POLICY,activity_policy_hash=state.get('activity_policy_hash'),
        entry_pool=entry_pool(state,mode),entry_setups={s:setups.get(s,{}) for s in entry_pool(state,mode)},
        equity=equity,basis=basis,floating_pnl=equity-state['cash']-basis,
        completed_cycles=sum(p['cycles'] for p in state['positions'].values()),
        equity_marks_stale=any(p['mark_stale'] and p['qty']>0 for p in state['positions'].values()),
        reserve_warning='250 USDT is a cash acquisition constraint, never a guaranteed equity floor.',
        execution_warning='Visible-book hypothetical IOC fills; account eligibility and actual matching unverified.',
        persistence_free_bytes=shutil.disk_usage(DATA).free if DATA.exists() else None,
        config=CONFIG,config_hash=CONFIG_HASH,execution_policy=EXECUTION_POLICY,
        execution_policy_hash=state.get('execution_policy_hash'),selection_config_hash=SELECTION_HASH,at=at)

def position_details(mode,symbol):
    """Read-only history from the full ledger, not the truncated recent-events endpoint."""
    with LOCK:
        db=DBS[mode]
        if db is None or symbol not in db.state['positions']:return None
        p=copy.deepcopy(db.state['positions'][symbol]);trades=[]
        # Share the incremental journal with the Live Ledger instead of rescanning history.
        for r in execution_ledger(mode,include_evidence=False,filled_only=True,journal_only=True)['records']:
            if r['symbol']!=symbol:continue
            trade=dict(r,current_cycle=bool(p['active'] and r['at']>=p['entry_at']),realized=r['realized_pnl'])
            trades.append(trade)
    avg=p['basis']/p['qty'] if p['qty'] else None;cfg=cycle_settings(p)
    return dict(symbol=symbol,portfolio=mode,position=p,trades=trades,average_cost=avg,
        tp_arm_price=avg*(1+cfg['tp_arm_pct']/100) if avg else None,net_break_even=avg/(1-FEE) if avg else None,
        at=now_ms(),time_zone='Asia/Colombo',fee_assumption=FEE,mode='PAPER_ONLY')

def execution_ledger(mode,include_evidence=True,record_id=None,filled_only=False,journal_only=False):
    """Full attempt/fill journal, with stable paper identities and original execution evidence."""
    with LOCK:
        db=DBS[mode]
        if db is None:return dict(mode='PAPER_ONLY',portfolio=mode,records=[],ready=False,at=now_ms())
        cache=getattr(db,'_trade_journal',None)
        if cache is None:cache=dict(through=0,records=[],cycles={})
        records=cache['records'];cycles=cache['cycles']
        head=db.db.execute('SELECT id,hash FROM events ORDER BY id DESC LIMIT 1').fetchone()
        for i,_,payload,_,digest in db.event_batches(cache['through'],head[0] if head else 0,attempts_only=True):
            row=json.loads(payload)
            for index,e in enumerate(row['events']):
                if e.get('type')!='attempt':continue
                fill=e['fill'];q=fill['qty'];buy=e['side']=='buy';s=e['symbol'];order=e['order'];fee=e.get('fee',0)
                oid='paper-order:'+mode+':'+hashlib.sha256(canonical([s,e['side'],order['signal_at'],order['observed_at']]).encode()).hexdigest()[:20]
                if buy and q>0 and e.get('buy_number')==1:cycles[s]='paper-cycle:'+mode+':'+str(i)
                records.append(dict(ledger_id=i,ledger_hash=digest,record_id=mode+':'+str(i)+':'+str(index),
                    paper_order_id=oid,paper_fill_id='paper-fill:'+mode+':'+str(i)+':'+str(index) if q>0 else None,
                    paper_cycle_id=cycles.get(s),cycle_closed=bool(not buy and q>0 and not row['after']['positions'][s]['active']),exchange_order_id=None,portfolio=mode,symbol=s,side=e['side'],at=e['at'],
                    status=fill['status'],filled=q>0,price=fill['price'],gross_quantity=q,
                    net_quantity=q*(1-FEE) if buy else q,usdt=fill['notional'],requested_usdt=e.get('budget') if buy else None,
                    fee_usdt_equivalent=fee,fee_currency=s[:-4] if buy else 'USDT',fee_quantity=q*FEE if buy else fee,
                    buy_number=e.get('buy_number'),realized_pnl=e.get('realized',0),reason=order['reason'],
                    signal_at=order['signal_at'],observed_at=order['observed_at'],latency_ms=e['latency_ms'],
                    quote_at=fill.get('quote_at',e['book']['ts']),quote_age_ms=e['at']-e['book']['ts'],
                    max_buy_price=e.get('max_buy_price'),min_sell_price=e.get('min_sell_price'),
                    fill_source='SIMULATED_PUBLIC_BOOK'))
                if not buy and q>0 and not row['after']['positions'][s]['active']:cycles.pop(s,None)
        cache['through']=head[0] if head else 0;db._trade_journal=cache
        if record_id is not None or journal_only:
            chosen=[r for r in records if (record_id is None or r['record_id']==record_id) and (not filled_only or r['filled'])]
            chosen=[trade_evidence(db,r) for r in chosen] if include_evidence else [dict(r) for r in chosen]
            return dict(mode='PAPER_ONLY',portfolio=mode,records=chosen,ready=True,at=now_ms(),time_zone='Asia/Colombo')
        all_records=records;st=db.state
        totals=dict(cash=st['cash'],fees=st['fees'],realized_pnl=st['realized'],
            completed_cycles=sum(p['cycles'] for p in st['positions'].values()),
            buy_fills=sum(r['filled'] and r['side']=='buy' for r in all_records),
            sell_fills=sum(r['filled'] and r['side']=='sell' for r in all_records),
            rejections=sum(not r['filled'] for r in all_records))
        coins=[];quotes=market_view(MARKET,st['positions'],now_ms())
        for symbol,p in sorted(st['positions'].items()):
            trades=[r for r in all_records if r['symbol']==symbol];fills=[r for r in trades if r['filled']]
            if not (p['active'] or p['qty'] or trades):continue
            avg=p['basis']/p['qty'] if p['qty'] else None;q=quotes[symbol];cfg=cycle_settings(p,st)
            coins.append(dict(symbol=symbol,active=p['active'],quantity=p['qty'],basis=p['basis'],average_cost=avg,
                purchases=p['purchases'],cycles=p['cycles'],entry_at=p['entry_at'],last_exit=p['last_exit'],
                current_price=q.get('last_price'),change_24h_pct=q.get('change_24h_pct'),price_stale=q['stale'],
                floating_pnl=p['qty']*(p['mark'] or 0)-p['basis'] if p['mark'] is not None else None,
                realized_pnl=sum(r['realized_pnl'] for r in fills),fees=sum(r['fee_usdt_equivalent'] for r in fills),
                buy_fills=sum(r['side']=='buy' for r in fills),sell_fills=sum(r['side']=='sell' for r in fills),
                rejected_attempts=sum(not r['filled'] for r in trades),last_purchase_price=p['last_fill'],
                dca_trigger=p['threshold'] if p['active'] and p['purchases']<15 else None,
                strategy_settings=cfg,tracked_low=p['trough'],tp_arm_price=avg*(1+cfg['tp_arm_pct']/100) if avg and p['active'] else None,
                tracked_peak=p['peak'],trailing_exit_level=p['peak']*(1-cfg['tp_callback_pct']/100) if p['peak'] is not None else None,
                activity={k:v for k,v in SETUPS.get(symbol,{}).get('activity',{}).items() if k!='window'},
                pending_side=p['pending']['side'] if p['pending'] else None))
        cycle_summaries={}
        for r in all_records:
            cid=r['paper_cycle_id']
            if not cid:continue
            c=cycle_summaries.setdefault(cid,dict(cycle_id=cid,symbol=r['symbol'],started_at=r['at'],closed_at=None,
                status='Open',buy_fills=0,sell_fills=0,spent=0,net_proceeds=0,fees=0,realized_pnl=0))
            if r['filled']:
                c['buy_fills' if r['side']=='buy' else 'sell_fills']+=1
                c['spent']+=r['usdt'] if r['side']=='buy' else 0
                c['net_proceeds']+=r['usdt']-r['fee_usdt_equivalent'] if r['side']=='sell' else 0
                c['fees']+=r['fee_usdt_equivalent'];c['realized_pnl']+=r['realized_pnl']
                if r['cycle_closed']:c.update(status='Completed',closed_at=r['at'])
        records=[r for r in all_records if (record_id is None or r['record_id']==record_id) and (not filled_only or r['filled'])]
        records=[trade_evidence(db,r) for r in records] if include_evidence else [dict(r) for r in records]
    return dict(mode='PAPER_ONLY',portfolio=mode,records=records,totals=totals,coins=coins,cycles=list(cycle_summaries.values()),
        ledger_head=head[1] if head else '0'*64,ready=True,at=now_ms(),time_zone='Asia/Colombo')

def selection_worker():
    while True:
        try:
            with LOCK:db=DBS['weekly'];fixed=DBS['fixed']
            if db is None or fixed is None:time.sleep(2);continue
            boundary=week_boundary(now_ms())
            with LOCK:old=copy.deepcopy(db.state.get('selection'))
            if old and old['boundary']==boundary:snapshot=old
            else:
                with LOCK:SELECTOR.update(status='acquiring',boundary=boundary,completed=0)
                def progress(n,failures):
                    with LOCK:SELECTOR.update(completed=n,failures=copy.deepcopy(failures))
                snapshot=scan(db,LOCK,boundary,progress)
                with LOCK:db.apply('weekly-selection:'+str(boundary),lambda st:apply_selection(st,snapshot,'weekly'))
            with LOCK:
                five=DBS['weekly_five']
                if five is not None:five.apply('weekly-selection:'+str(boundary),lambda st:apply_selection(st,snapshot,'weekly_five'))
                if not fixed.state.get('replacement'):fixed.apply('fixed-fifth-amendment:'+str(boundary),lambda st:apply_selection(st,snapshot,'fixed'))
                SELECTOR.update(status='ready',completed=len(RESEARCH_COHORT),boundary=boundary,selected=snapshot['selected'],
                                next_rotation_at=snapshot['next_rotation_at'],failures={})
            time.sleep(20)
        except Exception as exc:
            with LOCK:SELECTOR.update(status='acquisition_failed',error=str(exc)[:240])
            print(canonical(dict(type='selector_failure',error=type(exc).__name__,mode='PAPER_ONLY')),flush=True);time.sleep(30)

def worker(mode='fixed'):
    database_url=None;db=None
    try:
        if os.environ.get('PAPER_ONLY','true').lower()!='true':raise ValueError('PAPER_ONLY is immutable')
        if any(os.environ.get(k) for k in ('BYBIT_API_KEY','BYBIT_API_SECRET','EXCHANGE_API_KEY','EXCHANGE_API_SECRET')):raise ValueError('exchange credentials refused')
        DATA.mkdir(parents=True,exist_ok=True);database_url=os.environ.get('DATABASE_URL')
        persistent=bool(database_url) or os.path.ismount(DATA)
        if not persistent and os.environ.get('LOCAL_PAPER_TEST')!='1':raise ValueError('durable storage not connected; paper processing disabled')
        if not database_url:
            import platform
            lockfile=open(DATA/(mode+'-worker.lock'),'a+')
            if platform.system()=='Windows':
                import msvcrt
                lockfile.seek(0);lockfile.write('1');lockfile.flush();lockfile.seek(0);msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with LOCK:
            db=Store(database_url or str(DATA/('phoenix.sqlite3' if mode=='fixed' else mode+'.sqlite3')),
                factory=(lambda:weekly_initial(mode)) if mode!='fixed' else initial,validator=weekly_audit if mode!='fixed' else audit,
                schema='phoenix_'+mode if mode!='fixed' and database_url else None)
            DBS[mode]=db;STATUSES[mode].update(persistence=persistent,storage='managed_postgresql' if database_url else 'sqlite',ledger_head=db.verified_head)
            print(canonical(dict(type='ledger_verified',portfolio=mode,head=db.verified_head,memory_bytes=memory_usage())),flush=True)
            db.apply('execution-policy:'+EXECUTION_HASH,lambda st:install_execution_policy(st,now_ms()))
            db.apply('cycle-entry-policy:'+CYCLE_ENTRY_HASH,lambda st:install_cycle_entry_policy(st,now_ms()))
            if mode=='weekly_five':db.apply('activity-policy:'+ACTIVITY_HASH,lambda st:install_activity_policy(st,now_ms()))
        client=PublicSpot(tuple(set(SYMBOLS+RESEARCH_COHORT)));rules={};hours={};features={};minute={};refresh={}
        while True:
            cycle_at=now_ms();ok={};books={};telemetry={};failures={}
            if mode=='weekly_five':
                with LOCK:
                    inputs=refill_inputs(db.state,mode,SETUPS,cycle_at)
                    if db.state.get('selection'):
                        allocation_key='refill-allocation:'+hashlib.sha256(canonical(inputs).encode()).hexdigest()
                        db.apply(allocation_key,lambda state:apply_refill_allocation(state,inputs,cycle_at))
            with LOCK:st=copy.deepcopy(db.state);symbols=active_symbols(st,mode);selected=selected_symbols(st,mode)
            schedule_ok=mode=='fixed' or bool(st.get('selection') and st['selection']['boundary']==week_boundary(cycle_at))
            for s in symbols:
                try:
                    if s not in rules or cycle_at-rules[s]['at']>=1800000:
                        result,meta=client.get('instruments-info',s);rules[s]=parse_rule(result,s,meta['received_at'])
                    if s not in hours or (cycle_at-300000)//HOUR!=refresh.get('hour:'+s):
                        result,meta=client.get('kline',s,interval=60,limit=1000)
                        complete=[r for r in rows(result) if r[0]+HOUR+300000<=meta['received_at']]
                        with LOCK:hours[s]=db.hourly(s,complete)
                        features[s]=feature(hours[s],meta['received_at']);refresh['hour:'+s]=(cycle_at-300000)//HOUR
                    if s not in minute or cycle_at//MINUTE!=refresh.get('minute:'+s):
                        result,meta=client.get('kline',s,interval=1,limit=3)
                        complete=[r for r in rows(result) if r[0]+MINUTE<=meta['received_at']]
                        if not complete:raise ValueError('no completed minute')
                        minute[s]=complete[-1]
                        if minute[s][0]+MINUTE>cycle_at-MINUTE:refresh['minute:'+s]=cycle_at//MINUTE
                    if now_ms()-(minute[s][0]+MINUTE)>=90000:raise ValueError('minute data stale')
                    if now_ms()-features[s]['at']>=HOUR:raise ValueError('hour data stale')
                    book,meta=client.get('orderbook',s,limit=50);book['received_at']=meta['received_at']
                    bid,ask=valid_book(book,now_ms(),s);books[s]=book;ok[s]=True
                    telemetry[s]=dict(status='spot_public_ok',bid=bid,ask=ask,quote_at=book['ts'],received_at=meta['received_at'],rtt_ms=meta['rtt_ms'],
                        spread_bps=(ask-bid)/((bid+ask)/2)*10000,signal_bar_close=minute[s][0]+MINUTE,feature=features[s],
                        depth_probes={str(n):marketable(book,rules[s],'buy',n,now_ms()) for n in (7,10,16)},
                        exit_150_probe=marketable(book,rules[s],'sell',150/bid,now_ms()))
                except Exception as exc:failures[s]=str(exc)[:180];telemetry[s]=dict(status='data_failure',error=str(exc)[:180])
            all_ok=bool(symbols) and all(ok.get(s) for s in symbols)
            for s in symbols:
                if s not in books:continue
                try:
                    book,meta=client.get('orderbook',s,limit=50);book.update(received_at=meta['received_at'],request_at=meta['request_at'],rtt_ms=meta['rtt_ms'])
                    with LOCK:
                        # Read the clock after any storage/reader wait, not before it.
                        # A delayed snapshot must expire rather than become a backdated fill.
                        at=now_ms();valid_book(book,at,s)
                        p=db.state['positions'][s]
                        plan=copy.deepcopy(SETUPS.get(s,{}))
                        ready_entry=not p['active'] and p['last_bar']!=minute[s][0] and fresh_entry_valid(p,plan,minute[s],at)
                        need_commit=(ready_entry or p['last_observation_bar']!=minute[s][0] or (p['pending'] and at//MINUTE>p['pending'].get('last_attempt_bar',-1)) or at-(p['last_observation_at'] or 0)>=MINUTE)
                        if not need_commit:continue
                        entry_ok=schedule_ok and s in selected and account_settings(db.state)['entries_enabled'];key=f"{s}:{book['ts']}:{book.get('u')}:{at//5000}"
                        db.apply(key,lambda state:advance(state,s,minute[s],features[s],book,rules[s],at,allow_buy=all_ok,allow_entry=entry_ok,force_entry=mode=='weekly_five',entry_plan=plan))
                        STATUSES[mode]['ledger_head']=db.db.execute('SELECT hash FROM events ORDER BY id DESC LIMIT 1').fetchone()[0]
                except Exception as exc:all_ok=False;failures[s]=str(exc)[:180];telemetry[s]=dict(status='data_failure',error=str(exc)[:180])
            with LOCK:
                STATUSES[mode].update(ready=all_ok and schedule_ok,symbols=telemetry,last_poll=now_ms(),errors=STATUSES[mode]['errors']+len(failures),
                    new_buys_halted=not all_ok or not schedule_ok,fifth_slot=db.state.get('replacement',{}).get('symbol','selecting replacement') if mode=='fixed' else 'weekly top-five allocation',
                    persistent_db_bytes=db.db.execute('SELECT pg_database_size(current_database())').fetchone()[0] if database_url else (DATA/('phoenix.sqlite3' if mode=='fixed' else mode+'.sqlite3')).stat().st_size)
            print(canonical(dict(type='poll',portfolio=mode,at=now_ms(),mode='PAPER_ONLY',ready=all_ok and schedule_ok,failures=failures,observations=db.state['observations'],cash=db.state['cash'])),flush=True)
            time.sleep(max(.25,5-time.time()%5))
    except Exception as exc:
        error='storage/worker startup failure (connection details withheld)' if database_url else str(exc)[:240]
        with LOCK:STATUSES[mode].update(ready=False,new_buys_halted=True,fatal=error,error_class=type(exc).__name__,error_code=getattr(exc,'sqlstate',None))
        print(canonical(dict(type='fatal',portfolio=mode,error=error,error_class=type(exc).__name__,error_code=getattr(exc,'sqlstate',None),mode='PAPER_ONLY')),flush=True)

def settings_view(mode):
    with LOCK:
        db=DBS[mode];st=db.state if db else {}
        return dict(portfolio=mode,ready=db is not None,values=account_settings(st),defaults=DEFAULT_SETTINGS,
            revision=st.get('settings_revision',0),fields=SETTING_FIELDS,history=copy.deepcopy(st.get('settings_history',[])),
            admin_configured=bool(os.environ.get('PHOENIX_ADMIN_PASSWORD_HASH')),
            open_cycles=[dict(symbol=s,values=cycle_settings(p,st)) for s,p in st.get('positions',{}).items() if p['active']],
            immutable=dict(paper_only=True,starting_cash=1000,reserve=250,coin_basis_cap=150,total_basis_cap=750,max_slots=5,
                ladder=list(LADDER),fee_pct=FEE*100,quote_max_age_ms=MAX_AGE,slippage_bps=MAX_SLIP*10000),at=now_ms())

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        url=urllib.parse.urlsplit(self.path)
        if url.path!='/api/settings':self.send_error(501);return
        mode=urllib.parse.parse_qs(url.query).get('mode',['fixed'])[0]
        code=400;response=dict(error='Invalid settings request')
        verifier=os.environ.get('PHOENIX_ADMIN_PASSWORD_HASH','')
        origin=self.headers.get('Origin','');host=self.headers.get('Host','')
        try:
            if mode not in DBS:raise ValueError('Unknown paper account')
            if not verifier:code=503;raise ValueError('Admin password has not been configured')
            if origin not in ('https://'+host,'http://'+host) or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                code=403;raise ValueError('Use the Settings page on this site')
            if not check_admin(self.headers.get('Authorization',''),verifier,self.client_address[0]):
                code=401;raise ValueError('Admin password incorrect, or too many attempts. Wait one minute before retrying.')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(length))
            if set(body)!={'values','revision','request_id'} or type(body['revision']) is not int or not isinstance(body['request_id'],str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',body['request_id']):
                raise ValueError('Invalid settings request')
            values=validate_settings(body['values'])
            key='settings:'+mode+':'+body['request_id']+':'+settings_hash(values)
            with LOCK:
                db=DBS[mode]
                if db is None:code=503;raise ValueError('Account is warming. Retry when ready.')
                code=409
                db.apply(key,lambda st:change_settings(st,values,now_ms(),body['revision'],body['request_id']))
            code=200;response=settings_view(mode)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:response=dict(error=str(exc))
        except Exception:code=503;response=dict(error='Settings could not be saved. Reload to check the recorded revision before retrying.')
        data=canonical(response).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

    def do_GET(self):
        # Dashboard fetches report + selection concurrently. Only full-history
        # routes need backpressure; routine status must remain independently usable.
        if urllib.parse.urlsplit(self.path).path not in ('/api/trades','/api/position','/api/ledger'):
            self.read_response();return
        if not READ_RESPONSE.acquire(timeout=0.1):
            self.send_response(503);self.send_header('Retry-After','2');self.end_headers();return
        try:self.read_response()
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        finally:READ_RESPONSE.release()

    def read_response(self):
        url=urllib.parse.urlsplit(self.path);path=url.path;mode=urllib.parse.parse_qs(url.query).get('mode',['fixed'])[0]
        status=200;ctype='application/json';result=None;evidence_db=None
        if mode not in DBS:status=400;data=b'{"error":"unknown paper portfolio"}'
        elif path=='/':data=(ROOT/'dashboard_v2.html').read_bytes();ctype='text/html; charset=utf-8'
        elif path=='/settings':data=(ROOT/'settings.html').read_bytes();ctype='text/html; charset=utf-8'
        elif path=='/api/settings':data=canonical(settings_view(mode)).encode()
        elif path=='/ledger':data=(ROOT/'ledger.html').read_bytes();ctype='text/html; charset=utf-8'
        elif path=='/healthz':data=canonical(dict(alive=True,mode='PAPER_ONLY',version='phoenix-paper-6-memory',memory_bytes=memory_usage())).encode()
        elif path=='/readyz':
            health=summary(mode)['health'];status=200 if health['ready'] else 503;data=canonical(health).encode()
        elif path in ('/api/report','/api/state'):data=canonical(summary(mode)).encode()
        elif path=='/api/trades':
            args=urllib.parse.parse_qs(url.query)
            result=execution_ledger(mode,include_evidence=False,record_id=args.get('record',[None])[0],filled_only=args.get('filled',['0'])[0]=='1')
            if args.get('evidence',['0'])[0]=='1':evidence_db=DBS[mode]
        elif path=='/api/position':
            result=position_details(mode,urllib.parse.parse_qs(url.query).get('symbol',[''])[0])
            status=200 if result else 404;data=canonical(result or dict(error='position unavailable')).encode()
        elif path=='/api/selection':
            with LOCK:sel=copy.deepcopy(DBS['weekly'].state.get('selection')) if DBS['weekly'] else None;progress=copy.deepcopy(SELECTOR)
            data=canonical(dict(status=progress,selection=sel,config=SELECTION_CONFIG)).encode()
        elif path=='/api/ledger':
            with LOCK:data=canonical(DBS[mode].ledger() if DBS[mode] else []).encode()
        elif path=='/metrics':
            r=summary(mode);ctype='text/plain; version=0.0.4'
            data=('phoenix_paper_only 1\n'+f"phoenix_ready {int(r['health']['ready'])}\nphoenix_cash {r['state']['cash']}\nphoenix_equity {r['equity']}\nphoenix_basis {r['basis']}\nphoenix_cycles {r['completed_cycles']}\nphoenix_observations {r['state']['observations']}\nphoenix_max_drawdown {r['state']['max_drawdown']}\n").encode()
        else:status=404;data=b'{"error":"not found"}'
        compressed='gzip' in self.headers.get('Accept-Encoding','').lower() and (result is not None or len(data)>4096)
        # Spill response bytes to a temporary file, never a second full RAM buffer.
        # Temporary files are derived responses only; the durable ledger is untouched.
        with tempfile.TemporaryFile() as spool:
            output=gzip.GzipFile(fileobj=spool,mode='wb',compresslevel=3,mtime=0) if compressed else spool
            try:
                if result is None:output.write(data)
                else:
                    for chunk in response_chunks(result,evidence_db):output.write(chunk.encode())
            finally:
                if compressed:output.close()
            length=spool.tell();spool.seek(0)
            self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Vary','Accept-Encoding')
            if compressed:self.send_header('Content-Encoding','gzip')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.send_header('Content-Length',str(length));self.end_headers()
            self.connection.settimeout(15)
            shutil.copyfileobj(spool,self.wfile,length=65536)

if __name__=='__main__':
    threading.Thread(target=market_worker,daemon=True).start()
    threading.Thread(target=setup_worker,daemon=True).start()
    for mode in DBS:threading.Thread(target=worker,args=(mode,),daemon=True).start()
    threading.Thread(target=selection_worker,daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
