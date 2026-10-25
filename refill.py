"""Prospective fresh-cycle setups and deterministic vacancy allocation. Paper only."""
from phoenix import *
from rotation import CORE,EXCLUDED,ranking
import statistics

ACTIVITY_POLICY=dict(version='adaptive-activity-priority-1',active_minute_range_pct=.1,
    preferred_active_fraction=.2,preferred_daily_range_pct=[3,25],
    rank=['activity in preferred band','fraction of active last-30 minutes','median minute range',
          'daily range capped at 25%','current turnover','5m strength','unused coin','weekly ATR','symbol'],
    scope='adaptive next vacant slots only; preference, not a forced exit or a new hard entry gate')
ACTIVITY_HASH=hashlib.sha256(canonical(ACTIVITY_POLICY).encode()).hexdigest()

def install_activity_policy(st,at):
    if st.get('activity_policy_hash')==ACTIVITY_HASH:return []
    st['activity_policy_hash']=ACTIVITY_HASH
    return [dict(type='activity_policy_amendment',at=at,policy=ACTIVITY_POLICY,policy_hash=ACTIVITY_HASH)]

def daily_activity(result,meta):
    if result.get('category')!='spot':raise ValueError('activity requires spot tickers')
    if not isinstance(meta.get('server_at'),(int,float)) or not 0<=meta['received_at']-meta['server_at']<=30000:
        raise ValueError('stale activity ticker')
    output={}
    for r in result.get('list',[]):
        if r.get('symbol') not in RESEARCH_COHORT:continue
        try:
            high,low,turnover=(float(r[k]) for k in ('highPrice24h','lowPrice24h','turnover24h'))
            if not all(math.isfinite(v) for v in (high,low,turnover)) or not 0<low<=high or turnover<=0:continue
            output[r['symbol']]=dict(daily_range_pct=(high/low-1)*100,turnover_24h=turnover,
                high_24h=high,low_24h=low,ticker_at=meta['server_at'])
        except (ValueError,TypeError,KeyError):continue
    return output

def entry_pool(st,mode):
    if mode=='fixed':return list(CORE)+([st['replacement']['symbol']] if st.get('replacement') else [])
    snapshot=st.get('selection')
    if not snapshot:return []
    if mode=='weekly':return snapshot['selected']
    return [r['symbol'] for r in ranking(snapshot['metrics'])]

def recent_setup(candles,at,daily=None):
    rr=sorted([r for r in candles if r[0]+MINUTE<=at],key=lambda r:r[0])[-30:]
    if len(rr)!=30 or any(r[0]%MINUTE for r in rr) or any(b[0]-a[0]!=MINUTE for a,b in zip(rr,rr[1:])):
        raise ValueError('30 contiguous completed minutes required for fresh cycle')
    for r in rr:
        if not all(math.isfinite(v) for v in r[:7]) or not (0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2]):
            raise ValueError('invalid fresh-cycle minute')
    b,previous=rr[-1],rr[-2];stabilized=b[4]>=b[1] and b[4]>=previous[4]
    fresh=0<=at-(b[0]+MINUTE)<=55000
    plan=dict(policy_hash=CYCLE_ENTRY_HASH,bar_at=b[0],at=at,ready=bool(stabilized and fresh),
        price_cap=b[4]*1.003,strength_5m=b[4]/rr[-6][4]-1,
        close=b[4],previous_close=previous[4],open=b[1],
        reason='Current minute stabilized; eligible for a fresh cycle' if stabilized and fresh else
               'Waiting for a fresh completed minute' if not fresh else 'Waiting for current-minute stabilization')
    if daily is not None:
        ranges=[(r[2]-r[3])/r[4]*100 for r in rr]
        plan['activity']=dict(**daily,active_fraction=sum(v>=.1 for v in ranges)/30,
            active_minutes=sum(v>=.1 for v in ranges),median_minute_range_pct=statistics.median(ranges),
            window=[[r[0],r[2],r[3],r[4]] for r in rr])
    return plan

def refill_inputs(st,mode,plans,at):
    pool=entry_pool(st,mode)
    inputs=dict(mode=mode,pool=pool,plans={s:copy.deepcopy(plans.get(s,{})) for s in pool},
        position_order=list(st['positions']),
        positions={s:dict(active=p['active'],pending=bool(p['pending']),entry_at=p['entry_at'],last_exit=p['last_exit'])
                   for s,p in st['positions'].items()},minute=at//MINUTE,
        metrics=st.get('selection',{}).get('metrics',[]) if st.get('selection') else [])
    if st.get('activity_policy_hash')==ACTIVITY_HASH:inputs['activity_policy_hash']=ACTIVITY_HASH
    return inputs

def apply_refill_allocation(st,inputs,at):
    pool=inputs['pool']
    if not set(pool)<=set(RESEARCH_COHORT)-set(EXCLUDED):raise ValueError('unapproved refill universe')
    metrics={r['symbol']:r for r in inputs['metrics']}
    order=inputs.get('position_order',list(st['positions']))
    if set(order)!=set(st['positions']) or len(order)!=len(set(order)):raise ValueError('invalid recorded position order')
    reserved=[s for s in order if st['positions'][s]['active'] or (st['positions'][s]['pending'] and s in pool)]
    candidates=[];blocked={}
    for s in pool:
        p=st['positions'].get(s,position());plan=inputs['plans'].get(s,{})
        if s in reserved:continue
        if not plan or not fresh_entry_valid(p,plan,[plan.get('bar_at',0)],at):
            blocked[s]='Post-exit cooldown' if p['last_exit'] and at<p['last_exit']+2*MINUTE else plan.get('reason','Waiting for fresh candidate data')
            continue
        m=metrics[s]
        candidates.append(dict(symbol=s,never_traded=p['entry_at']==0,strength_5m=plan['strength_5m'],
            weekly_atr=m['median_atr_ratio'],turnover=m['median_daily_turnover']))
        if inputs.get('activity_policy_hash')==ACTIVITY_HASH:
            a=plan.get('activity',{});available=bool(a.get('daily_range_pct') is not None and 0<=at-a.get('ticker_at',0)<=90000)
            candidates[-1].update(activity_available=available,active_fraction=a.get('active_fraction',0) if available else 0,
                median_minute_range_pct=a.get('median_minute_range_pct',0) if available else 0,
                daily_range_pct=a.get('daily_range_pct',0) if available else 0,turnover_24h=a.get('turnover_24h',0) if available else 0,
                preferred_activity=available and a.get('active_fraction',0)>=.2 and 3<=a.get('daily_range_pct',0)<=25)
    if inputs.get('activity_policy_hash')==ACTIVITY_HASH:
        candidates.sort(key=lambda r:(not r['preferred_activity'],not r['activity_available'],-r['active_fraction'],
            -r['median_minute_range_pct'],-min(r['daily_range_pct'],25),-r['turnover_24h'],
            -r['strength_5m'],not r['never_traded'],-r['weekly_atr'],r['symbol']))
    else:candidates.sort(key=lambda r:(not r['never_traded'],-r['strength_5m'],-r['weekly_atr'],-r['turnover'],r['symbol']))
    selected=reserved+[r['symbol'] for r in candidates[:max(0,5-len(reserved))]]
    for s in selected:st['positions'].setdefault(s,position())
    cancelled=[]
    for s,p in st['positions'].items():
        if not p['active'] and p['pending'] and s not in selected:
            cancelled.append(s);p['pending']=None
    st['refill_allocation']=dict(at=at,selected=selected,pool=pool,ranked=candidates,blocked=blocked,
        policy_hash=CYCLE_ENTRY_HASH)
    return [dict(type='refill_allocation',at=at,inputs=inputs,cancelled_entries=cancelled)]
