from dataclasses import dataclass, field
from .model import D, displacement

@dataclass
class Zone:
    id: str
    tf: int
    side: int
    kind: str
    low: D
    high: D
    origin: int
    available: int
    displacement_at: int
    valid: bool = True
    visits: int = 0
    inside: bool = False
    separation: int = 0
    midpoint_touched: bool = False
    first_eligible: bool = True

    @property
    def mid(self): return (self.low+self.high)/2
    def distance(self,p): return max(self.low-p,p-self.high,D(0))
    def update(self,p):
        if not self.valid: return
        if self.side*(p-self.mid)<=0: self.midpoint_touched=True
        if self.kind=='FVG' and self.midpoint_touched: self.valid=False
        if self.kind=='OB' and (p<self.low if self.side==1 else p>self.high): self.valid=False
        penetrated=p<self.high if self.side==1 else p>self.low
        if penetrated and not self.inside:
            if self.first_eligible or self.separation>=2:
                self.visits+=1; self.inside=True; self.separation=0; self.first_eligible=False
    def own_close(self,b):
        outside=b.l>self.high if self.side==1 else b.h<self.low
        self.separation=self.separation+1 if outside else 0
        if self.separation>=2: self.inside=False; self.first_eligible=False

def new_zones(structure):
    """Creation candles cannot count as visits. All break references predate open."""
    bars=structure.bars; n=len(bars); out=[]
    if n<3: return out
    a,b,c=bars[-3:]
    for side in (1,-1):
        low,high=(a.h,c.l) if side==1 else (c.h,a.l)
        if low<high and structure.prior_displacement(n-2,side):
            z=Zone(f'{c.symbol}:{c.tf}:FVG:{c.end}:{side}',c.tf,side,'FVG',low,high,a.start,c.end,b.end)
            z.first_eligible=(c.c>high if side==1 else c.c<low)
            out.append(z)
        # Last opposing candle, never more than five bars from origin.
        origins=[i for i in range(max(0,n-6),n-1) if bars[i].side==-side]
        if not origins: continue
        i=origins[-1]; origin=bars[i]
        if any(x.side==-side for x in bars[i+1:]): continue
        ds=[k for k in range(i+1,n) if structure.prior_displacement(k,side)]
        if not ds: continue
        pivots=[p for p in structure.chain if p.kind==side and p.frozen is not None and p.available<=c.start]
        if not pivots: continue
        ref=pivots[-1]
        prior_break=any(side*(x.c-ref.price)>0 for x in bars if ref.available<=x.start<c.start)
        if prior_break or side*(c.c-ref.price)<=0 or ds[0]>n-1: continue
        low,high=(origin.l,origin.o) if side==1 else (origin.o,origin.h)
        if low>=high: continue
        if any(x.l<low if side==1 else x.h>high for x in bars[i+1:]): continue
        z=Zone(f'{c.symbol}:{c.tf}:OB:{origin.start}:{side}',c.tf,side,'OB',low,high,origin.start,c.end,bars[ds[0]].end)
        z.first_eligible=(c.c>high if side==1 else c.c<low)
        out.append(z)
    return out

@dataclass
class Pool:
    id: str
    tf: int
    kind: int
    members: list
    available: int
    tolerance: D | None = None
    version: int = 1
    consumed: bool = False
    member_consumed: bool = False
    frozen: bool = False

    @property
    def cluster(self): return len(self.members)>1
    @property
    def outer(self): return max(p.price for p in self.members) if self.kind==1 else min(p.price for p in self.members)
    @property
    def approach(self): return min(p.price for p in self.members) if self.kind==1 else max(p.price for p in self.members)
    @property
    def key(self): return self.id+':v'+str(self.version)

class Liquidity:
    def __init__(self): self.pools=[]; self.members={}; self.consumed=set(); self.assigned=set()
    def install(self,p,tf,atr):
        self.members[p.id]=p
        standalone=Pool(p.id,tf,p.kind,[p],p.available); self.pools.append(standalone)
        if atr is None or atr<=0: return
        eligible=[x for x in self.pools if x.cluster and not x.frozen and not x.consumed and x.tf==tf and x.kind==p.kind]
        fits=[]
        for x in eligible:
            width=max([q.price for q in x.members]+[p.price])-min([q.price for q in x.members]+[p.price])
            if width<=x.tolerance: fits.append((width,x.available,x.id,x))
        if fits:
            x=min(fits,key=lambda row:row[:3])[3]; x.members.append(p); x.version+=1; self.assigned.add(p.id); return
        candidates=[x for x in self.pools if not x.cluster and x.tf==tf and x.kind==p.kind and x.id!=p.id and x.id not in self.assigned and x.id not in self.consumed and abs(x.outer-p.price)<=D('.1')*atr]
        if candidates:
            old=max(candidates,key=lambda x:(x.available,x.id))
            self.pools.append(Pool('cluster:'+old.id+':'+p.id,tf,p.kind,[old.members[0],p],p.available,D('.1')*atr))
            self.assigned.update((old.id,p.id))
    def price(self,p,at):
        hits=[]
        for x in self.pools:
            if x.available>at or x.consumed: continue
            if x.kind*(p-x.outer)>0:
                x.consumed=True; x.frozen=True; hits.append(x)
        # Members share consumption even if cluster outer is still untouched.
        for pid,q in self.members.items():
            if q.available<=at and q.kind*(p-q.price)>0: self.consumed.add(pid)
        for x in self.pools:
            if x.cluster and any(q.id in self.consumed for q in x.members): x.member_consumed=True
        return hits
    def targets(self,side,price):
        return [p for p in self.pools if p.kind==side and not p.consumed and not p.member_consumed
                and not any(m.id in self.consumed for m in p.members) and side*(p.approach-price)>0]
