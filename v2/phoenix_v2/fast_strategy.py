"""RC3F experimental causal reversal. No profitability claim or manufactured fills."""
from dataclasses import asdict
from .model import D, displacement
from .structure import Structure, Pivot
from .strategy import Strategy, Sweep, Setup, Signal
from .objects import new_zones,Pool

PROFILE='RC3F-1'
SPEC=dict(version=PROFILE, pivot_left=2, pivot_right=2, daily_pivot_left_right=3,
          structural_lookback_bars=300,
          direction='1H closed break of a previously confirmed swing; 4H fallback; opposing Daily veto',
          fast='Observed sweep then reclaim/break; completed 5m close in terminal quarter and recovery >= prior ATR',
          delayed='Post-reclaim 1.5 ATR / 65% displacement and beyond-level close, twelve candles',
          activation='First evidenced post-decision trade, <=5s after activation and <MSS+60s; receipt age <=2s',
          risk='Unchanged 0.5% entry / 1.5% aggregate / three slots; independent A/B; gross 2R',
          context='Conservative own-frame extreme replay proves current zone visits/validity and untouched standalone pools; no historical signals or invented ticks',
          ordering='Ignore only commuting same-ms operations; quarantine ambiguous local fills/protection and simultaneous material cross-symbol account effects',
          research_certified=False)

class FastStructure(Structure):
    def __init__(self,symbol,tf):
        super().__init__(symbol,tf);self.raw_context=tf in (15,60);self.broken_at={}

    def bound(self):
        self.bars=self.bars[-300:];self.atrs=self.atrs[-300:];self.chain=self.chain[-300:]
        self.sequence_history=self.sequence_history[-300:]
        if self.bars:self.raw=[p for p in self.raw if p.origin>=self.bars[0].start]
        ids={p.id for p in self.raw};self.broken_at={k:v for k,v in self.broken_at.items() if k in ids}
        self.exclusions=[p for p in self.exclusions if p in ids]

    def sequence(self):
        if self.tf==1440:return super().sequence()
        hs=[p for p in self.chain if p.kind==1][-2:]
        ls=[p for p in self.chain if p.kind==-1][-2:]
        if len(hs)!=2 or len(ls)!=2:return 0,[]
        side=1 if hs[1].price>hs[0].price and ls[1].price>ls[0].price else -1 if hs[1].price<hs[0].price and ls[1].price<ls[0].price else 0
        return side,sorted(hs+ls,key=lambda p:p.origin)

    def close(self,bar):
        if self.tf==1440:
            result=super().close(bar);self.bound();return result
        if self.bars and bar.start!=self.bars[-1].end:raise ValueError('timeframe gap')
        # References are sampled before current close/confirmation, never from it.
        known=[p for p in self.raw if p.available<=bar.start]
        highs=[p for p in known if p.kind==1 and p.id not in self.broken_at];lows=[p for p in known if p.kind==-1 and p.id not in self.broken_at]
        high=highs[-1] if highs else None;low=lows[-1] if lows else None
        neutral=False
        if self.regime and self.leg and self.regime*(bar.c-self.leg[0].price)<0:
            self.regime=0;self.leg=None;self.transition=bar.end;neutral=True
        old=self.atr.value;self.bars.append(bar);self.atrs.append(old);self.atr.update(bar)
        confirmed=[]
        if len(self.bars)>=5:
            five=self.bars[-5:];mid=five[2]
            for kind,attr in ((1,'h'),(-1,'l')):
                if all(kind*(getattr(mid,attr)-getattr(x,attr))>0 for i,x in enumerate(five) if i!=2):
                    p=Pivot(f'{bar.symbol}:{bar.tf}:2R:{mid.start}:{kind}',kind,getattr(mid,attr),mid.start,bar.end)
                    self.raw.append(p);confirmed.append(p)
        if len(confirmed)==2:self.exclusions.extend(p.id for p in confirmed)
        else:
            for p in confirmed:
                if self.chain and self.chain[-1].kind==p.kind:
                    if p.kind*(p.price-self.chain[-1].price)>0:self.chain[-1]=p
                else:
                    if self.chain:self.chain[-1].frozen=bar.end
                    self.chain.append(p)
        seq,points=self.sequence()
        if seq:self.sequence_history.append((bar.end,seq,tuple(p.id for p in points)))
        if self.tf==60:
            for side,ref,anchor in ((1,high,low),(-1,low,high)):
                if not ref or not anchor or side*(bar.c-ref.price)<=0:continue
                if side*(bar.c-anchor.price)<=0:continue
                # Closed break candle supplies a known range endpoint, not a future pivot.
                endpoint=Pivot(f'{bar.symbol}:60:break:{bar.end}:{side}',side,bar.h if side==1 else bar.l,bar.start,bar.end)
                self.regime=side;self.leg=(anchor,endpoint)
        elif not neutral:
            leg=self.governing(seq) if seq else None
            if seq and leg and seq*(bar.c-leg[0].price)>=0:self.regime=seq;self.leg=leg
            elif self.regime:
                leg=self.governing(self.regime)
                if leg:self.leg=leg
        for p in known:
            if p.kind*(bar.c-p.price)>0:self.broken_at.setdefault(p.id,bar.end)
        self.bound()
        return confirmed,neutral

class FastStrategy(Strategy):
    def __init__(self,symbol,journal):
        super().__init__(symbol,journal)
        self.frames={t:FastStructure(symbol,t) for t in (5,15,60,240,1440)}
        self.profile=PROFILE

    def direction_frame(self):return self.frames[60] if self.frames[60].regime and self.frames[60].leg else self.frames[240]

    def bias(self):
        h=self.direction_frame();d=self.frames[1440]
        return h.regime if h.regime and h.leg and d.regime!=-h.regime else 0

    def price(self,p,at):
        self.last_price=p
        for z in self.zones:
            if z.available<=at:z.update(p)
        for side,s in list(self.setups.items()):
            if side*(p-s.extreme)<0 or not s.poi.valid:
                self.log(at,'setup_cancel',id=s.id,reason='swept_extreme_or_context');del self.setups[side]
        for side,e in self.sweeps.items():
            if side*(p-e.extreme)<0:
                e.extreme=p;e.extreme_at=at;e.reclaim_tick=None;e.break_tick=None
            primary=min([q for q in e.pools if q.cluster] or e.pools,key=lambda q:side*q.outer)
            if side*(p-primary.outer)>0 and e.reclaim_tick is None:e.reclaim_tick=at
            if e.reclaim_tick is not None and side*(p-e.anchor.price)>0 and at>e.start and e.break_tick is None:e.break_tick=at
        hits=self.liquidity.price(p,at)
        for side in (1,-1):
            pools=[q for q in hits if q.kind==-side]
            if not pools:continue
            self.log(at,'liquidity_consumed',symbol=self.symbol,pools=[q.key for q in pools],reserved=side in self.setups)
            reasons=[]
            if side in self.setups:reasons.append('setup_reserved')
            if not self.warm():reasons.append('structure_warmup')
            if self.bias()!=side:reasons.append('direction_or_daily_veto')
            candidates=[]
            for z in self.zones:
                if z.tf in (15,60) and z.available<=at and z.side==side and z.valid and z.inside and 1<=z.visits<=2:
                    atr=self.frames[z.tf].atr.value
                    if atr and any(z.distance(q.outer)<=D('.25')*atr for q in pools):candidates.append((z,z.visits,atr))
            if not candidates:reasons.append('no_eligible_visited_poi_near_pool')
            anchors=[q for q in self.frames[5].raw if q.kind==side and q.available<=at and q.id not in self.frames[5].broken_at]
            if not anchors:reasons.append('no_preexisting_5m_break_reference')
            self.log(at,'penetration_evaluation',symbol=self.symbol,side=side,pools=[q.key for q in pools],reasons=reasons,bias=self.bias(),poi_candidates=len(candidates))
            if reasons:continue
            if side in self.sweeps:
                e=self.sweeps[side]
                e.pools.extend(q for q in pools if q.key in e.population and any(z.valid and z.visits==v and z.distance(q.outer)<=D('.25')*a for z,v,a in e.candidates))
                continue
            pools=[q for q in pools if any(z.distance(q.outer)<=D('.25')*a for z,_,a in candidates)]
            e=Sweep(f'{PROFILE}:{self.symbol}:{side}:{at}',side,at,at//300,{q.key for q in self.liquidity.pools if q.available<=at and (not q.consumed or q in pools)},candidates,list(pools),p,at)
            e.anchor=anchors[-1];e.reclaim_tick=None;e.break_tick=None
            self.sweeps[side]=e
            self.log(at,'sweep_start',id=e.id,anchor=e.anchor.id,anchor_available=e.anchor.available,population=sorted(e.population),candidates=[z.id for z,_,_ in candidates])

    def make_signal(self,s,b,mode,score):
        targets=self.liquidity.targets(s.side,b.c)
        if not targets:
            self.log(b.end,'setup_reject',id=s.id,reason='no_target');return None
        target=min(targets,key=lambda q:(s.side*q.approach,-q.tf,not q.cluster,q.available,q.id))
        atr=self.frames[5].atr.value;side=s.side
        zones=[z for z in self.zones if z.tf==5 and z.side==side and z.valid and not z.midpoint_touched and
               z.available<=b.end and (z.displacement_at>=b.end if mode=='FAST_RECLAIM' else z.displacement_at>s.reclaim)]
        zones.sort(key=lambda z:(z.kind!='FVG',z.available,z.id));zone=zones[0] if zones else None
        stop=s.extreme-side*D('.1')*atr;tp=target.approach-side*D('.1')*atr
        evidence=dict(s.evidence,mode=mode,strategy_version=PROFILE,target=target.key,poi=s.poi.id,protected=s.protected)
        signal=Signal(s.id,self.symbol,side,b.end,s.sweep_at,stop,tp,atr,score,s.extreme,zone,b.c,evidence)
        self.log(b.end,'mss',id=s.id,mode=mode,stop=stop,target=tp,score=score,b_zone=zone.id if zone else None)
        return signal

    def five_close(self,b):
        at=b.end;signals=[];f=self.frames[5]
        for side,s in list(self.setups.items()):
            age=(at-s.reclaim)//300
            if 1<=age<=12:
                if displacement(b,f.atrs[-1],side):
                    score=abs(b.c-b.o)/f.atrs[-1]
                    if score>s.score:s.score=score;s.displacement_at=at
                if s.displacement_at and side*(b.c-s.protected)>0:
                    result=self.make_signal(s,b,'DELAYED_MSS',s.score)
                    if result:signals.append(result)
                    del self.setups[side];continue
            if age>=12:self.log(at,'setup_expired',id=s.id);del self.setups[side]
        for side,e in list(self.sweeps.items()):
            age=int((at//300-1)-e.candle+1)
            primary=min([q for q in e.pools if q.cluster] or e.pools,key=lambda q:side*q.outer)
            if side*(b.c-primary.outer)>0:
                leg=self.direction_frame().leg;candidates=[]
                if leg and self.bias()==side:
                    lo,hi=sorted(q.price for q in leg);eq=(lo+hi)/2
                    candidates=[z for z,v,a in e.candidates if z.valid and z.visits==v and v<=2 and side*(eq-z.mid)>0 and z.distance(primary.outer)<=D('.25')*a]
                if candidates:
                    z=min(candidates,key=lambda z:(z.distance(primary.outer),-z.tf,-z.available,z.kind!='OB',z.id))
                    evidence=dict(primary=primary.key,anchor=e.anchor.id,anchor_available=e.anchor.available,
                                  sweep_at=e.start,reclaim_tick=e.reclaim_tick,break_tick=e.break_tick,extreme_at=e.extreme_at,range=[str(lo),str(hi)])
                    s=Setup(e.id,self.symbol,side,e.start,at,e.extreme,e.anchor.price,z,lo,hi,evidence)
                    self.log(at,'reclaim',id=e.id,evidence=evidence)
                    atr=f.atrs[-1];span=b.h-b.l
                    terminal=(b.h-b.c if side==1 else b.c-b.l)
                    fast=bool(e.reclaim_tick is not None and e.break_tick is not None and e.extreme_at<e.break_tick and
                              side*(b.c-e.anchor.price)>0 and atr and side*(b.c-e.extreme)>=atr and span>0 and terminal<=span*D('.25'))
                    if fast:
                        result=self.make_signal(s,b,'FAST_RECLAIM',side*(b.c-e.extreme)/atr)
                        if result:signals.append(result)
                    else:
                        self.setups[side]=s
                        self.log(at,'setup_waiting',id=e.id,reason='await_post_reclaim_displacement')
                else:self.log(at,'sweep_reject',id=e.id,reason='poi_invalid_or_range_or_bias')
                del self.sweeps[side]
            elif age>=3:self.log(at,'sweep_expired',id=e.id);del self.sweeps[side]
        return signals


def rebuild_context(old,log,recent=(),as_of=None):
    """Reconstruct current survivors using order-independent extrema, not fake ticks.

    Every POI visit/separation is defined on its own timeframe. Any invalidating
    extremum invalidates the zone regardless of intrabar order. Only untouched
    standalone liquidity is seeded; historical cluster ordering is not guessed.
    """
    new=FastStrategy(old.symbol,log)
    for tf,original in old.frames.items():
        frame=new.frames[tf];pairs=list(zip(original.bars,original.atrs))[-300:]
        zones=[];pools=[]
        for b,prior_atr in pairs:
            if tf in (15,60):
                for z in zones:
                    if z.available<=b.start:
                        z.update(b.l if z.side==1 else b.h);z.own_close(b)
                for pool in pools:
                    if not pool.consumed and pool.available<=b.start and (b.h>pool.outer if pool.kind==1 else b.l<pool.outer):
                        pool.consumed=True;new.liquidity.consumed.add(pool.id)
            pivots,_=frame.close(b)
            frame.atrs[-1]=prior_atr  # Preserve the actual pre-candle Wilder series.
            if tf in (15,60):
                zones.extend(new_zones(frame))
                for pivot in pivots:
                    pool=Pool(pivot.id,tf,pivot.kind,[pivot],pivot.available)
                    pools.append(pool);new.liquidity.members[pivot.id]=pivot
        frame.atr=original.atr
        new.zones.extend(z for z in zones if z.valid and z.visits<=2)
        new.liquidity.pools.extend(p for p in pools if not p.consumed)
    as_of=as_of if as_of is not None else max([b.end for b in recent]+[f.bars[-1].end for f in new.frames.values() if f.bars]+[0])
    covered={b.start for b in recent}
    censored={tf for tf in (15,60) if new.frames[tf].bars and
              not set(range(new.frames[tf].bars[-1].end,as_of,60)).issubset(covered)}
    new.zones=[z for z in new.zones if z.tf not in censored]
    new.liquidity.pools=[q for q in new.liquidity.pools if q.tf not in censored]
    known={z.id:z for z in new.zones}
    for z in old.zones:
        if not z.valid or z.visits>2:continue
        if z.id not in known:new.zones.append(z)
        else:
            rebuilt=known[z.id];rebuilt.visits=max(rebuilt.visits,z.visits)
            rebuilt.midpoint_touched=rebuilt.midpoint_touched or z.midpoint_touched
            if rebuilt.kind=='FVG' and rebuilt.midpoint_touched:rebuilt.valid=False
    def identity(q,tf=None):return (tf if tf is not None else int(q.id.split(':')[1]),q.kind,q.origin,q.price)
    old_members={identity(q):q.id for q in old.liquidity.members.values()}
    for q in new.liquidity.members.values():
        prior_id=old_members.get(identity(q))
        if prior_id in old.liquidity.consumed:new.liquidity.consumed.add(q.id)
        if prior_id in old.liquidity.assigned:new.liquidity.assigned.add(q.id)
    ids={p.key for p in new.liquidity.pools}
    standalone={identity(p.members[0],p.tf) for p in new.liquidity.pools if not p.cluster}
    for pool in old.liquidity.pools:
        if not pool.cluster and identity(pool.members[0],pool.tf) in standalone:continue
        if not pool.consumed and pool.key not in ids:
            new.liquidity.pools.append(pool)
            for q in pool.members:new.liquidity.members[q.id]=q
    new.liquidity.consumed.update(old.liquidity.consumed)
    # Known consumed members cannot be revived by a reconstructed standalone.
    for p in new.liquidity.pools:
        if p.id in new.liquidity.consumed:p.consumed=True
    # Forming own-frame tails are covered only by known closed 1m extremes.
    # No visit separation can be inferred until the own-timeframe bar closes.
    for b in recent:
        for z in new.zones:
            end=new.frames[z.tf].bars[-1].end if new.frames[z.tf].bars else b.end
            if b.start>=end and z.available<=b.start:z.update(b.l if z.side==1 else b.h)
        for p in new.liquidity.pools:
            end=new.frames[p.tf].bars[-1].end if new.frames[p.tf].bars else b.end
            if b.start>=end and p.available<=b.start and (b.h>p.outer if p.kind==1 else b.l<p.outer):
                p.consumed=True;new.liquidity.consumed.add(p.id)
        for pid,q in new.liquidity.members.items():
            tf=int(q.id.split(':')[1]);end=new.frames[tf].bars[-1].end if new.frames[tf].bars else b.end
            if b.start>=end and q.available<=b.start and (b.h>q.price if q.kind==1 else b.l<q.price):new.liquidity.consumed.add(pid)
    new.zones=[z for z in new.zones if z.valid and z.visits<=2]
    new.liquidity.pools=[p for p in new.liquidity.pools if not p.consumed and p.id not in new.liquidity.consumed]
    new.last_price=old.last_price
    return new
