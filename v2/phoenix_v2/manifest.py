"""Evidence is an input, never manufactured by successful software tests."""
from pathlib import Path
import hashlib
import json
from .model import D, number, digest

OPERATIONAL=('V01','V02','V03','V04','V05','V06','V09','V10')
GATES={
    'V01':'Point-in-time linear-USDT contract universe and aliases',
    'V02':'Dated fee schedule and maker/taker treatment',
    'V03':'Historical tick/quantity/minimum/margin rules',
    'V04':'Chronological fills, quantities, queue model and latency',
    'V05':'Funding settlement coverage and valuation',
    'V06':'Immutable data, history/state continuity and mark freshness',
    'V07':'Untouched window and sealed research protocol',
    'V08':'Full RC2 causal fidelity and accounting audit',
    'V09':'Protection, cancellation races and liquidation feasibility',
    'V10':'Frozen per-arm capital and available collateral',
}

class Manifest:
    def __init__(self,data=None,root=None,fixture=False):
        self.data=data or {}; self.fixture=fixture; self.root=Path(root or '.')
        if self.data.get('mode','paper')!='paper': raise ValueError('paper only')
        if self.data.get('category','linear')!='linear': raise ValueError('linear only')
        self.hash=digest(self.data); self.errors={}
        for gate in GATES:
            evidence=self.data.get('evidence',{}).get(gate,[])
            if not evidence: self.errors[gate]='MISSING'; continue
            for e in evidence:
                try:
                    p=(self.root/e['path']).resolve()
                    if not p.is_relative_to(self.root.resolve()): raise ValueError('evidence outside bundle')
                    if hashlib.sha256(p.read_bytes()).hexdigest()!=e['sha256']: raise ValueError('hash mismatch')
                    if not all(e.get(k) for k in ('source','scope','reviewed_at','reviewer')): raise ValueError('missing provenance')
                except (OSError,ValueError,KeyError) as exc: self.errors[gate]=str(exc)
        # Evidence presence is necessary, not a substitute for scope review.
        if self.data.get('scope_review')!='complete':
            for gate in OPERATIONAL: self.errors.setdefault(gate,'SCOPE_REVIEW_REQUIRED')
    def missing(self): return [] if self.fixture else [g for g in OPERATIONAL if g in self.errors]
    def report(self): return {g:dict(description=desc,status='SYNTHETIC_FIXTURE' if self.fixture else self.errors.get(g,'EVIDENCE_SUPPLIED_REVIEW_REQUIRED')) for g,desc in GATES.items()}
    def rules(self,symbol,at):
        rows=[r for r in self.data.get('contracts',{}).get(symbol,[]) if r['start']<=at<r['end']]
        if len(rows)!=1: raise ValueError('missing/overlapping contract rules')
        r=dict(rows[0])
        for k in ('tick','step','minimum_qty','minimum_notional','multiplier','initial_margin','taker','maker','free_collateral'):
            r[k]=number(r[k])
        if any(r[k]<=0 for k in ('tick','step','multiplier','initial_margin')) or any(r[k]<0 for k in ('minimum_qty','minimum_notional','taker','maker','free_collateral')):
            raise ValueError('invalid rule values')
        if not self.fixture and not r.get('liquidation_review'): raise ValueError('liquidation feasibility unavailable')
        return r
