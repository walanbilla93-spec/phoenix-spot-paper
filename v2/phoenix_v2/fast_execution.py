"""Conservative same-ms decision checks for receipt-aware paper activation."""
from .model import number

def material_times(runtime,symbol,rows):
    """Times that could change this symbol's cash, reservation or exposure.

    Cross-symbol public ticks away from these thresholds do not affect shared
    equity: valuation uses the separately archived minute mark, not trade ticks.
    """
    result=set()
    for arm in runtime.state['arms'].values():
        pos=arm.positions.get(symbol);order=arm.orders.get(symbol)
        if order and arm.name=='A':
            first=next((r for r in rows if r['T']/1000>order.submitted),None)
            if first:result.add(first['T'])
        for r in rows:
            at=r['T']/1000;p=number(r['p'])
            if pos:
                side=pos.signal.side;stop=pos.stop
                if pos.be_pending is not None and at>=pos.be_effective_at:stop=max(stop,pos.be_pending) if side==1 else min(stop,pos.be_pending)
                if pos.triggered or side*(p-stop)<=0 or side*(p-pos.target)>=pos.rules['tick']:result.add(r['T'])
            if order and at>=order.submitted:
                side=order.signal.side
                if side*(p-order.signal.extreme)<0 or side*(p-order.target)>=0:result.add(r['T'])
                if arm.name=='B' and side*(order.entry-p)>=order.rules['tick']:result.add(r['T'])
                if side*(p-order.stop)<=0 or side*(p-order.target)>=order.rules['tick']:result.add(r['T'])
    return result

def ambiguous(runtime,symbol,rows):
    groups={}
    for row in rows:groups.setdefault(row['T'],[]).append(row)
    strategy=runtime.state['strategies'][symbol]
    potential=[]
    for arm in runtime.state['arms'].values():
        pos=arm.positions.get(symbol)
        if pos:potential.append((pos.signal.side,pos.stop,pos.target,pos.opened,pos.rules['tick'],pos.be_pending,pos.be_effective_at,pos.triggered))
        order=arm.orders.get(symbol)
        if order and arm.name=='A':
            eligible=[r for r in rows if r['T']/1000>order.submitted]
            if eligible:
                first=eligible[0];prices={number(r['p']) for r in groups[first['T']]}
                if len(prices)>1:return True  # No invented favorable first price.
                side=order.signal.side
                potential.append((side,order.stop,order.target,first['T']/1000,order.rules['tick'],None,None,False))
    for ms,group in groups.items():
        prices={number(r['p']) for r in group}
        if len(prices)<2:continue
        at=ms/1000;lo,hi=min(prices),max(prices)
        for side,stop,target,opened,tick,be,effective,triggered in potential:
            if at<opened:continue
            if triggered:return True
            if be is not None and effective is not None and at>=effective:stop=max(stop,be) if side==1 else min(stop,be)
            # No order-sensitive fill occurs between protection levels. A BE
            # request has the same timestamp/stop in any ordering of this group.
            if (lo<=stop or hi>=target+tick) if side==1 else (hi>=stop or lo<=target-tick):return True
        for arm in runtime.state['arms'].values():
            o=arm.orders.get(symbol)
            if o and arm.name=='B' and at>=o.submitted:
                if (lo<=o.entry-o.rules['tick'] or lo<o.signal.extreme or hi>=o.target) if o.signal.side==1 else (hi>=o.entry+o.rules['tick'] or hi>o.signal.extreme or lo<=o.target):return True
        for side,e in strategy.sweeps.items():
            if side*(lo-e.extreme)<0 if side==1 else side*(hi-e.extreme)<0:return True
            primary=min([q for q in e.pools if q.cluster] or e.pools,key=lambda q:side*q.outer)
            if e.reclaim_tick is None and side*((hi if side==1 else lo)-primary.outer)>0:return True
            if e.break_tick is None and side*((hi if side==1 else lo)-e.anchor.price)>0:return True
        for side,s in strategy.setups.items():
            if (lo<s.extreme if side==1 else hi>s.extreme):return True
        for z in strategy.zones:
            if z.valid:
                def region(p):return (z.side*(p-z.mid)<=0,p<z.low if z.side==1 else p>z.high,p<z.high if z.side==1 else p>z.low)
                if region(lo)!=region(hi):return True
        for pool in strategy.liquidity.pools:
            if not pool.consumed and (hi>pool.outer if pool.kind==1 else lo<pool.outer):return True
    return False
