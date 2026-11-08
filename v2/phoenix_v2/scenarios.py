"""Propagate supplied admissible common-market branches without hindsight.

This does not claim to reconstruct all paths from OHLC. An episode must carry
an externally audited admissibility/coverage record; otherwise it is incomplete.
"""
import copy
from .model import digest, canonical

class Scenarios:
    def __init__(self,engine,max_branches=128):
        self.branches={'root':engine};self.max_branches=max_branches;self.episodes=[];self.incomplete=[]
        self.primary={a:'root' for a in ('A','B')}
    @staticmethod
    def objective(engine,arm,sequence):
        a=engine.arms[arm];at=engine.previous
        try: eq=a.equity(at)
        except ValueError: raise ValueError('unpriceable ambiguity state')
        risk=sum((o.reserved_risk for o in a.orders.values()),start=0)+sum((p.planned_risk for p in a.positions.values()),start=0)
        return eq,-risk,not a.locked,-a.streak,sequence
    def episode(self,alternatives,evidence):
        if not alternatives or not evidence.get('coverage_review') or not evidence.get('sha256'):
            self.incomplete.append('ADMISSIBLE_SCENARIO_COVERAGE_UNAVAILABLE');return False
        if len(self.branches)*len(alternatives)>self.max_branches:
            self.incomplete.append('SCENARIO_RESOURCE_LIMIT');return False
        old_primary=dict(self.primary);new={};parents={};sequences={}
        for parent,engine in sorted(self.branches.items()):
            for alt in sorted(alternatives,key=lambda x:x['id']):
                child=copy.deepcopy(engine);key=parent+'/'+alt['id']
                for batch in alt['batches']:child.batch(batch)
                new[key]=child;parents[key]=parent;sequences[key]=canonical(alt['batches'])
        # Choose the local adverse outgoing state only from the incoming primary.
        # Keep every alternative for subsequent capacity/lock/acceptance bounds.
        for arm in ('A','B'):
            candidates=[k for k in new if parents[k]==old_primary[arm]]
            self.primary[arm]=min(candidates,key=lambda k:self.objective(new[k],arm,sequences[k]))
        self.branches=new
        self.episodes.append(dict(evidence=evidence,ids=sorted(new),primary=dict(self.primary)))
        return True
    def bounds(self):
        arms={};paired=[]
        for arm in ('A','B'):
            values=[e.arms[arm].equity(e.previous) for e in self.branches.values()]
            arms[arm]=dict(minimum=min(values),maximum=max(values),primary=self.primary[arm])
        for key,e in self.branches.items():
            paired.append((key,e.arms['A'].equity(e.previous)-e.arms['B'].equity(e.previous)))
        return dict(arms=arms,paired_difference_min=min(x[1] for x in paired),paired_difference_max=max(x[1] for x in paired),
                    paired_scenarios=paired,incomplete=list(self.incomplete),superiority='NOT_PREREGISTERED')
