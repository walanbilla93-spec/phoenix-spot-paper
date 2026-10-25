"""Prospective fresh-cycle setups and deterministic vacancy allocation. Paper only."""
from phoenix import *
from rotation import CORE,EXCLUDED,ranking

def entry_pool(st,mode):
    if mode=='fixed':return list(CORE)+([st['replacement']['symbol']] if st.get('replacement') else [])
    snapshot=st.get('selection')
    if not snapshot:return []
    if mode=='weekly':return snapshot['selected']
    return [r['symbol'] for r in ranking(snapshot['metrics'])]

def recent_setup(candles,at):
    rr=sorted([r for r in candles if r[0]+MINUTE<=at],key=lambda r:r[0])[-30:]
    if len(rr)!=30 or any(r[0]%MINUTE for r in rr) or any(b[0]-a[0]!=MINUTE for a,b in zip(rr,rr[1:])):
        raise ValueError('30 contiguous completed minutes required for fresh cycle')
    for r in rr:
        if not all(math.isfinite(v) for v in r[:7]) or not (0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2]):
            raise ValueError('invalid fresh-cycle minute')
    b,previous=rr[-1],rr[-2];stabilized=b[4]>=b[1] and b[4]>=previous[4]
    fresh=0<=at-(b[0]+MINUTE)<=55000
    return dict(policy_hash=CYCLE_ENTRY_HASH,bar_at=b[0],at=at,ready=bool(stabilized and fresh),
        price_cap=b[4]*1.003,strength_5m=b[4]/rr[-6][4]-1,
        close=b[4],previous_close=previous[4],open=b[1],
        reason='Current minute stabilized; eligible for a fresh cycle' if stabilized and fresh else
               'Waiting for a fresh completed minute' if not fresh else 'Waiting for current-minute stabilization')

def refill_inputs(st,mode,plans,at):
    pool=entry_pool(st,mode)
    return dict(mode=mode,pool=pool,plans={s:copy.deepcopy(plans.get(s,{})) for s in pool},
        positions={s:dict(active=p['active'],pending=bool(p['pending']),entry_at=p['entry_at'],last_exit=p['last_exit'])
                   for s,p in st['positions'].items()},minute=at//MINUTE,
        metrics=st.get('selection',{}).get('metrics',[]) if st.get('selection') else [])

def apply_refill_allocation(st,inputs,at):
    pool=inputs['pool']
    if not set(pool)<=set(RESEARCH_COHORT)-set(EXCLUDED):raise ValueError('unapproved refill universe')
    metrics={r['symbol']:r for r in inputs['metrics']}
    reserved=[s for s,p in st['positions'].items() if p['active'] or (p['pending'] and s in pool)]
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
    candidates.sort(key=lambda r:(not r['never_traded'],-r['strength_5m'],-r['weekly_atr'],-r['turnover'],r['symbol']))
    selected=reserved+[r['symbol'] for r in candidates[:max(0,5-len(reserved))]]
    for s in selected:st['positions'].setdefault(s,position())
    cancelled=[]
    for s,p in st['positions'].items():
        if not p['active'] and p['pending'] and s not in selected:
            cancelled.append(s);p['pending']=None
    st['refill_allocation']=dict(at=at,selected=selected,pool=pool,ranked=candidates,blocked=blocked,
        policy_hash=CYCLE_ENTRY_HASH)
    return [dict(type='refill_allocation',at=at,inputs=inputs,cancelled_entries=cancelled)]
