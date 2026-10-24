"""Read-only HTTP dashboard and single-worker public spot polling application."""
import copy
import json
import os
import pathlib
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from phoenix import *

ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path(os.environ.get('DATA_DIR','/data'))
LOCK=threading.RLock()
STATUS=dict(mode='PAPER_ONLY',real_orders_enabled=False,account_eligibility='UNVERIFIED',
            ready=False,persistence=False,symbols={},last_poll=None,errors=0,version=VERSION)
DB=None

def rows(result):
    return sorted([[int(r[0])]+[float(v) for v in r[1:7]] for r in result['list']],key=lambda r:r[0])

def summary():
    at=now_ms()
    with LOCK:
        state=copy.deepcopy(DB.state) if DB else initial()
        health=copy.deepcopy(STATUS)
    equity=state['cash'];basis=0
    for s,p in state['positions'].items():
        p['mark_age_ms']=at-p['mark_at'] if p['mark_at'] else None
        p['mark_stale']=p['mark_age_ms'] is None or p['mark_age_ms']>MAX_AGE
        p['marked_value']=p['qty']*(p['mark'] or 0)
        p['floating_pnl']=p['marked_value']-p['basis']
        p['cycle_age_days']=(at-p['entry_at'])/86400000 if p['active'] else 0
        p['underwater_days']=(at-p['underwater_since'])/86400000 if p['underwater_since'] else 0
        p['ladder_exhausted']=p['purchases']>=15
        equity+=p['marked_value'];basis+=p['basis']
    # Public quote snapshots are polled, so mark age rises between requests.
    health['ready']=bool(health['ready'] and health['last_poll'] and at-health['last_poll']<30000)
    return dict(health=health,state=state,equity=equity,basis=basis,floating_pnl=equity-state['cash']-basis,
                completed_cycles=sum(p['cycles'] for p in state['positions'].values()),
                equity_marks_stale=any(p['mark_stale'] and p['qty']>0 for p in state['positions'].values()),
                reserve_warning='250 USDT is a cash acquisition constraint, never a guaranteed equity floor.',
                execution_warning='Visible-book hypothetical IOC fills; cancellations, queue priority, real latency and eligibility are unverified.',
                persistence_free_bytes=shutil.disk_usage(DATA).free if DATA.exists() else None,config=CONFIG,
                config_hash=CONFIG_HASH,at=at)

def worker():
    global DB
    try:
        if os.environ.get('PAPER_ONLY','true').lower()!='true':
            raise ValueError('PAPER_ONLY is immutable; non-paper configuration refused')
        if any(os.environ.get(k) for k in ('BYBIT_API_KEY','BYBIT_API_SECRET','EXCHANGE_API_KEY','EXCHANGE_API_SECRET')):
            raise ValueError('exchange credentials are unnecessary and refused')
        DATA.mkdir(parents=True,exist_ok=True)
        # No ephemeral portfolio: a real mounted persistent directory is mandatory in deployment.
        database_url=os.environ.get('DATABASE_URL')
        persistent=bool(database_url) or os.path.ismount(DATA)
        if not persistent and os.environ.get('LOCAL_PAPER_TEST')!='1':
            raise ValueError('persistent volume not mounted: paper ledger disabled; dashboard remains available')
        if not database_url:
            import platform
            lockfile=open(DATA/'worker.lock','a+')
            if platform.system()=='Windows':
                import msvcrt
                lockfile.seek(0);lockfile.write('1');lockfile.flush();lockfile.seek(0)
                msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with LOCK:
            DB=Store(database_url or str(DATA/'phoenix.sqlite3'))
            STATUS.update(persistence=persistent,storage='managed_postgresql' if database_url else 'sqlite',ledger_head=DB.verify())
        client=PublicSpot();rules={};hours={};features={};minute={};refresh={};instruments_at=0
        while True:
            cycle_at=now_ms();ok={};books={};telemetry={};failures={}
            # Validate every supported pair before permitting any new buy this poll.
            for s in SYMBOLS:
                try:
                    if cycle_at-instruments_at>=1800000 or s not in rules:
                        result,meta=client.get('instruments-info',s)
                        rules[s]=parse_rule(result,s,meta['received_at'])
                    if s not in hours or (cycle_at-300000)//HOUR!=refresh.get('hour:'+s):
                        result,meta=client.get('kline',s,interval=60,limit=1000)
                        complete=[r for r in rows(result) if r[0]+HOUR+300000<=meta['received_at']]
                        with LOCK: hours[s]=DB.hourly(s,complete)
                        features[s]=feature(hours[s],meta['received_at'])
                        refresh['hour:'+s]=(cycle_at-300000)//HOUR
                    if s not in minute or cycle_at//MINUTE!=refresh.get('minute:'+s):
                        result,meta=client.get('kline',s,interval=1,limit=3)
                        complete=[r for r in rows(result) if r[0]+MINUTE<=meta['received_at']]
                        if not complete: raise ValueError('no completed minute')
                        minute[s]=complete[-1]
                        if minute[s][0]+MINUTE>cycle_at-MINUTE:
                            refresh['minute:'+s]=cycle_at//MINUTE
                    # Ensure cached features and candles remain current; no outage catch-up fills.
                    if now_ms()-(minute[s][0]+MINUTE)>=90000: raise ValueError('minute data stale')
                    if now_ms()-features[s]['at']>=HOUR: raise ValueError('hour data stale')
                    book,meta=client.get('orderbook',s,limit=50)
                    book['received_at']=meta['received_at']
                    bid,ask=valid_book(book,now_ms(),s)
                    books[s]=book;ok[s]=True
                    telemetry[s]=dict(status='spot_public_ok',bid=bid,ask=ask,quote_at=book['ts'],
                                      received_at=meta['received_at'],rtt_ms=meta['rtt_ms'],
                                      spread_bps=(ask-bid)/((bid+ask)/2)*10000,
                                      signal_bar_close=minute[s][0]+MINUTE,feature=features[s],
                                      depth_probes={str(n):marketable(book,rules[s],'buy',n,now_ms()) for n in (7,10,16)},
                                      exit_150_probe=marketable(book,rules[s],'sell',150/bid,now_ms()))
                except Exception as exc:
                    failures[s]=str(exc)[:240]
                    telemetry[s]=dict(status='cash_unsupported' if s=='XMRUSDT' and s not in rules else 'data_failure',error=str(exc)[:240])
            if cycle_at-instruments_at>=1800000: instruments_at=cycle_at
            required=[s for s in SYMBOLS if s!='XMRUSDT' or s in rules]
            all_ok=all(ok.get(s) for s in required)
            # Refetch execution books AFTER the complete data sweep, never reuse an old early snapshot.
            for s in required:
                if s not in books: continue
                try:
                    book,meta=client.get('orderbook',s,limit=50);book['received_at']=meta['received_at']
                    at=now_ms();valid_book(book,at,s)
                    with LOCK:
                        p=DB.state['positions'][s]
                        need_commit=(p['last_observation_bar']!=minute[s][0] or
                                     (p['pending'] and at//MINUTE>p['pending'].get('last_attempt_bar',-1)) or
                                     at-(p['last_observation_at'] or 0)>=MINUTE)
                        if not need_commit: continue
                        key=f"{s}:{book['ts']}:{book.get('u')}:{at//5000}"
                        DB.apply(key,lambda st:advance(st,s,minute[s],features[s],book,rules[s],at,allow_buy=all_ok))
                        STATUS['ledger_head']=DB.db.execute('SELECT hash FROM events ORDER BY id DESC LIMIT 1').fetchone()[0]
                except Exception as exc:
                    all_ok=False;failures[s]=str(exc)[:240]
                    telemetry[s]=dict(status='data_failure',error=str(exc)[:240])
            with LOCK:
                STATUS.update(ready=all_ok,symbols=telemetry,last_poll=now_ms(),errors=STATUS['errors']+len(failures),
                              new_buys_halted=not all_ok,fifth_slot='active genuine spot' if 'XMRUSDT' in rules else 'cash',
                              persistent_db_bytes=DB.db.execute('SELECT pg_database_size(current_database())').fetchone()[0] if database_url else (DATA/'phoenix.sqlite3').stat().st_size)
            print(canonical(dict(type='poll',at=now_ms(),mode='PAPER_ONLY',ready=all_ok,failures=failures,
                                 observations=DB.state['observations'],cash=DB.state['cash'])),flush=True)
            # Boundary aligned frequent polls let completed-minute signals be observed without replaying history.
            time.sleep(max(.25,5-time.time()%5))
    except Exception as exc:
        # Do not expose database connection strings/credentials through errors.
        error='storage connection failure (details withheld)' if 'database_url' in locals() and database_url else str(exc)[:300]
        with LOCK: STATUS.update(ready=False,new_buys_halted=True,fatal=error)
        print(canonical(dict(type='fatal',error=error,mode='PAPER_ONLY')),flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split('?')[0]
        status=200;ctype='application/json'
        if path=='/':
            data=(ROOT/'dashboard.html').read_bytes();ctype='text/html; charset=utf-8'
        elif path=='/healthz': data=canonical(dict(alive=True,mode='PAPER_ONLY')).encode()
        elif path=='/readyz':
            health=summary()['health'];status=200 if health['ready'] else 503;data=canonical(health).encode()
        elif path in ('/api/report','/api/state'): data=canonical(summary()).encode()
        elif path=='/api/ledger':
            with LOCK: data=canonical(DB.ledger() if DB else []).encode()
        elif path=='/metrics':
            r=summary();ctype='text/plain; version=0.0.4'
            data=('phoenix_paper_only 1\n'+f"phoenix_ready {int(r['health']['ready'])}\nphoenix_cash {r['state']['cash']}\nphoenix_equity {r['equity']}\nphoenix_basis {r['basis']}\nphoenix_cycles {r['completed_cycles']}\nphoenix_observations {r['state']['observations']}\nphoenix_max_drawdown {r['state']['max_drawdown']}\n").encode()
        else: status=404;data=b'{"error":"not found"}'
        self.send_response(status);self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

if __name__=='__main__':
    threading.Thread(target=worker,daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
