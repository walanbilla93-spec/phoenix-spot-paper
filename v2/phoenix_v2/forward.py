"""Experimental forward paper worker; explicitly separate from research gates.

Closed public candles warm structures. Only volume/OHLC-reconciled public
trades advance price-sensitive state. Historical zones/consumption are never
guessed: bootstrap and interrupted intervals clear those objects prospectively.
"""
from dataclasses import asdict
import json
import os
import re
import threading
import time
import traceback
from . import checkpoint
from .bybit_public import PublicAPI, TradeStream, coverage
from .journal import Journal
from .manifest import Manifest
from .model import D, canonical, digest, floor, ceil, number
from .objects import Liquidity
from .portfolio import Arm
from .strategy import Strategy

FRAMES = (5,15,60,240,1440)
FEE_SOURCE = 'https://www.bybit.com/en/fee/futures'
POLICY = {
    'version':'experimental-public-trade-1', 'research_certified':False,
    'scope':'BTCUSDT only; prospective development observations, not a point-in-time universe',
    'capital_per_arm':'1000', 'notional_multiple':'3',
    'fee_rate':'0.00055', 'fee_source':FEE_SOURCE, 'fee_observed_date':'2026-11-09',
    'fee_basis':'Published Standard Crypto regular-user taker, no MNT discount; taker on every paper fill. Account/history unverified.',
    'quantity':'At most 1% of the observed individual public trade size, rounded down. Independent counterfactual allocation per arm, not queue evidence.',
    'activation':'Next one-minute open for A; B resting after MSS boundary. A remainder canceled immediately in the model.',
    'latency':'1 second BE amendment; immediate model cancellations. Venue races unverified.',
    'ordering':'Public feed ordering. Differing prices at the same millisecond are quarantined whenever they could affect an active object, setup, reservation or position; irrelevant ties cannot create fills.',
    'data_loss':'Cancel unfilled entries, clear path-dependent setups/zones/liquidity; retain open liabilities and block new risk until manual evidence reconciliation.',
    'historical_context':'300 own-timeframe closed bars; historical visit/consumption unknown, so no historical POIs or pools are eligible.',
    'funding':'Exact public rate required; preceding completed 1m mark close is an experimental valuation approximation. Missing settlement is unresolved, never zero.',
    'bounds':'No queue/no-fill sensitivity, historical fidelity certification or profitability claim.',
}

class PaperPolicy:
    """Explicit experimental permission, never a fixture or a passed V gate."""
    fixture = False
    def __init__(self, contracts=None):
        self.data = dict(capital='1000',mark_max_age_seconds=120,
                         amendment_latency_seconds=1,contracts=contracts or {})
        self.hash = digest(POLICY)
    def missing(self): return []  # Only counterfactual paper operation; Manifest.report stays OPEN.
    def rules(self, symbol, at):
        rows = [r for r in self.data['contracts'].get(symbol,[]) if r['start']<=at<r['end']]
        if len(rows)!=1: raise ValueError('current experimental contract snapshot unavailable')
        rule={k:(number(v) if k in ('tick','step','minimum_qty','minimum_notional','multiplier',
                     'initial_margin','taker','maker','free_collateral') else v) for k,v in rows[0].items()}
        if any(rule[k]<=0 for k in ('tick','step','multiplier','initial_margin')) or any(
                rule[k]<0 for k in ('minimum_qty','minimum_notional','taker','maker','free_collateral')):
            raise ValueError('invalid experimental contract rule')
        return rule

class Runtime:
    def __init__(self, journal, symbols=('BTCUSDT',)):
        self.journal = journal; self.events = []; self.policy = PaperPolicy()
        saved = journal.load()
        if saved:
            self.restore(saved)
            if tuple(self.state['symbols']) != tuple(symbols): raise ValueError('symbol scope change requires a new journal directory')
            if self.state['policy_hash'] != digest(POLICY): raise ValueError('paper policy change requires explicit migration')
        else:
            self.state = dict(symbols=list(symbols),policy_hash=digest(POLICY),previous=None,
                contracts={},strategies={s:Strategy(s,self.log) for s in symbols},
                arms={a:Arm(a,self.policy,self.log) for a in ('A','B')},pending=[],
                unresolved=[],boots=0,minutes=0,last_clean=None,last_quarantine=None)
        self.bind()

    def bind(self):
        self.policy = PaperPolicy(self.state['contracts'])
        for s in self.state['strategies'].values(): s.log = self.log
        for a in self.state['arms'].values(): a.log = self.log; a.manifest = self.policy
    def restore(self, text): self.state = checkpoint.loads(text); self.bind()
    def log(self, at, event, **fields):
        self.events.append(dict(at=at,event=event,experimental=True,**fields))
    def commit(self, evidence=()):
        try:
            self.journal.commit(evidence,self.events,checkpoint.dumps(self.state)); self.events.clear()
        except BaseException:
            saved = self.journal.load()
            if saved: self.restore(saved)
            self.events.clear(); raise
    def checkpoint_arms(self, at):
        biases = {s:x.bias() for s,x in self.state['strategies'].items()}
        atrs = {s:x.frames[5].atr.value for s,x in self.state['strategies'].items()}
        for arm in self.state['arms'].values(): arm.checkpoint(at,biases,atrs)

    def ambiguous_ties(self, symbol, rows):
        groups={}
        for row in rows:groups.setdefault(row['T'],set()).add(number(row['p']))
        groups=[prices for prices in groups.values() if len(prices)>1]
        if not groups:return False
        # With exposure or a pending next-open attempt, unresolved order/volume
        # allocation matters even if two prices lie on the same side of a level.
        if any(s.symbol==symbol for s in self.state['pending']):return True
        if any(symbol in a.orders or symbol in a.positions for a in self.state['arms'].values()):return True
        strategy=self.state['strategies'][symbol]
        if strategy.setups or strategy.sweeps:return True
        for prices in groups:
            lo,hi=min(prices),max(prices)
            for z in strategy.zones:
                if z.valid and (lo<z.high if z.side==1 else hi>z.low):return True
            for p in strategy.liquidity.pools:
                if not p.consumed and (hi>p.outer if p.kind==1 else lo<p.outer):return True
        # No price-sensitive threshold can be touched in any permutation.
        # Last-price storage is descriptive; it is never an entry predicate.
        return False

    def interrupted(self, at, reason):
        self.state['last_quarantine'] = dict(at=at,reason=reason)
        self.state['pending'] = []
        for symbol,s in self.state['strategies'].items():
            s.invalidate(at,reason); s.zones=[]; s.liquidity=Liquidity(); s.quarantined=False
        for arm in self.state['arms'].values():
            arm.halt(at,reason)
            if arm.positions:
                arm.data_valid=False
                for symbol,p in arm.positions.items():
                    key=arm.name+':'+p.id+':'+str(at)
                    if key not in self.state['unresolved']: self.state['unresolved'].append(key)
                    self.log(at,'liability_quarantined',arm=arm.name,id=p.id,symbol=symbol,
                             qty=p.qty,reason=reason,disposition='RETAINED_NO_FABRICATED_EXIT')
        self.log(at,'interval_quarantined',reason=reason)

    def bootstrap(self, api, anchor):
        evidence=[]
        for symbol in self.state['symbols']:
            s=self.state['strategies'][symbol]
            for tf in FRAMES:
                bars=api.candles(symbol,tf,anchor)
                if len(bars)<250: raise ValueError('insufficient own-timeframe history '+symbol+':'+str(tf))
                for b in bars:
                    s.install([b]); evidence.append((f'candle:{symbol}:{tf}:{b.start}','candle',b.end,asdict(b)))
                if bars[-1].end != anchor//(tf*60)*(tf*60): raise ValueError('bootstrap history stale')
            s.zones=[]; s.liquidity=Liquidity(); s.setups={}; s.sweeps={}
        self.state['previous']=anchor
        self.log(anchor,'bootstrap_complete',policy=POLICY,
                 history='Structures warmed; all historical POIs/pools ineligible due unknown visits/consumption')
        self.commit(evidence)

    def update_contracts(self, api, at):
        evidence=[]
        for symbol in self.state['symbols']:
            current=self.state['contracts'].get(symbol,[])
            if current and current[0]['end']-at>3600: continue
            r=api.instrument(symbol); p=r['priceFilter']; q=r['lotSizeFilter']
            start=int(time.time()); end=start+86400
            rule=dict(start=start,end=end,tick=p['tickSize'],step=q['qtyStep'],
                minimum_qty=q['minOrderQty'],minimum_notional=q['minNotionalValue'],
                multiplier='1',initial_margin='0.33333333333333333334',taker='0.00055',
                maker='0.00055',free_collateral='1000',funding_minutes=int(r['fundingInterval']),
                snapshot=digest(r),experimental=True)
            if any(number(rule[k])<=0 for k in ('tick','step','minimum_qty')) or rule['funding_minutes']<=0:
                raise ValueError('invalid contract metadata')
            self.state['contracts'][symbol]=[rule]
            evidence.append((f'instrument:{symbol}:{start}','instrument',start,r))
            self.log(at,'contract_snapshot',symbol=symbol,rule=rule,source='/v5/market/instruments-info')
        self.policy.data['contracts']=self.state['contracts']
        if evidence: self.commit(evidence)

    def minute(self, end, frames, marks, streams, settlements):
        if end!=self.state['previous']+60: raise ValueError('noncontiguous worker minute')
        start=end-60; evidence=[]; reasons=[]; ticks=[]; quality={}
        for symbol in self.state['symbols']:
            bars=frames[symbol]; b=next(x for x in bars if x.tf==1)
            if b.start!=start: raise ValueError('incorrect minute')
            for x in bars: evidence.append((f'candle:{symbol}:{x.tf}:{x.start}','candle',x.end,asdict(x)))
            mark=marks[symbol]
            evidence.append((f'mark:{symbol}:{start}','mark',end,asdict(mark)))
            rows,bad=streams[symbol]; reason=coverage(b,rows,bad)
            evidence.append((f'trades:{symbol}:{start}','public_trades',end,rows))
            if self.ambiguous_ties(symbol,rows):reason=reason or 'same_millisecond_price_ambiguity'
            prices=[number(r['p']) for r in rows]
            observed=dict(o=prices[0],h=max(prices),l=min(prices),c=prices[-1],
                          v=sum((number(r['v']) for r in rows),D(0))) if rows else None
            quality[symbol]=dict(reason=reason,expected=asdict(b),observed=observed,
                trade_count=len(rows),trade_evidence_hash=digest(rows),
                first_trade_at=rows[0]['T'] if rows else None,last_trade_at=rows[-1]['T'] if rows else None)
            if reason: reasons.append(symbol+':'+reason)
            for i,r in enumerate(rows): ticks.append((r['T'],symbol,i,r))
            for arm in self.state['arms'].values():
                arm.marks[symbol]=mark.o; arm.mark_times[symbol]=start
        if reasons: self.interrupted(start,';'.join(reasons))
        clean=not reasons and not self.state['unresolved']
        for arm in self.state['arms'].values():
            if not self.state['unresolved']: arm.data_valid=clean
            arm.reset(start)
        self.checkpoint_arms(start)
        if clean:
            for signal in self.state['pending']:
                s=signal.symbol; rows=streams[s][0]; b=next(x for x in frames[s] if x.tf==1)
                first=rows[0]; a=self.state['arms']['A']
                if signal.at!=start: raise ValueError('late A activation')
                rule=self.policy.rules(s,start)
                qty=floor(number(first['v'])*D('.01'),rule['step'])
                # Missing quantity does not grant an entry. Reference is the reconciled next open.
                a.attempt_a(signal,start,b.o,dict(qty=str(qty),evidence='EXPERIMENTAL_1_PERCENT_PUBLIC_TRADE:'+first['i']) if qty>0 else None)
                o=self.state['arms']['B'].orders.get(s)
                if o and signal.side*(b.o-o.entry)<=0: self.state['arms']['B'].cancel(s,start,'marketable_at_activation')
                self.checkpoint_arms(start)
            self.state['pending']=[]
            for ms,symbol,_,r in sorted(ticks,key=lambda x:x[:3]):
                at=ms/1000; p=number(r['p']); s=self.state['strategies'][symbol]
                s.price(p,at); self.checkpoint_arms(at)
                atr=s.frames[5].atr.value
                rule=self.policy.rules(symbol,at); qty=floor(number(r['v'])*D('.01'),rule['step'])
                for arm in self.state['arms'].values():
                    entry=None; exit_qty=None; o=arm.orders.get(symbol); pos=arm.positions.get(symbol)
                    if qty>0:
                        if o and (r['S']=='Sell' if o.signal.side==1 else r['S']=='Buy'): entry=qty
                        if pos: exit_qty=qty
                    # Never allow a one-tick B entry and a second full use of the
                    # same market volume for an exit in this event.
                    arm.price(symbol,at,p,atr,exit_qty,entry)
                    self.checkpoint_arms(at)
            self.state['last_clean']=end
        for symbol in self.state['symbols']:
            rule=self.state['contracts'].get(symbol,[{}])[0]
            interval=rule.get('funding_minutes',0)*60
            if interval and end%interval==0:
                row=settlements.get(symbol)
                if not row:
                    for arm in self.state['arms'].values():
                        if symbol in arm.positions:
                            arm.data_valid=False; arm.halt(end,'funding_unavailable')
                            key='funding:'+arm.name+':'+symbol+':'+str(end)
                            self.state['unresolved'].append(key)
                            self.log(end,'funding_missing',arm=arm.name,symbol=symbol)
                else:
                    evidence.append((f'funding:{symbol}:{end}','funding',end,row))
                    for arm in self.state['arms'].values():
                        arm.funding(symbol,end,number(row['fundingRate']),marks[symbol].c,digest(row))
            for arm in self.state['arms'].values():
                arm.marks[symbol]=marks[symbol].c; arm.mark_times[symbol]=end; arm.reset(end)
        for symbol in self.state['symbols']:
            self.state['strategies'][symbol].install(frames[symbol])
        self.checkpoint_arms(end)
        signals=[]
        if clean and end%300==0:
            for symbol,s in self.state['strategies'].items():
                b=next((b for b in frames[symbol] if b.tf==5),None)
                if b: signals.extend(s.five_close(b))
            self.state['pending']=sorted(signals,key=lambda s:(-s.score,s.symbol,s.sweep_at,s.id))
            for signal in self.state['pending']:
                self.log(end,'signal',id=signal.id,signal=asdict(signal),policy=self.state['policy_hash'])
                for arm in self.state['arms'].values():
                    arm.reserve(signal,end,signal.close); self.checkpoint_arms(end)
        if end%300==0:
            self.log(end,'signal_evaluation',disposition='QUALIFIED_SIGNAL' if signals else
                     ('NO_QUALIFIED_SIGNAL' if clean else 'QUARANTINED_DATA'),
                     signals=len(signals),reasons=reasons,unresolved=len(self.state['unresolved']))
        self.state['previous']=end; self.state['minutes']+=1
        self.state['quality']=quality
        # Journal is authoritative; these lists are only recent in-memory summaries.
        for arm in self.state['arms'].values():
            arm.completed=arm.completed[-100:]; arm.opportunities=arm.opportunities[-100:]
        objects=0
        for s in self.state['strategies'].values():
            objects+=len(s.zones)+len(s.liquidity.pools)+sum(len(f.bars)+len(f.raw)+len(f.sequence_history) for f in s.frames.values())
        if objects>25000: raise RuntimeError('strategy object resource ceiling; explicit archival/reseed required')
        self.log(end,'minute_processed',clean=clean,trades_observed=len(ticks),reasons=reasons,quality=quality)
        self.commit(evidence)

class Worker:
    def __init__(self, directory, symbols=('BTCUSDT',), persistent=False, api=None):
        if not 1<=len(symbols)<=3 or len(set(symbols))!=len(symbols) or any(not re.fullmatch('[A-Z0-9]{3,20}USDT',s) for s in symbols):
            raise ValueError('one to three explicit USDT symbols required')
        self.stop=threading.Event(); self.api=api or PublicAPI(); self.directory=directory
        self.symbols=symbols; self.persistent=persistent; self.started=time.time(); self.heartbeat=0
        self.last_success=None; self.error=None; self.failures=0; self.fatal=False; self.thread=None
        self.journal=None; self.runtime=None; self.stream=TradeStream(symbols,self.stop); self.view={}

    def start(self):
        self.thread=threading.Thread(target=self.run,name='rc2-paper-worker',daemon=True); self.thread.start()

    def publish(self):
        state=self.runtime.state if self.runtime else {}
        self.view=dict(previous=state.get('previous'),minutes=state.get('minutes',0),
            boots=state.get('boots',0),last_clean=state.get('last_clean'),
            last_quarantine=state.get('last_quarantine'),unresolved=state.get('unresolved',[])[:30],
            quality=state.get('quality',{}),
            arms={name:dict(cash=str(a.cash),positions=[asdict(p) for p in a.positions.values()],
                orders=[asdict(o) for o in a.orders.values()],locked=a.locked,streak=a.streak,
                data_valid=a.data_valid,virtual_capital='1000') for name,a in state.get('arms',{}).items()},
            warmup={s:{str(tf):f.atr.count for tf,f in x.frames.items()} for s,x in state.get('strategies',{}).items()},
            counts=dict(self.journal.counts) if self.journal else {},
            evidence_counts=dict(self.journal.evidence_counts) if self.journal else {},
            journal_head=self.journal.head if self.journal else None,
            journal_sequence=self.journal.sequence if self.journal else 0,
            storage_bytes=self.journal.capacity() if self.journal else 0)

    def status(self):
        now=time.time(); prev=self.view.get('previous')
        live=bool(self.thread and self.thread.is_alive() and self.heartbeat and now-self.heartbeat<120 and not self.fatal)
        operational=bool(live and self.last_success and now-self.last_success<120 and prev and now-prev<180)
        ready=operational and self.persistent
        execution=bool(ready and self.stream.connected and self.view.get('last_clean')==prev and not self.view.get('unresolved'))
        return dict(self.view,worker_alive=live,forward_paper_operational=operational,
            ready=ready,execution_ready=execution,persistence_configured=self.persistent,
            status='EXPERIMENTAL_PAPER_RUNNING' if ready else ('STARTING' if not self.error else 'DEGRADED'),
            error=self.error,consecutive_failures=self.failures,heartbeat_at=self.heartbeat,
            last_success_at=self.last_success,worker_uptime_seconds=int(now-self.started),
            public_api_requests=self.api.requests,stream_connected=self.stream.connected,
            stream_trades_received=self.stream.total,stream_reconnects=self.stream.reconnects,
            stream_error=self.stream.last_error,symbols=list(self.symbols),policy=POLICY,
            research_certified=False,profitability_certified=False)

    def cycle(self):
        r=self.runtime; now=int(time.time()); r.update_contracts(self.api,now)
        if r.state['previous'] is None: r.bootstrap(self.api,now//60*60)
        target=min((now-7)//60*60,r.state['previous']+3600)
        if target<=r.state['previous']: return
        start=r.state['previous']; by_end={}; mark_map={}
        # One bounded page per symbol/timeframe and at most 60 minutes per cycle.
        for symbol in self.symbols:
            for tf in (1,)+FRAMES:
                first=start if tf==1 else r.state['strategies'][symbol].frames[tf].bars[-1].end
                rows=self.api.candles(symbol,tf,target,start=first,limit=1000)
                expected=set(range(first+tf*60,target//(tf*60)*(tf*60)+1,tf*60))
                if {b.end for b in rows}!=expected: raise ValueError('missing candle/backfill '+symbol+':'+str(tf))
                for b in rows: by_end.setdefault(b.end,{}).setdefault(symbol,[]).append(b)
            marks=self.api.candles(symbol,1,target,start=start,limit=1000,mark=True)
            if {b.end for b in marks}!=set(range(start+60,target+1,60)): raise ValueError('missing mark backfill')
            for b in marks: mark_map.setdefault(b.end,{})[symbol]=b
        for end in range(start+60,target+1,60):
            settlements={}
            for symbol in self.symbols:
                interval=r.state['contracts'][symbol][0]['funding_minutes']*60
                if end%interval==0:
                    try: settlements[symbol]=self.api.funding(symbol,end)
                    except (ValueError,OSError): pass  # Runtime flags liabilities, never substitutes zero.
            streams={s:self.stream.take(s,end-60) for s in self.symbols}
            r.minute(end,by_end[end],mark_map[end],streams,settlements)
            self.heartbeat=time.time(); self.last_success=time.time(); self.publish()

    def run(self):
        try:
            self.journal=Journal(self.directory); self.runtime=Runtime(self.journal,self.symbols)
            self.runtime.state['boots']+=1
            self.runtime.log(time.time(),'worker_started',boots=self.runtime.state['boots'],
                             previous=self.runtime.state['previous'],policy_hash=digest(POLICY))
            self.runtime.commit(); self.publish(); self.stream.start()
            while not self.stop.is_set():
                self.heartbeat=time.time()
                try:
                    self.cycle(); self.failures=0; self.error=None; self.publish()
                except Exception as exc:
                    self.failures+=1; self.error=type(exc).__name__+': '+str(exc)[:240]
                    print(canonical(dict(event='worker_error',error=self.error,failures=self.failures)),flush=True)
                    # Restore atomic state after any mutation interrupted before commit.
                    saved=self.journal.load()
                    if saved: self.runtime.restore(saved)
                    self.runtime.events.clear()
                    if any(t in self.error for t in ('corruption','resource ceiling','storage limit','immutable evidence conflict')):
                        self.fatal=True; break
                self.stop.wait(15 if not self.failures else min(60,2**min(self.failures,6)))
        except Exception as exc:
            self.error=type(exc).__name__+': '+str(exc)[:240]; self.fatal=True
            print(canonical(dict(event='worker_fatal',error=self.error)),flush=True)
        finally:
            self.stop.set()
            if self.stream.thread: self.stream.thread.join(timeout=13)
            if self.journal: self.journal.close()
