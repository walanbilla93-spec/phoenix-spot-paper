from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from .model import D, floor, ceil, slip, number, adverse_round

def risk_day(at): return (datetime.fromtimestamp(at,timezone.utc)+timedelta(minutes=330)).date().isoformat()

@dataclass
class Order:
    id: str
    signal: object
    qty: D
    entry: D
    stop: D
    target: D
    unit_risk: D
    reserved_risk: D
    reserved_margin: D
    submitted: int
    expires: int
    rules: dict

@dataclass
class Position:
    id: str
    signal: object
    qty: D
    original_qty: D
    entry: D
    initial_stop: D
    stop: D
    target: D
    r: D
    planned_risk: D
    margin: D
    opened: int
    rules: dict
    fees: D = D(0)
    funding: D = D(0)  # signed cashflow
    realized: D = D(0)
    be_pending: D | None = None
    be_requested_at: int | None = None
    be_effective_at: int | None = None
    triggered: bool = False

class Arm:
    def __init__(self,name,manifest,log):
        self.name=name; self.manifest=manifest; self.log=log
        self.cash=D(str(manifest.data.get('capital','1000'))); self.day_start=self.cash
        self.day=None; self.locked=False; self.streak=0; self.orders={}; self.positions={}; self.completed=[]
        self.marks={}; self.mark_times={}; self.data_valid=True; self.ended=False; self.opportunities=[]
    def event(self,at,event,**data):
        data.setdefault('arm',self.name)
        self.log(at,event,**data)
    def equity(self,at):
        total=self.cash
        for symbol,p in self.positions.items():
            max_age=self.manifest.data.get('mark_max_age_seconds')
            if max_age is None or symbol not in self.marks or self.mark_times[symbol]>at or at-self.mark_times[symbol]>max_age:
                raise ValueError('missing/stale mark')
            total+=p.signal.side*(self.marks[symbol]-p.entry)*p.qty*p.rules['multiplier']
        return total
    def reset(self,at):
        day=risk_day(at)
        if self.day!=day:
            self.day_start=self.equity(at); self.day=day; self.locked=False; self.streak=0
            self.event(at,'risk_day_reset',day=day,equity=self.day_start)
    def cancel(self,symbol,at,reason):
        o=self.orders.pop(symbol,None)
        if o: self.event(at,'order_cancelled',id=o.id,reason=reason)
    def halt(self,at,reason):
        for symbol in list(self.orders): self.cancel(symbol,at,reason)
    def budgets(self,at):
        eq=self.equity(at); risk=sum((x.reserved_risk for x in self.orders.values()),D(0))+sum((p.planned_risk for p in self.positions.values()),D(0))
        notional=sum((o.qty*max(o.entry,self.marks.get(s,o.entry))*o.rules['multiplier'] for s,o in self.orders.items()),D(0))+sum((p.qty*self.marks[s]*p.rules['multiplier'] for s,p in self.positions.items()),D(0))
        margin=sum((o.reserved_margin for o in self.orders.values()),D(0))+sum((p.margin for p in self.positions.values()),D(0))
        return eq,risk,notional,margin
    def checkpoint(self,at,biases,atrs):
        try:
            eq=self.equity(at)
            if eq<=0 or eq<=self.day_start*D('.98') or self.streak>=5: self.locked=True
            if self.locked or not self.data_valid or self.ended or self.manifest.missing(): self.halt(at,'risk_or_data_gate')
            for symbol,o in list(self.orders.items()):
                if at<o.submitted:continue
                max_age=self.manifest.data.get('mark_max_age_seconds')
                if max_age is None or symbol not in self.marks or self.mark_times[symbol]>at or at-self.mark_times[symbol]>max_age:
                    self.cancel(symbol,at,'pending_mark_unavailable');continue
                if biases.get(symbol)!=o.signal.side: self.cancel(symbol,at,'bias'); continue
                if at>=o.expires: self.cancel(symbol,at,'expiry'); continue
                try:
                    r=self.manifest.rules(symbol,at); cost=self.cost(o.entry,o.stop,atrs[symbol],o.signal.side,r)
                    if o.qty*cost>eq*D('.005'): self.cancel(symbol,at,'individual_risk'); continue
                    # Changes in venue rules/fees require cancel, never hindsight resize.
                    if r!=o.rules: self.cancel(symbol,at,'rule_version_changed'); continue
                    o.unit_risk=cost; o.reserved_risk=o.qty*cost
                except (ValueError,KeyError): self.cancel(symbol,at,'estimate_unavailable')
            for o in sorted(list(self.orders.values()),key=lambda o:(o.submitted,o.id),reverse=True):
                eq,risk,notional,margin=self.budgets(at)
                free=min([x.rules['free_collateral'] for x in self.orders.values()]+[x.rules['free_collateral'] for x in self.positions.values()] or [eq])
                if risk>eq*D('.015') or notional>eq*3 or margin>free: self.cancel(o.signal.symbol,at,'aggregate_capacity')
        except ValueError:
            self.data_valid=False; self.halt(at,'equity_unavailable')
    @staticmethod
    def cost(entry,stop,atr,side,rules):
        estimated_stop=adverse_round(stop-side*slip(stop,atr),rules['tick'],-side)
        cost=(side*(entry-estimated_stop)+rules['taker']*(entry+estimated_stop))*rules['multiplier']
        if cost<=0: raise ValueError('nonpositive risk')
        return cost
    def reserve(self,signal,at,reference):
        symbol=signal.symbol; side=signal.side
        def reject(reason):
            self.opportunities.append((signal.id,reason)); self.event(at,'opportunity',id=signal.id,status=reason); return None
        if self.manifest.missing(): return reject('verification_blocked')
        if self.locked or not self.data_valid or self.ended: return reject('locked_or_incomplete')
        if symbol in self.positions or symbol in self.orders: return reject('coin_reserved')
        if len(self.positions)+len(self.orders)>=3: return reject('slots')
        if self.name=='B' and signal.b_zone is None: return reject('no_zone')
        try:
            rules=self.manifest.rules(symbol,at); eq,risk,notional,margin=self.budgets(at)
            if symbol not in self.marks or at-self.mark_times[symbol]>self.manifest.data['mark_max_age_seconds']: return reject('mark_unavailable')
            tick=rules['tick']; stop=floor(signal.stop,tick) if side==1 else ceil(signal.stop,tick)
            target=floor(signal.target,tick) if side==1 else ceil(signal.target,tick)
            if self.name=='A': entry=adverse_round(reference+side*slip(reference,signal.atr),tick,side)
            else:
                entry=floor(signal.b_zone.mid,tick) if side==1 else ceil(signal.b_zone.mid,tick)
                if side*(reference-entry)<=0: return reject('marketable_limit')
            if side*(reference-stop)<=0 or side*(target-reference)<=0: return reject('reference_outside_geometry')
            r=side*(entry-stop); reward=side*(target-entry)
            if r<=0 or reward<2*r: return reject('gross_2R')
            cost=self.cost(entry,stop,signal.atr,side,rules)
            m=rules['multiplier']; notional_per=max(entry,self.marks[symbol])*m
            margin_per=notional_per*rules['initial_margin']+(entry+stop)*m*rules['taker']
            free=min([rules['free_collateral']]+[x.rules['free_collateral'] for x in self.orders.values()]+[p.rules['free_collateral'] for p in self.positions.values()])
            qty=floor(min(eq*D('.005')/cost,(eq*D('.015')-risk)/cost,(eq*3-notional)/notional_per,(free-margin)/margin_per),rules['step'])
            if qty<=0 or qty<rules['minimum_qty'] or qty*entry*m<rules['minimum_notional']: return reject('minimum_or_capacity')
            o=Order(self.name+':'+signal.id,signal,qty,entry,stop,target,cost,qty*cost,qty*margin_per,at,signal.at+3600,rules)
            self.orders[symbol]=o; self.opportunities.append((signal.id,'reserved'))
            self.event(at,'order_reserved',id=o.id,entry=entry,stop=stop,target=target,qty=qty,equity=eq,atr=signal.atr,unit_risk=cost,risk=o.reserved_risk,margin=o.reserved_margin,rules=rules)
            return o
        except (ValueError,KeyError): return reject('rules_or_equity_unavailable')
    def fill_entry(self,symbol,at,qty,price,atr,source):
        """Explicit evidenced quantity; touch never implies full fill."""
        o=self.orders.get(symbol)
        if o is None: return
        side=o.signal.side
        if qty<=0 or qty>o.qty or at<o.submitted: raise ValueError('invalid entry quantity/time')
        if self.name=='B' and price!=o.entry: raise ValueError('limit baseline has no improvement')
        c=self.cost(price,o.stop,atr,side,o.rules); eq=self.equity(at)
        if qty*c>eq*D('.005') or side*(price-o.stop)<=0 or side*(o.target-price)<2*side*(price-o.stop):
            self.cancel(symbol,at,'fill_recheck'); return
        fee=qty*price*o.rules['multiplier']*o.rules['taker']; self.cash-=fee
        p=Position(o.id,o.signal,qty,qty,price,o.stop,o.stop,o.target,side*(price-o.stop),qty*c,o.reserved_margin*qty/o.qty,at,o.rules,fees=fee)
        self.positions[symbol]=p; del self.orders[symbol]
        self.event(at,'entry_fill',id=p.id,qty=qty,price=price,fee=fee,source=source,remainder_cancelled=o.qty-qty)
    def attempt_a(self,signal,at,reference,evidence):
        o=self.orders.get(signal.symbol)
        if o is None: return
        side=signal.side
        window=self.manifest.data.get('a_activation_window_seconds',0)
        timely=(signal.at<=at<signal.at+window and at>=o.submitted) if window else at==signal.at
        if not timely or side*(reference-o.stop)<=0 or side*(o.target-reference)<=0:
            self.cancel(signal.symbol,at,'immediate_reference_invalid'); return
        entry=adverse_round(reference+side*slip(reference,signal.atr),o.rules['tick'],side)
        cost=self.cost(entry,o.stop,signal.atr,side,o.rules)
        eq,risk,notional,margin=self.budgets(at)
        m=o.rules['multiplier']; unit_notional=max(entry,self.marks[signal.symbol])*m
        unit_margin=unit_notional*o.rules['initial_margin']+(entry+o.stop)*m*o.rules['taker']
        old_notional=o.qty*max(o.entry,self.marks[signal.symbol])*m
        qty=floor(min(o.qty,o.reserved_risk/cost,eq*D('.005')/cost,
                      (eq*D('.015')-risk+o.reserved_risk)/cost,
                      (eq*3-notional+old_notional)/unit_notional,
                      (o.rules['free_collateral']-margin+o.reserved_margin)/unit_margin),o.rules['step'])
        if qty<=0 or qty<o.rules['minimum_qty'] or qty*entry*m<o.rules['minimum_notional'] or side*(entry-o.stop)<=0 or side*(o.target-entry)<2*side*(entry-o.stop):
            self.cancel(signal.symbol,at,'immediate_geometry_or_capacity'); return
        o.qty=qty; o.entry=entry; o.unit_risk=cost; o.reserved_risk=qty*cost; o.reserved_margin=qty*unit_margin
        if evidence and evidence.get('evidence'):
            self.fill_entry(signal.symbol,at,min(number(evidence['qty']),qty),entry,signal.atr,evidence['evidence'])
        else: self.cancel(signal.symbol,at,'market_no_fill_evidence')
    def exit_fill(self,symbol,at,qty,price,reason,fee_rate=None):
        p=self.positions[symbol]
        if qty<=0 or qty>p.qty: raise ValueError('over-close')
        fee=qty*price*p.rules['multiplier']*(p.rules['taker'] if fee_rate is None else fee_rate)
        gross=p.signal.side*(price-p.entry)*qty*p.rules['multiplier']
        self.cash+=gross-fee; p.realized+=gross; p.fees+=fee; p.qty-=qty
        self.event(at,'exit_fill',id=p.id,qty=qty,price=price,reason=reason,fee=fee,residual=p.qty)
        if p.qty==0:
            net=p.realized-p.fees+p.funding
            row=dict(id=p.id,sweep_id=p.signal.id,symbol=symbol,arm=self.name,exit_at=at,net=net,price_risk=p.original_qty*p.r*p.rules['multiplier'],planned_risk=p.planned_risk)
            self.completed.append(row)
            if net<0: self.streak+=1
            elif net>0: self.streak=0
            if self.streak>=5: self.locked=True; self.halt(at,'five_losses')
            self.event(at,'position_closed',**row); del self.positions[symbol]
    def funding(self,symbol,at,rate,valuation,evidence):
        if not evidence: raise ValueError('funding evidence required')
        if valuation<=0:raise ValueError('invalid settlement valuation')
        p=self.positions.get(symbol)
        if p:
            flow=-p.signal.side*p.qty*valuation*p.rules['multiplier']*rate
            p.funding+=flow; self.cash+=flow; self.event(at,'funding',id=p.id,cashflow=flow,evidence=evidence)
    def exit_group(self,exits,at,ordered=False):
        incoming=self.streak;begin=len(self.completed)
        rows=exits if ordered else sorted(exits,key=lambda x:(x['symbol'],x['arm']))
        for x in rows:self.exit_fill(x['symbol'],at,number(x['qty']),number(x['price']),x['reason'])
        if not ordered:
            outcomes=[x['net'] for x in self.completed[begin:]];losses=sum(x<0 for x in outcomes);profits=any(x>0 for x in outcomes)
            if incoming+losses>=5:self.locked=True;self.halt(at,'simultaneous_loss_envelope')
            self.streak=losses if profits else incoming+losses
            if len(outcomes)>1:self.event(at,'simultaneous_streak_envelope',incoming=incoming,losses=losses,profits=profits,ending=self.streak,locked=self.locked)
    def price(self,symbol,at,price,atr,exit_quantity=None,entry_quantity=None):
        p=self.positions.get(symbol)
        if p:
            if p.be_pending is not None and at>p.be_requested_at and at>=p.be_effective_at:
                if p.signal.side*(p.be_pending-p.stop)>0: p.stop=p.be_pending
                p.be_pending=None; self.event(at,'be_effective',id=p.id,stop=p.stop)
            side=p.signal.side
            if side*(price-p.stop)<=0 or p.triggered:
                p.triggered=True
                if exit_quantity is not None:
                    fill=adverse_round(price-side*slip(price,atr),p.rules['tick'],-side)
                    self.exit_fill(symbol,at,min(exit_quantity,p.qty),fill,'stop')
                else:
                    self.data_valid=False;self.halt(at,'protection_quantity_unavailable');self.event(at,'protection_quantity_unavailable',id=p.id)
            elif side*(price-p.target)>=p.rules['tick']:
                if exit_quantity is not None: self.exit_fill(symbol,at,min(exit_quantity,p.qty),p.target,'tp')
                else: self.event(at,'tp_quantity_unavailable',id=p.id)
            elif side*(price-p.entry)>=D('1.5')*p.r:
                be=floor(p.entry,p.rules['tick']) if side==1 else ceil(p.entry,p.rules['tick'])
                if side*(be-p.stop)>0 and p.be_pending is None:
                    latency=self.manifest.data.get('amendment_latency_seconds',0 if self.manifest.fixture else None)
                    if latency is None:
                        self.data_valid=False;self.halt(at,'amendment_latency_unverified');self.event(at,'be_latency_unavailable',id=p.id)
                    else:
                        p.be_pending=be;p.be_requested_at=at;p.be_effective_at=at+latency
                        self.event(at,'be_requested',id=p.id,analytical=p.entry,submitted=be,effective_no_earlier_than=p.be_effective_at)
        o=self.orders.get(symbol)
        if o:
            if at<o.submitted:return
            side=o.signal.side; zone=o.signal.b_zone
            if side*(price-o.signal.extreme)<0 or side*(price-o.target)>=0:
                self.cancel(symbol,at,'extreme_or_target'); return
            if self.name=='B' and zone and (price<zone.low if side==1 else price>zone.high):
                self.cancel(symbol,at,'zone_breach'); return
            if self.name=='B' and side*(o.entry-price)>=o.rules['tick'] and entry_quantity is not None:
                self.fill_entry(symbol,at,min(entry_quantity,o.qty),o.entry,atr,'evidenced_quantity')
