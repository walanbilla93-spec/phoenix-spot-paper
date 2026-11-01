"""Frozen price-path functions from the all-95 baseline; do not tune in place."""
import math, copy

def adverse(price,side,tick,entry=False):
    x=price*(1+(side if entry else -side)*.0005)
    return (math.ceil(x/tick-1e-8) if (side==1)==entry else math.floor(x/tick+1e-8))*tick

def execute(state,reason,price,ts,trade):
    z=copy.deepcopy(state);side=trade['side'];entry=trade['entry'];tick=trade['tick']
    fraction=.5 if reason=='TP1' else z['remaining']
    fill=adverse(price,side,tick)
    z['unit_gross']+=fraction*side*(fill-entry);z['exit_notional']+=fraction*fill
    z['remaining']-=fraction
    z['events'].append(dict(reason=reason,minute_ms=ts,reference=price,fill=fill,fraction=fraction))
    if reason=='TP1':z['tp1']=True;z['stop']=entry
    else:
        z['closed']=True;z['reason']=reason;z['closed_ms']=ts
        if reason=='TP2':z['tp2']=True
    return z

def mark(state,price,trade):
    r=trade['side']*(price-trade['entry'])/trade['distance']
    state['mfe']=max(state['mfe'],r);state['mae']=min(state['mae'],r)

def path_step(state,points,ts,trade):
    z=copy.deepcopy(state);side=trade['side'];tp1=trade['tp1'];tp2=trade['tp2']
    opening=points[0];mark(z,opening,trade)
    if side*(opening-z['stop'])<=0:return execute(z,'BE_STOP' if z['tp1'] else 'STOP_GAP',opening,ts,trade)
    if not z['tp1'] and side*(opening-tp1)>=0:z=execute(z,'TP1',tp1,ts,trade)
    if z['tp1'] and side*(opening-tp2)>=0:return execute(z,'TP2',tp2,ts,trade)
    for start,end in zip(points,points[1:]):
        if z['closed']:break
        current=start
        for _ in range(3):
            stop=z['stop'];target=tp2 if z['tp1'] else tp1
            levels=[(stop,'BE_STOP' if z['tp1'] else 'STOP'),(target,'TP2' if z['tp1'] else 'TP1')]
            hits=[(p,reason) for p,reason in levels if min(current,end)-1e-10*abs(p)<=p<=max(current,end)+1e-10*abs(p)]
            if not hits:mark(z,end,trade);break
            price,reason=min(hits,key=lambda v:abs(v[0]-current));mark(z,price,trade)
            z=execute(z,reason,price,ts,trade)
            if z['closed']:break
            current=price
    return z

def economic_key(z):
    return (z['closed'],z['tp1'],z['tp2'],z.get('reason'),z['stop'],round(z['unit_gross'],12),round(z['exit_notional'],12),z.get('closed_ms'))

def merge(states):
    unique={}
    for z in states:
        k=economic_key(z)
        if k in unique:
            old=unique[k];old['mfe']=max(old['mfe'],z['mfe']);old['mae']=min(old['mae'],z['mae'])
        else:unique[k]=z
    if len(unique)>8192:raise ValueError('STATE_CAP')
    return list(unique.values())

