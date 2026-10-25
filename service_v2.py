"""Three independent PAPER_ONLY portfolios; all public HTTP endpoints are read-only."""
import copy
import concurrent.futures
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
from refill import entry_pool,recent_setup,refill_inputs,apply_refill_allocation

ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path(os.environ.get('DATA_DIR','/data'))
LOCK=threading.RLock()
DBS={'fixed':None,'weekly':None,'weekly_five':None}
STATUSES={mode:dict(mode='PAPER_ONLY',portfolio=mode,real_orders_enabled=False,
    account_eligibility='UNVERIFIED',ready=False,persistence=False,symbols={},last_poll=None,
    errors=0,version='phoenix-paper-5') for mode in DBS}
SELECTOR=dict(status='warming',completed=0,total=len(RESEARCH_COHORT),selection_hash=SELECTION_HASH)
MARKET=dict(status='warming',symbols={},last_success_at=None)
DISPLAY_MAX_AGE=30000
SETUPS={}

def setup_worker():
    client=PublicSpot(tuple(set(SYMBOLS+RESEARCH_COHORT)));updated={}
    while True:
        with LOCK:
            fixed=DBS['fixed'];weekly=DBS['weekly']
            pool=set(entry_pool(fixed.state,'fixed')) if fixed else set(CORE)
            if weekly and weekly.state.get('selection'):
                pool.update(r['symbol'] for r in ranking(weekly.state['selection']['metrics']))
        at=now_ms();needed=[s for s in pool if updated.get(s)!=at//MINUTE]
        def acquire(s):
            result,meta=client.get('kline',s,interval=1,limit=32)
            return recent_setup(rows(result),meta['received_at'])
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
        # Filter on the database before transfer: observation-only snapshots are large.
        # Canonical JSON emits this exact discriminator; all historical attempts use it.
        for i,payload,digest in db.db.execute('SELECT id,payload,hash FROM events WHERE payload LIKE ? ORDER BY id',('%"type":"attempt"%',)):
            for e in json.loads(payload)['events']:
                if e.get('type')!='attempt' or e.get('symbol')!=symbol or not e.get('fill',{}).get('qty',0)>0:continue
                fill=e['fill'];buy=e['side']=='buy'
                trades.append(dict(ledger_id=i,ledger_hash=digest,side=e['side'],at=e['at'],
                    current_cycle=bool(p['active'] and e['at']>=p['entry_at']),price=fill['price'],
                    gross_quantity=fill['qty'],net_quantity=fill['qty']*(1-FEE) if buy else fill['qty'],
                    usdt=fill['notional'],fee_usdt_equivalent=e['fee'],
                    fee_currency=symbol[:-4] if buy else 'USDT',fee_quantity=fill['qty']*FEE if buy else e['fee'],
                    buy_number=e.get('buy_number'),status=fill['status'],realized=e.get('realized'),
                    reason=e['order']['reason'],latency_ms=e['latency_ms'],
                    fill_source='SIMULATED_PUBLIC_BOOK',exchange_order_id=None,
                    quote_at=fill.get('quote_at'),quote_age_ms=fill.get('quote_age_ms'),
                    max_buy_price=e.get('max_buy_price'),min_sell_price=e.get('min_sell_price')))
    avg=p['basis']/p['qty'] if p['qty'] else None
    return dict(symbol=symbol,portfolio=mode,position=p,trades=trades,average_cost=avg,
        tp_arm_price=avg*1.016 if avg else None,net_break_even=avg/(1-FEE) if avg else None,
        at=now_ms(),time_zone='Asia/Colombo',fee_assumption=FEE,mode='PAPER_ONLY')

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
            DBS[mode]=db;STATUSES[mode].update(persistence=persistent,storage='managed_postgresql' if database_url else 'sqlite',ledger_head=db.verify())
            db.apply('execution-policy:'+EXECUTION_HASH,lambda st:install_execution_policy(st,now_ms()))
            db.apply('cycle-entry-policy:'+CYCLE_ENTRY_HASH,lambda st:install_cycle_entry_policy(st,now_ms()))
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
                    at=now_ms();valid_book(book,at,s)
                    with LOCK:
                        p=db.state['positions'][s]
                        plan=copy.deepcopy(SETUPS.get(s,{}))
                        ready_entry=not p['active'] and p['last_bar']!=minute[s][0] and fresh_entry_valid(p,plan,minute[s],at)
                        need_commit=(ready_entry or p['last_observation_bar']!=minute[s][0] or (p['pending'] and at//MINUTE>p['pending'].get('last_attempt_bar',-1)) or at-(p['last_observation_at'] or 0)>=MINUTE)
                        if not need_commit:continue
                        entry_ok=schedule_ok and s in selected;key=f"{s}:{book['ts']}:{book.get('u')}:{at//5000}"
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

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url=urllib.parse.urlsplit(self.path);path=url.path;mode=urllib.parse.parse_qs(url.query).get('mode',['fixed'])[0]
        status=200;ctype='application/json'
        if mode not in DBS:status=400;data=b'{"error":"unknown paper portfolio"}'
        elif path=='/':data=(ROOT/'dashboard_v2.html').read_bytes();ctype='text/html; charset=utf-8'
        elif path=='/healthz':data=canonical(dict(alive=True,mode='PAPER_ONLY')).encode()
        elif path=='/readyz':
            health=summary(mode)['health'];status=200 if health['ready'] else 503;data=canonical(health).encode()
        elif path in ('/api/report','/api/state'):data=canonical(summary(mode)).encode()
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
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

if __name__=='__main__':
    threading.Thread(target=market_worker,daemon=True).start()
    threading.Thread(target=setup_worker,daemon=True).start()
    for mode in DBS:threading.Thread(target=worker,args=(mode,),daemon=True).start()
    threading.Thread(target=selection_worker,daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
