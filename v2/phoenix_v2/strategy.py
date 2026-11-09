from dataclasses import dataclass, field
from .model import D, displacement
from .structure import Structure
from .objects import Liquidity, new_zones

@dataclass
class Sweep:
    id: str
    side: int
    start: int
    candle: int
    population: set
    candidates: list
    pools: list
    extreme: D
    extreme_at: int

@dataclass
class Setup:
    id: str
    symbol: str
    side: int
    sweep_at: int
    reclaim: int
    extreme: D
    protected: D
    poi: object
    range_low: D
    range_high: D
    evidence: dict
    score: D = D(0)
    displacement_at: int | None = None

@dataclass(frozen=True)
class Signal:
    id: str
    symbol: str
    side: int
    at: int
    sweep_at: int
    stop: D
    target: D
    atr: D
    score: D
    extreme: D
    b_zone: object | None
    close: D
    evidence: dict

class Strategy:
    def __init__(self,symbol,journal):
        self.symbol=symbol; self.log=journal
        self.frames={t:Structure(symbol,t) for t in (5,15,60,240,1440)}
        self.zones=[]; self.liquidity=Liquidity(); self.sweeps={}; self.setups={}; self.last_price=None
        self.ready=False; self.quarantined=False
    def bias(self):
        h,d=self.frames[240],self.frames[1440]
        return h.regime if h.regime and h.leg and d.regime!=-h.regime else 0
    def warm(self): return all(s.atr.ready for s in self.frames.values()) and not self.quarantined
    def invalidate(self,at,reason):
        self.quarantined=True
        for s in list(self.setups.values()): self.log(at,'setup_cancel',id=s.id,reason=reason)
        self.setups.clear(); self.sweeps.clear()
    def price(self,p,at):
        self.last_price=p
        for z in self.zones:
            if z.available<=at: z.update(p)
        for side,s in list(self.setups.items()):
            if side*(p-s.extreme)<0 or not s.poi.valid:
                self.log(at,'setup_cancel',id=s.id,reason='swept_extreme_or_context'); del self.setups[side]
        hits=self.liquidity.price(p,at)
        for side in (1,-1):
            event=self.sweeps.get(side)
            if event and side*(p-event.extreme)<0: event.extreme=p; event.extreme_at=at
            pools=[x for x in hits if x.kind==-side]
            if not pools: continue
            self.log(at,'liquidity_consumed',symbol=self.symbol,pools=[x.key for x in pools],reserved=side in self.setups)
            if side in self.setups: continue  # R2D09: irrevocable at penetration
            if event:
                event.pools.extend(x for x in pools if x.key in event.population and any(z.valid and z.visits==v and z.distance(x.outer)<=D('.25')*atr for z,v,atr in event.candidates))
                continue
            if not self.warm() or self.bias()!=side: continue
            candidates=[]
            for z in self.zones:
                if z.tf not in (15,60) or z.available>at or z.side!=side or not z.valid or not z.inside or not(1<=z.visits<=2): continue
                atr=self.frames[z.tf].atr.value
                if atr and any(z.distance(x.outer)<=D('.25')*atr for x in pools):
                    candidates.append((z,z.visits,atr))
            if not candidates: continue
            pools=[x for x in pools if any(z.distance(x.outer)<=D('.25')*atr for z,_,atr in candidates)]
            population={x.key for x in self.liquidity.pools if x.available<=at and (not x.consumed or x in pools)}
            event=Sweep(f'{self.symbol}:{side}:{at}',side,at,at//300,population,candidates,list(pools),p,at)
            self.sweeps[side]=event
            self.log(at,'sweep_start',id=event.id,population=sorted(population),candidates=[(z.id,v,str(a)) for z,v,a in candidates])
    def cancel_bias(self,at):
        for side,s in list(self.setups.items()):
            if self.bias()!=side: self.log(at,'setup_cancel',id=s.id,reason='bias'); del self.setups[side]
        for side,e in list(self.sweeps.items()):
            if self.bias()!=side: self.log(at,'sweep_reject',id=e.id,reason='bias'); del self.sweeps[side]
    def install(self,bars):
        """Process HTF batch first; objects installed at end cannot support past price."""
        at=bars[0].end
        for b in sorted((b for b in bars if b.tf in self.frames),key=lambda b:-b.tf):
            s=self.frames[b.tf]
            for z in self.zones:
                if z.tf==b.tf and z.available<b.end: z.own_close(b)
            pivots,neutral=s.close(b)
            for p in pivots:
                self.log(at,'pivot_confirmed',id=p.id,origin=p.origin,available=p.available,excluded=p.id in s.exclusions)
                if b.tf in (15,60): self.liquidity.install(p,b.tf,s.atr.value)
            if neutral: self.log(at,'regime_neutral',symbol=self.symbol,tf=b.tf)
            fresh=new_zones(s)
            self.zones.extend(fresh)
            for z in fresh: self.log(at,'zone_confirmed',id=z.id,low=z.low,high=z.high,available=z.available)
        self.ready=self.warm(); self.cancel_bias(at)
    def five_close(self,b):
        signals=[]; at=b.end; frame=self.frames[5]
        # Existing reclaim snapshots only; same-candle reclaim/MSS is impossible.
        for side,s in list(self.setups.items()):
            age=(at-s.reclaim)//300
            if 1<=age<=12:
                if displacement(b,frame.atrs[-1],side):
                    score=abs(b.c-b.o)/frame.atrs[-1]
                    if score>s.score: s.score=score; s.displacement_at=at
                if s.displacement_at and side*(b.c-s.protected)>0:
                    targets=self.liquidity.targets(side,b.c)
                    if targets:
                        target=min(targets,key=lambda p:(side*p.approach,-p.tf,not p.cluster,p.available,p.id))
                        atr=frame.atr.value
                        stop=s.extreme-side*D('.1')*atr; tp=target.approach-side*D('.1')*atr
                        zones=[z for z in self.zones if z.tf==5 and z.side==side and z.valid and not z.midpoint_touched and s.reclaim<z.displacement_at<=at and z.available<=at]
                        fvgs=sorted((z for z in zones if z.kind=='FVG'),key=lambda z:(z.available,z.id))
                        obs=sorted((z for z in zones if z.kind=='OB' and z.origin>=s.reclaim-300),key=lambda z:(z.displacement_at,-z.origin,z.id))
                        zone=(fvgs or obs or [None])[0]
                        evidence=dict(s.evidence,target=target.key,poi=s.poi.id,protected=s.protected,displacement_at=s.displacement_at)
                        signals.append(Signal(s.id,self.symbol,side,at,s.sweep_at,stop,tp,atr,s.score,s.extreme,zone,b.c,evidence))
                        self.log(at,'mss',id=s.id,stop=stop,target=tp,score=s.score,b_zone=zone.id if zone else None)
                    else: self.log(at,'setup_reject',id=s.id,reason='no_target')
                    del self.setups[side]; continue
            if age>=12: self.log(at,'setup_expired',id=s.id); del self.setups[side]
        for side,e in list(self.sweeps.items()):
            age=(at//300-1)-e.candle+1
            clusters=[p for p in e.pools if p.cluster]
            primary=min(clusters or e.pools,key=lambda p:(side*p.outer,-p.tf,p.available,p.id))
            if side*(b.c-primary.outer)>0:
                h=self.frames[240]; leg=h.leg
                candidates=[]
                if leg and self.bias()==side:
                    lo,hi=sorted(p.price for p in leg); eq=(lo+hi)/2
                    for z,visit,atr in e.candidates:
                        if z.valid and z.visits==visit and visit<=2 and side*(eq-z.mid)>0 and z.distance(primary.outer)<=D('.25')*atr:
                            candidates.append(z)
                anchor=frame.mss_anchor(side,e.extreme,e.extreme_at,at)
                if candidates and anchor:
                    z=min(candidates,key=lambda z:(z.distance(primary.outer),-z.tf,-z.available,z.kind!='OB',z.id))
                    a,terminal,points=anchor
                    if side*(b.c-a.price)<=0:
                        evidence=dict(primary=primary.key,pools=[p.key for p in e.pools],sequence=[p.id for p in points],anchor=a.id,terminal=terminal.id,extreme_at=e.extreme_at,range=[lo,hi])
                        self.setups[side]=Setup(e.id,self.symbol,side,e.start,at,e.extreme,a.price,z,lo,hi,evidence)
                        self.log(at,'reclaim',id=e.id,evidence=evidence)
                    else: self.log(at,'sweep_reject',id=e.id,reason='reclaim_already_beyond')
                else: self.log(at,'sweep_reject',id=e.id,reason='context_range_or_structure')
                del self.sweeps[side]
            elif age>=3: self.log(at,'sweep_expired',id=e.id); del self.sweeps[side]
        return signals
