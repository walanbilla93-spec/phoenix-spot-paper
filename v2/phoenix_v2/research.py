"""RC2 deterministic resampling and descriptive diagnostics; no acceptance shortcut."""
import hashlib
from math import ceil
from datetime import datetime, timezone
from collections import Counter, defaultdict
from calendar import monthrange
from .model import D

def indices(n,replicates=10000):
    if n<14: raise ValueError('need at least 14 UTC grid days')
    b=n-13; limit=(2**64//b)*b
    for j in range(1,replicates+1):
        row=[]
        for k in range(1,ceil(n/14)+1):
            c=0
            while True:
                u=int.from_bytes(hashlib.sha256(f'PhoenixV2RC2|20261108|{n}|{j}|{k}|{c}'.encode()).digest()[:8],'big')
                if u<limit: break
                c+=1
            start=u%b; row.extend(range(start,start+14))
        yield row[:n]

def interval(grid):
    values=[]
    for row in indices(len(grid)):
        numerator=sum((grid[i][0] for i in row),D(0)); count=sum(grid[i][1] for i in row)
        if count==0: return None
        values.append(numerator/count)
    values.sort(); return values[124],values[9874]

def descriptive(completed):
    gains=sum((x['net'] for x in completed if x['net']>0),D(0)); losses=-sum((x['net'] for x in completed if x['net']<0),D(0))
    return dict(completed=len(completed),net=gains-losses,mean_R=sum((x['net']/x['price_risk'] for x in completed),D(0))/len(completed) if completed else None,
                profit_factor=gains/losses if losses else ('infinity' if gains else None),disposition='INCOMPLETE_UNTIL_ALL_RC2_GATES_AND_BOUNDARY_COHORTS_RECONCILED')

def support(rows):
    symbols=Counter(x['symbol'] for x in rows)
    sweeps={x.get('sweep_id',x['id']) for x in rows}
    weeks={datetime.fromtimestamp(x['exit_at'],timezone.utc).isocalendar()[:2] for x in rows}
    return dict(completed=len(rows),original_sweeps=len(sweeps),utc_exit_weeks=len(weeks),per_symbol=dict(symbols),
                passes=len(rows)>=300 and len(sweeps)>=100 and len(weeks)>=50 and sum(n>=20 for n in symbols.values())>=5)

def add_months(timestamp,months):
    t=datetime.fromtimestamp(timestamp,timezone.utc)
    if (t.hour,t.minute,t.second)!=(0,0,0):raise ValueError('endpoint must be a UTC day boundary')
    m=t.month-1+months
    year=t.year+m//12;month=m%12+1
    return int(t.replace(year=year,month=month,day=min(t.day,monthrange(year,month)[1])).timestamp())

class Holdout:
    """Support-only extension choice precedes releasing performance reports."""
    def __init__(self,start,end12,end18,freeze_hash):
        if not freeze_hash or not start<end12<end18:raise ValueError('unfrozen research protocol')
        self.start=start;self.end12=end12;self.end18=end18;self.end=end12;self.freeze_hash=freeze_hash
        self.choice=None;self.performance_exposed=False;self.boundary=None
    def counts_only(self,arms,at):
        if at!=self.end12 or self.choice is not None or self.performance_exposed:raise ValueError('invalid counts-only decision')
        counts={name:dict(support(arm.completed),open_positions=len(arm.positions),data_complete=arm.data_valid) for name,arm in arms.items()}
        extend=any(not x['passes'] for x in counts.values())
        self.choice='EXTEND_COMMON_SIX_MONTHS' if extend else 'STOP_NEW_ENTRIES'
        self.end=self.end18 if extend else self.end12
        return dict(at=at,counts=counts,choice=self.choice,freeze_hash=self.freeze_hash)
    def endpoint(self,arms,at):
        if self.choice is None or at!=self.end:raise ValueError('endpoint not determined')
        self.boundary={name:dict(positions=sorted(arm.positions),orders=sorted(arm.orders),support=support(arm.completed)) for name,arm in arms.items()}
        for arm in arms.values():arm.ended=True;arm.halt(at,'scheduled_end')
        return self.boundary
    def can_finalize(self,arms):
        return self.boundary is not None and all(not a.positions and not a.orders and a.data_valid for a in arms.values())

def acceptance(rows,equity_grid,day_grid,verification_complete=False,open_liabilities=0,stress=None,ambiguity=None):
    """Per-arm gate calculation; incomplete evidence can never become acceptance."""
    stats=descriptive(rows);counts=support(rows);issues=[]
    if not verification_complete:issues.append('VERIFICATION_OR_FIDELITY_INCOMPLETE')
    if open_liabilities:issues.append('UNRESOLVED_BOUNDARY_LIABILITIES')
    if not equity_grid or any(x<=0 for x in equity_grid):issues.append('VALUATION_INCOMPLETE')
    ci=interval(day_grid) if len(day_grid)>=14 else None
    if ci is None:issues.append('INTERVAL_UNAVAILABLE')
    peak=None;dd=D(0)
    for eq in equity_grid:
        if eq<=0:continue
        peak=max(peak or eq,eq);dd=max(dd,(peak-eq)/peak)
    by_symbol=defaultdict(lambda:D(0));by_month=defaultdict(lambda:D(0))
    for row in rows:
        by_symbol[row['symbol']]+=row['net'];by_month[datetime.fromtimestamp(row['exit_at'],timezone.utc).strftime('%Y-%m')]+=row['net']
    best_symbol=min(by_symbol,key=lambda s:(-by_symbol[s],s)) if by_symbol else None
    best_month=min(by_month,key=lambda s:(-by_month[s],s)) if by_month else None
    breadth=bool(rows) and stats['net']-by_symbol[best_symbol]>0 and stats['net']-by_month[best_month]>0
    pf=stats['profit_factor'];economic=stats['net']>0 and stats['mean_R'] is not None and stats['mean_R']>=D('.1') and (pf=='infinity' or pf is not None and pf>=D('1.2'))
    stress_ok=stress is not None and stress.get('net',0)>0 and stress.get('mean_R',0)>0
    ambiguity_ok=ambiguity is not None and ambiguity.get('opportunities',0)>0 and ambiguity.get('filled',0)>0 and D(ambiguity['decision_ambiguous'])/ambiguity['opportunities']<=D('.01') and D(ambiguity['ordering_ambiguous'])/ambiguity['filled']<=D('.01') and ambiguity.get('all_material_alternatives_pass') is True
    if stress is None:issues.append('STRESS_UNAVAILABLE')
    if ambiguity is None:issues.append('AMBIGUITY_BOUNDS_UNAVAILABLE')
    gates=dict(economic=economic,confidence=ci is not None and ci[0]>0,stress=stress_ok,drawdown=bool(equity_grid) and dd<=D('.15'),breadth=breadth,ambiguity=ambiguity_ok)
    disposition='INVALID_INCOMPLETE_EVIDENCE' if issues else 'INCONCLUSIVE_SUPPORT' if not counts['passes'] else 'RESEARCH_ACCEPTED' if all(gates.values()) else 'NOT_ACCEPTED'
    return dict(stats=stats,support=counts,confidence=ci,drawdown=dd,by_symbol=dict(by_symbol),by_utc_exit_month=dict(by_month),gates=gates,issues=issues,disposition=disposition)
