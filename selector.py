"""Acquire and retain the original weekly selector's public spot inputs."""
import concurrent.futures
import time
from rotation import *

def parse_rows(result):return [[int(r[0])]+[float(v) for v in r[1:7]] for r in result['list']]

def history(client,symbol,interval,start,end):
    found={};receipts=[];cursor=end
    while cursor>=start:
        window_start=max(start,cursor-int(interval)*MINUTE*1000+1)
        for attempt in range(3):
            try:
                result,meta=client.get('kline',symbol,interval=interval,start=window_start,end=cursor,limit=1000)
                break
            except Exception:
                if attempt==2:raise
                time.sleep(1+attempt)
        page=parse_rows(result)
        if not page:
            cursor=window_start-1;continue
        if any(r[0]>cursor or r[0]<window_start for r in page):raise ValueError('selection page outside requested time bounds')
        for r in page:
            if not (0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2]) or min(r[5:])<0:raise ValueError('invalid selection OHLC/volume')
            found[r[0]]=r
        receipts.append(dict(**meta,first=min(r[0] for r in page),last=max(r[0] for r in page),rows=len(page)))
        next_cursor=window_start-1
        if next_cursor>=cursor:raise ValueError('selection pagination did not advance')
        cursor=next_cursor
    return sorted(found.values(),key=lambda r:r[0]),receipts

def scan(store,lock,boundary,progress):
    client=PublicSpot(RESEARCH_COHORT);catalog,meta=client.get('instruments-info',None)
    with lock:store.db.execute('CREATE TABLE IF NOT EXISTS selection_inputs (boundary BIGINT,symbol TEXT,payload TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(boundary,symbol))')
    def acquire(symbol):
        with lock:existing=store.db.execute('SELECT payload,hash FROM selection_inputs WHERE boundary=? AND symbol=?',(boundary,symbol)).fetchone()
        if existing:
            if hashlib.sha256(existing[0].encode()).hexdigest()!=existing[1]:raise ValueError('frozen selection input hash mismatch')
            payload=json.loads(existing[0]);return dict(payload['metric'],input_hash=existing[1])
        five=[];hours=[];receipts={};rule=None
        if symbol in EXCLUDED:metric=candidate(symbol,[],[],boundary,spot=False)
        else:
            try:rule=parse_rule(catalog,symbol,meta['received_at'])
            except (ValueError,KeyError):rule=None
            if rule:
                five,r5=history(client,symbol,5,boundary-30*DAY,boundary-1)
                hours,rh=history(client,symbol,60,boundary-40*DAY,boundary-1)
                receipts=dict(five=r5,hours=rh,instrument=meta)
            metric=candidate(symbol,five,hours,boundary,spot=bool(rule))
        payload=canonical(dict(metric=metric,five=five,hours=hours,rule=rule,receipts=receipts));digest=hashlib.sha256(payload.encode()).hexdigest()
        with lock:store.db.execute('INSERT INTO selection_inputs VALUES(?,?,?,?) ON CONFLICT(boundary,symbol) DO NOTHING',(boundary,symbol,payload,digest))
        return dict(metric,input_hash=digest)
    metrics=[];failures={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(acquire,s):s for s in RESEARCH_COHORT}
        for future in concurrent.futures.as_completed(futures):
            s=futures[future]
            try:metrics.append(future.result())
            except Exception as exc:failures[s]=str(exc)[:180]
            progress(len(metrics)+len(failures),failures)
    if failures:raise ValueError('weekly input acquisition incomplete: '+','.join(sorted(failures)))
    ranked=ranking(metrics);metric_rows=sorted(metrics,key=lambda r:r['symbol']);evidence_hash=hashlib.sha256(canonical(metric_rows).encode()).hexdigest()
    return dict(boundary=boundary,available_at=boundary+FIVE,recorded_at=now_ms(),selection_hash=SELECTION_HASH,
        evidence_hash=evidence_hash,selected=[r['symbol'] for r in ranked[:5]],metrics=metric_rows,
        next_rotation_at=boundary+7*DAY+FIVE,note='Current genuine spot eligibility; prices stop at weekly boundary. No forced exits.')
