from dataclasses import dataclass
from .model import D, ATR, displacement

@dataclass
class Pivot:
    id: str
    kind: int  # high +1 / low -1
    price: D
    origin: int
    available: int
    frozen: int | None = None

class Structure:
    def __init__(self, symbol, tf):
        self.symbol=symbol; self.tf=tf; self.bars=[]; self.atrs=[]; self.atr=ATR()
        self.raw=[]; self.chain=[]; self.exclusions=[]; self.regime=0; self.leg=None
        self.transition=-1; self.sequence_history=[]

    def sequence(self):
        frozen=[p for p in self.chain if p.frozen is not None]
        hs=[p for p in frozen if p.kind==1][-3:]; ls=[p for p in frozen if p.kind==-1][-3:]
        if len(hs)!=3 or len(ls)!=3: return 0, []
        side=1 if all(a.price<b.price for pts in (hs,ls) for a,b in zip(pts,pts[1:])) else -1 if all(a.price>b.price for pts in (hs,ls) for a,b in zip(pts,pts[1:])) else 0
        return side, sorted(hs+ls,key=lambda p:p.origin)

    def governing(self, side):
        pairs=[]
        for i in range(2,len(self.chain)-1):
            a,b=self.chain[i:i+2]
            if a.kind != -side or a.frozen is None or a.origin >= b.origin: continue
            prev_a=self.chain[i-2]; prev_b=self.chain[i-1]
            if side*(a.price-prev_a.price)>0 and side*(b.price-prev_b.price)>0: pairs.append((a,b))
        return max(pairs,key=lambda ab:(ab[1].origin,ab[0].origin)) if pairs else None

    def close(self, bar):
        """Old anchors invalidate first. Pivots confirm only at third right close."""
        if self.bars and bar.start != self.bars[-1].end: raise ValueError('timeframe gap')
        old=self.atr.value; neutral=False
        if self.regime and self.leg and self.regime*(bar.c-self.leg[0].price)<0:
            self.regime=0; self.leg=None; self.transition=bar.end; neutral=True
        self.bars.append(bar); self.atrs.append(old); self.atr.update(bar)
        confirmed=[]
        if len(self.bars)>=7:
            seven=self.bars[-7:]; mid=seven[3]
            for kind, attr in ((1,'h'),(-1,'l')):
                value=getattr(mid,attr)
                if all(kind*(value-getattr(x,attr))>0 for i,x in enumerate(seven) if i!=3):
                    p=Pivot(f'{self.symbol}:{self.tf}:{mid.start}:{kind}',kind,value,mid.start,bar.end)
                    self.raw.append(p); confirmed.append(p)
        if len(confirmed)==2:
            self.exclusions.extend(p.id for p in confirmed)
        else:
            for p in confirmed:
                if self.chain and self.chain[-1].kind==p.kind:
                    if p.kind*(p.price-self.chain[-1].price)>0: self.chain[-1]=p
                else:
                    if self.chain: self.chain[-1].frozen=bar.end
                    self.chain.append(p)
        seq, points=self.sequence()
        if seq: self.sequence_history.append((bar.end,seq,tuple(p.id for p in points)))
        if not neutral:
            leg=self.governing(seq) if seq else None
            fresh=self.transition<0 or any(p.frozen is not None and p.frozen>self.transition for p in points)
            if seq and leg and fresh and seq*(bar.c-leg[0].price)>=0:
                self.regime=seq; self.leg=leg
            elif self.regime:
                new=self.governing(self.regime)
                if new: self.leg=new
        return confirmed, neutral

    def mss_anchor(self, side, extreme, extreme_at, reclaim):
        seq,points=self.sequence()
        if seq != -side: return None
        anchors=[p for p in self.chain if p.kind==side and p.frozen is not None]
        if not anchors: return None
        a=anchors[-1]; i=self.chain.index(a)
        if i<2 or i+1>=len(self.chain): return None
        b=self.chain[i+1]
        if not(a.origin<b.origin and a.available<=reclaim and b.available<=reclaim): return None
        if (-side)*(b.price-self.chain[i-1].price)<=0: return None
        if extreme_at < b.origin or side*(extreme-b.price)>0: return None
        if any(t>a.available and direction==side for t,direction,_ in self.sequence_history): return None
        return a,b,points

    def prior_displacement(self, index, side): return displacement(self.bars[index],self.atrs[index],side)
