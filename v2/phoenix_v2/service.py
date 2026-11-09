"""Read-only dashboard for the persistent experimental paper worker.

The legacy Handler remains for offline compatibility. Production main starts
the public-data worker and ForwardHandler. No order submission API exists.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from . import VERSION
from .manifest import Manifest
from .model import canonical

IMPLEMENTATION_LIMITS=[
    'V08 remains open: full 87-topic RC2 fidelity has not been independently certified.',
    'Automatic OHLC admissible-path construction, cancel-latency races and full holdout/stress acceptance orchestration remain incomplete; unsupported inputs are quarantined. Supplied audited scenario branches can be propagated.',
    'No verified dataset, fees, funding, contract-rule history, mark feed or execution quantity model is bundled.',
    'Forward paper processing is disabled; this deployment serves status only. Offline chronological replay components are available.',
]

def safety(env):
    if env.get('PAPER_ONLY','true').lower()!='true': raise RuntimeError('PAPER_ONLY must be true')
    forbidden=[k for k,v in env.items() if v and any(x in k.upper() for x in ('BYBIT_API','EXCHANGE_API','TRADING_KEY','TRADING_SECRET','BINANCE_API'))]
    if forbidden: raise RuntimeError('exchange trading credentials are forbidden')

def status():
    return dict(service='Phoenix V2 RC2',version=VERSION,mode='PAPER_ONLY',real_orders_enabled=False,
                process_healthy=True,forward_paper_operational=False,ready=False,
                status='BLOCKED_VERIFICATION_AND_IMPLEMENTATION',gates=Manifest().report(),
                implementation_limits=IMPLEMENTATION_LIMITS,backtest_results=None,
                spot_service_modified=False,uptime_seconds=int(time.monotonic()-START))

PAGE='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Phoenix V2 RC2 · Paper research</title><style>body{background:#0d1423;color:#eaf0fa;font:16px system-ui;max-width:980px;margin:50px auto;padding:24px}h1{color:#ffbf69}section{background:#172238;border-radius:12px;padding:22px;margin:18px 0}p{line-height:1.6}.badge{color:#ffce87}td{padding:10px;border-bottom:1px solid #334158}a{color:#89c9ff}small{color:#b5c3d8}</style><h1>Phoenix V2 <small>RC2</small></h1><p>Independent Arm A / Arm B · strictly paper-only</p><section><h2 class="badge">Blocked — verification and implementation remain open</h2><p>The web process is running. Forward paper processing is disabled. There are no exchange orders, trading credentials, simulated fills or backtest results.</p><p>The existing Phoenix Spot service is separate.</p></section><section><h2>What is available</h2><p>Offline Python components for causal candles, structure, POIs, liquidity, setups, independent arm accounting, risk checks and replay audit logs. Synthetic tests check software behavior; they do not establish market performance.</p><h2>Still required</h2><ul id="limits"></ul></section><section><h2>Verification gates</h2><table id="gates"></table></section><p><a href="/api/status">Status JSON</a> · <a href="/healthz">Process health</a> · <a href="/readyz">Paper readiness</a></p><script>fetch('/api/status').then(r=>r.json()).then(x=>{for(const s of x.implementation_limits){const li=document.createElement('li');li.textContent=s;document.querySelector('#limits').append(li)}for(const [g,v] of Object.entries(x.gates)){const tr=document.createElement('tr');for(const s of [g,v.description,v.status]){const td=document.createElement('td');td.textContent=s;tr.append(td)}document.querySelector('#gates').append(tr)}})</script></html>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split('?')[0]
        if path=='/': code=200; payload=PAGE.encode(); kind='text/html; charset=utf-8'
        elif path in ('/healthz','/readyz','/api/status'):
            code=503 if path=='/readyz' else 200
            payload=canonical(dict(healthy=True,mode='PAPER_ONLY') if path=='/healthz' else status()).encode(); kind='application/json'
        else: code=404; payload=b'Not found'; kind='text/plain'
        self.send_response(code); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(payload)
    def log_message(self,fmt,*args): print(canonical(dict(event='http',message=fmt%args)),flush=True)

START=time.monotonic()
WORKER=None
LEGACY_STATUS=status

def status():
    if WORKER is None: return LEGACY_STATUS()
    return dict(service='Phoenix V2 RC2',version=VERSION,mode='PAPER_ONLY',
        real_orders_enabled=False,spot_service_modified=False,backtest_results=None,
        gates=Manifest().report(),revision=os.environ.get('NF_DEPLOYMENT_SHA',os.environ.get('NORTHFLANK_COMMIT_SHA',os.environ.get('BUILD_REVISION','unknown'))),
        uptime_seconds=int(time.monotonic()-START),**WORKER.status())

class ForwardHandler(Handler):
    def do_GET(self):
        path=self.path.split('?')[0]; state=status(); code=200; kind='application/json'
        if path=='/':
            payload=Path(__file__).with_name('dashboard.html').read_bytes();kind='text/html; charset=utf-8'
        elif path=='/api/status': payload=canonical(state).encode()
        elif path in ('/healthz','/readyz','/executionz'):
            key={'/healthz':'worker_alive','/readyz':'ready','/executionz':'execution_ready'}[path]
            code=200 if state.get(key) else 503
            payload=canonical(dict(healthy=state.get(key,False),status=state['status'],error=state.get('error'))).encode()
        elif path in ('/api/journal','/api/signals','/api/trades','/api/candles'):
            if not WORKER.journal or WORKER.fatal:
                code=503;payload=b'{"error":"journal unavailable"}'
            else:
                kinds={'/api/signals':('signal','signal_evaluation'),
                       '/api/trades':('entry_fill','exit_fill','position_closed','liability_quarantined','funding','funding_missing')}.get(path)
                try: payload=canonical(WORKER.journal.candles() if path=='/api/candles' else WORKER.journal.recent(kinds)).encode()
                except Exception: code=503;payload=b'{"error":"journal unavailable"}'
        else: code=404;payload=b'{"error":"not found"}'
        self.send_response(code);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
        try:self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass

def main():
    global WORKER
    safety(os.environ)
    from .forward import Worker
    import signal
    from http.server import HTTPServer
    directory=os.environ.get('RC2_DATA_DIR','/data')
    # Filesystem presence alone is not persistence. Production requires a mount.
    persistent=os.path.ismount(directory)
    WORKER=Worker(directory,tuple(os.environ.get('RC2_SYMBOLS','BTCUSDT').split(',')),persistent)
    WORKER.start()
    class Server(HTTPServer):
        def get_request(self):
            sock,address=super().get_request();sock.settimeout(5);return sock,address
    server=Server(('0.0.0.0',int(os.environ.get('PORT','8080'))),ForwardHandler)
    def stop(*args):
        WORKER.stop.set()
        import threading
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(canonical(dict(event='startup',**status())),flush=True)
    try:server.serve_forever(poll_interval=.5)
    finally:
        WORKER.stop.set();WORKER.thread.join(timeout=25);server.server_close()
if __name__=='__main__': main()
