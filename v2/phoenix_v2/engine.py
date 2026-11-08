"""Synchronized chronological replay. Unknown intraminute paths fail closed.

Input batches contain complete 1m bars for every symbol, independently archived
chronological price evidence, marks and funding. No network or order API exists.
"""
from .model import Bar, D, number, Aggregator, digest
from .strategy import Strategy
from .portfolio import Arm

class Engine:
    def __init__(self,manifest):
        self.manifest=manifest; self.events=[]; self.previous=None; self.pending_signals=[]
        self.symbols=tuple(sorted(manifest.data.get('symbols',[])))
        self.strategies={s:Strategy(s,self.log) for s in self.symbols}
        self.aggregators={s:Aggregator() for s in self.symbols}
        self.arms={a:Arm(a,manifest,self.log) for a in ('A','B')}
        self.incomplete=set(manifest.missing()); self.sequence=0
    def log(self,at,event,**fields):
        self.sequence+=1
        self.events.append(dict(sequence=self.sequence,at=at,event=event,**fields))
    def biases(self): return {s:x.bias() for s,x in self.strategies.items()}
    def atrs(self): return {s:x.frames[5].atr.value for s,x in self.strategies.items()}
    def checkpoint(self,at):
        for arm in self.arms.values(): arm.checkpoint(at,self.biases(),self.atrs())
    def batch(self,batch):
        end=batch['end']; start=end-60
        if end%60 or self.previous is not None and end!=self.previous+60: raise ValueError('unsynchronized/gapped batch')
        if set(batch['bars'])!=set(self.symbols): raise ValueError('incomplete symbol batch')
        bars={}
        for symbol,row in batch['bars'].items():
            b=Bar(symbol,1,start,*(number(row[k]) for k in ('o','h','l','c','v')))
            b.validate(end); bars[symbol]=b
        if batch.get('ambiguous') or not batch.get('chronology_evidence'):
            self.incomplete.add('CHRONOLOGY_UNVERIFIED')
            for arm in self.arms.values(): arm.data_valid=False; arm.halt(start,'chronology_unverified')
            # Structures may be observed, but OHLC does not fabricate an event path.
            self.log(start,'batch_quarantined',reason='no_admissible_path_evidence')
        points=batch.get('points',[])
        last_key=None
        for point in points:
            key=(point['at'],point['sequence'])
            if last_key is not None and key<=last_key: raise ValueError('nonchronological points')
            if not start<=point['at']<end: raise ValueError('point outside interval')
            if point['symbol'] not in bars: raise ValueError('unknown symbol')
            price=number(point['price']); b=bars[point['symbol']]
            if not b.l<=price<=b.h: raise ValueError('point inconsistent with bar')
            last_key=key
        # Marks are independently sourced and never replaced by last trades.
        for symbol,mark in batch.get('opening_marks',{}).items():
            if symbol not in bars or mark['at']>start or start-mark['at']>self.manifest.data.get('mark_max_age_seconds',-1): raise ValueError('invalid opening mark')
            for arm in self.arms.values(): arm.marks[symbol]=number(mark['price']); arm.mark_times[symbol]=mark['at']
        for arm in self.arms.values(): arm.reset(start)
        self.checkpoint(start)
        # A has one attempt at exactly the next 1m open, never a later retry.
        for signal in self.pending_signals:
            if signal.at!=start: raise ValueError('late immediate-entry attempt')
            s=signal.symbol; b=bars[s]
            if self.strategies[s].bias()!=signal.side: continue
            self.arms['A'].attempt_a(signal,start,b.o,batch.get('a_fills',{}).get(s))
            o=self.arms['B'].orders.get(s)
            if o and signal.side*(b.o-o.entry)<=0: self.arms['B'].cancel(s,start,'marketable_at_activation')
            self.checkpoint(start)
        self.pending_signals=[]
        for point in points:
            at=point['at']; s=point['symbol']; p=number(point['price'])
            if 'mark' in point:
                for arm in self.arms.values(): arm.marks[s]=number(point['mark']); arm.mark_times[s]=at
            # Current price cancellation and risk checkpoints precede modeled entry.
            self.strategies[s].price(p,at)
            self.checkpoint(at)
            for name,arm in self.arms.items():
                e=point.get('quantities',{}).get(name,{})
                if e and not e.get('evidence'): raise ValueError('quantity without evidence')
                arm.price(s,at,p,self.atrs()[s],number(e['exit']) if 'exit' in e else None,number(e['entry']) if 'entry' in e else None)
                self.checkpoint(at)
        # Funding ownership at exact boundary belongs to pre-settlement position.
        for f in batch.get('funding',[]):
            if f['at']!=end: raise ValueError('funding belongs to exact boundary')
            for arm in self.arms.values(): arm.funding(f['symbol'],end,number(f['rate']),number(f['valuation']),f['evidence'])
        for symbol,m in batch.get('closing_marks',{}).items():
            if m['at']>end or end-m['at']>self.manifest.data.get('mark_max_age_seconds',-1): raise ValueError('stale closing mark')
            for arm in self.arms.values(): arm.marks[symbol]=number(m['price']); arm.mark_times[symbol]=m['at']
        for arm in self.arms.values(): arm.reset(end)
        # Exact-T exits after reset, before newly confirmed veto or entry conversion.
        for x in batch.get('boundary_exits',[]):
            if not x.get('evidence'): raise ValueError('unverified exit')
            arm=self.arms[x['arm']]
            arm.exit_fill(x['symbol'],end,number(x['qty']),number(x['price']),x['reason'])
        frames={s:self.aggregators[s].push(b,end) for s,b in bars.items()}
        for s in self.symbols: self.strategies[s].install(frames[s])
        self.checkpoint(end)
        signals=[]
        if end%300==0:
            for s in self.symbols:
                b5=next((b for b in frames[s] if b.tf==5),None)
                if b5: signals.extend(self.strategies[s].five_close(b5))
        self.pending_signals=sorted(signals,key=lambda s:(-s.score,s.symbol,s.sweep_at,s.id))
        for signal in self.pending_signals:
            for arm in self.arms.values(): arm.reserve(signal,end,signal.close)
            self.checkpoint(end)
        self.previous=end
        self.log(end,'batch_complete',input_hash=digest(batch),manifest=self.manifest.hash,incomplete=sorted(self.incomplete))
    def state(self):
        return dict(previous=self.previous,manifest=self.manifest.hash,incomplete=sorted(self.incomplete),
                    arms={a:dict(cash=x.cash,day=x.day,day_start=x.day_start,locked=x.locked,streak=x.streak,
                                 orders=x.orders,positions=x.positions,completed=x.completed) for a,x in self.arms.items()},
                    journal_hash=digest(self.events))
