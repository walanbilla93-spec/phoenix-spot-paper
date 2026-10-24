"""Frozen researched weekly selector. Public spot only; no performance fitting."""
import datetime as dt
import hashlib
import statistics
from phoenix import *

DAY=24*HOUR
FIVE=5*MINUTE
CORE=('SOLUSDT','AVAXUSDT','ALGOUSDT','HBARUSDT')
EXCLUDED={'TRXUSDT':'user excluded low-movement TRX',
          'MATICUSDT':'preserved migration exclusion; no POL alias substitution'}
SELECTION_CONFIG=dict(version='weekly-original-cohort-1',cohort=RESEARCH_COHORT,
    excluded=EXCLUDED,schedule='Monday 00:05 UTC',window_days=30,coverage_5m=.999,
    required_hours=720,median_daily_turnover_min=1000000,
    median_atr_ratio_min=.003,median_atr_ratio_max=.03,
    rank=['median_atr_ratio descending','EMA200 structure descending','median turnover descending','symbol ascending'],
    slots=5,legacy='keep managing dropped positions; no forced exit',
    fixed_fifth='highest eligible not in fixed core; frozen after first assignment')
SELECTION_HASH=hashlib.sha256(canonical(SELECTION_CONFIG).encode()).hexdigest()

def week_boundary(at):
    d=dt.datetime.fromtimestamp(at/1000,dt.timezone.utc)
    monday=(d-dt.timedelta(days=d.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    boundary=int(monday.timestamp()*1000)
    return boundary-7*DAY if at<boundary+FIVE else boundary

def hourly_features(rows):
    out={};history=[];prev=None;pt=None;atr=e200=None;trs=[]
    for r in sorted(rows,key=lambda r:r[0]):
        t,o,h,l,c=r[:5]
        if pt is not None and t-pt!=HOUR:
            history=[];prev=None;atr=e200=None;trs=[]
        if not (0<l<=min(o,c)<=max(o,c)<=h):raise ValueError('invalid selection hour')
        tr=max(h-l,abs(h-prev),abs(l-prev)) if prev else h-l
        trs.append(tr);n=len(history)+1
        if n==14:atr=sum(trs)/14
        elif n>14:atr=(13*atr+tr)/14
        if n==200:e200=sum(x[4] for x in history+[r])/200
        elif n>200:e200=2*c/201+199*e200/201
        if n>=240:out[t+HOUR]=dict(ar=atr/c,structure=max(-.2,min(.2,c/e200-1)))
        history.append(r);prev=c;pt=t
    return out

def candidate(symbol, five, hours, boundary, spot=True):
    # Explicitly discard all observations at/after the weekly UTC boundary.
    five={int(r[0]):r for r in five if boundary-30*DAY<=r[0]<boundary and r[0]%FIVE==0}
    hours={int(r[0]):r for r in hours if r[0]<boundary and r[0]%HOUR==0}
    expected=range(boundary-30*DAY,boundary,FIVE)
    coverage=sum(t in five for t in expected)/8640
    complete=[hours.get(t) for t in range(boundary-30*DAY,boundary,HOUR)]
    ff=hourly_features(list(hours.values()))
    ratios=[ff.get(t+HOUR,{}).get('ar') for t in range(boundary-30*DAY,boundary,HOUR)]
    daily=[sum(r[6] for r in complete[k:k+24]) for k in range(0,720,24)] if all(r is not None for r in complete) else []
    vol=statistics.median(ratios) if all(r is not None for r in ratios) else None
    turnover=statistics.median(daily) if daily else None
    structure=ff.get(boundary,{}).get('structure')
    reasons=[]
    if not spot:reasons.append('genuine trading spot pair unavailable')
    if symbol in EXCLUDED:reasons.append(EXCLUDED[symbol])
    if coverage<.999:reasons.append('insufficient 5-minute continuity')
    if len(daily)!=30 or vol is None or structure is None:reasons.append('incomplete 30-day hourly history/warmup')
    if turnover is not None and turnover<1000000:reasons.append('median daily turnover below 1m USDT')
    if vol is not None and not .003<=vol<=.03:reasons.append('median hourly ATR outside 0.3%-3%')
    return dict(symbol=symbol,eligible=not reasons,reasons=reasons,coverage=coverage,
                median_atr_ratio=vol,median_daily_turnover=turnover,structure=structure,boundary=boundary)

def ranking(metrics):
    return sorted([r for r in metrics if r['eligible'] and r['symbol'] in RESEARCH_COHORT and r['symbol'] not in EXCLUDED],key=lambda r:(-r['median_atr_ratio'],-r['structure'],-r['median_daily_turnover'],r['symbol']))

def weekly_initial():
    st=initial();st.update(positions={},selection_mode='weekly',selection_config_hash=SELECTION_HASH,selection=None)
    return st

def weekly_audit(st):
    audit(st)
    assert st['selection_mode']=='weekly' and st['selection_config_hash']==SELECTION_HASH
    assert set(st['positions'])<=set(RESEARCH_COHORT)-set(EXCLUDED)

def apply_selection(st,snapshot,mode):
    if snapshot['available_at']>snapshot['recorded_at'] or snapshot['selection_hash']!=SELECTION_HASH:
        raise ValueError('selection not yet available or changed protocol')
    selected=snapshot['selected']
    if selected!=[r['symbol'] for r in ranking(snapshot['metrics'])[:5]]:
        raise ValueError('selection must match frozen rank order')
    if mode=='weekly':
        for s in selected:st['positions'].setdefault(s,position())
        st['selection']=snapshot
        return [dict(type='weekly_selection',snapshot=snapshot)]
    if st.get('replacement'):return []
    eligible=[r['symbol'] for r in ranking(snapshot['metrics']) if r['symbol'] not in CORE and r['symbol'] not in EXCLUDED]
    if not eligible:return []
    s=eligible[0];st['positions'].setdefault(s,position())
    st['replacement']=dict(symbol=s,replaces='XMRUSDT',at=snapshot['recorded_at'],selection_hash=SELECTION_HASH,
                           boundary=snapshot['boundary'],evidence_hash=snapshot['evidence_hash'])
    return [dict(type='fixed_fifth_amendment',replacement=st['replacement'],snapshot=snapshot)]

def entry_reason(p,telemetry,selected,halted=False):
    if p['active']:
        if p['purchases']>=15:return 'Ladder exhausted; monitoring exit'
        if p['pending']:return 'Paper '+p['pending']['side']+' queued for next observation'
        return 'Position open; waiting for DCA rebound or profit callback'
    if not selected:return 'Outside this week’s selection; existing positions still managed'
    if halted:return 'New buys halted: market data unavailable'
    f=telemetry.get('feature')
    if not f:return 'Waiting for completed-hour history'
    if f['close']>f['prior_low']*1.01:
        return f"Waiting: hour close {f['close']:.8g} above entry ceiling {f['prior_low']*1.01:.8g}"
    if f['close']<f['prior_low']:return 'Waiting: hour close below prior low'
    if not f['dip']:return 'Waiting for hourly stabilization'
    if p['pending']:return 'Paper entry queued for next observation'
    return 'Entry conditions met; waiting for next fresh execution observation'
