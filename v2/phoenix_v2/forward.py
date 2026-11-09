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
from .universe import SYMBOLS, META, assess, LIQUIDITY_POLICY
from .metrics import Metrics

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
LEGACY_POLICY_HASH=digest(POLICY)
POLICY=dict(POLICY,version='experimental-public-trade-2',
    scope='Explicit configured Bybit USDT perpetuals; twelve requested assets, native SHIB1000/1000PEPE denominations. Prospective experimental scope, not a certified point-in-time universe.',
    feed_isolation='Quarantine affected symbols. Any unresolved held liability blocks new shared-arm risk. No cross-arm netting.')

class PaperPolicy:
    """Explicit experimental permission, never a fixture or a passed V gate."""
    fixture = False
    def __init__(self, contracts=None, eligibility=None):
        self.data = dict(capital='1000',mark_max_age_seconds=120,
                         amendment_latency_seconds=1,contracts=contracts or {})
        self.hash = digest(POLICY)
        self.eligibility=eligibility
    def missing(self): return []  # Only counterfactual paper operation; Manifest.report stays OPEN.
    def rules(self, symbol, at):
        if self.eligibility is not None:
            e=self.eligibility.get(symbol,{})
            if not e.get('liquidity_eligible') or not e.get('available') or not e.get('observed_at',at+1)<=at<e.get('expires_at',0):
                raise ValueError('symbol eligibility unavailable/expired')
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
        self.journal = journal; self.events = []; self.policy = PaperPolicy();self.dirty=False
        saved = journal.load()
        if saved:
            self.restore(saved)
            previous=tuple(self.state['symbols'])
            if previous!=tuple(symbols) or self.state['policy_hash']!=digest(POLICY):
                if previous==('BTCUSDT',) and tuple(symbols)==SYMBOLS and self.state['policy_hash']==LEGACY_POLICY_HASH:
                    for symbol in symbols:
                        if symbol not in self.state['strategies']:self.state['strategies'][symbol]=Strategy(symbol,self.log)
                    self.state['symbols']=list(symbols);self.state['policy_hash']=digest(POLICY)
                    self.log(time.time(),'universe_expanded',from_symbols=list(previous),to_symbols=list(symbols),
                        old_policy_hash=LEGACY_POLICY_HASH,new_policy_hash=digest(POLICY),
                        disposition='AUTHORIZED_ADDITIVE_MIGRATION_ACCOUNTS_AND_HISTORY_RETAINED',policy=POLICY)
                else:raise ValueError('unrecognized scope/policy migration; existing journal retained')
        else:
            self.state = dict(symbols=list(symbols),policy_hash=digest(POLICY),previous=None,
                contracts={},strategies={s:Strategy(s,self.log) for s in symbols},
                arms={a:Arm(a,self.policy,self.log) for a in ('A','B')},pending=[],
                unresolved=[],boots=0,minutes=0,last_clean=None,last_quarantine=None)
        self.bind()

    def bind(self):
        self.policy = PaperPolicy(self.state['contracts'],self.state.setdefault('eligibility',{}) if self.state.get('enforce_liquidity') else None)
        for s in self.state['strategies'].values(): s.log = self.log
        for a in self.state['arms'].values(): a.log = self.log; a.manifest = self.policy
    def restore(self, text):
        # Release the old graph before decoding its replacement on the 256 MB
        # tier. Building both complete graphs at once can exhaust the container.
        import gc
        self.state=None;gc.collect()
        self.state=checkpoint.loads(text);self.dirty=False;self.bind()
    def log(self, at, event, **fields):
        self.dirty=True
        if not fields.get('symbol'):
            if isinstance(fields.get('signal'),dict):fields['symbol']=fields['signal'].get('symbol')
            else:
                for part in str(fields.get('id','')).split(':')[:2]:
                    if part in META:fields['symbol']=part;break
        if event=='funding_missing' and fields.get('arm') and fields.get('symbol'):
            p=self.state['arms'][fields['arm']].positions.get(fields['symbol'])
            if p:fields.setdefault('id',p.id)
        if fields.get('arm') and fields.get('id') and hasattr(self,'state'):
            a=self.state['arms'].get(fields['arm'])
            p=next((p for p in a.positions.values() if p.id==fields['id']),None) if a else None
            o=next((o for o in a.orders.values() if o.id==fields['id']),None) if a else None
            item=p or o
            if item:
                fields.setdefault('symbol',item.signal.symbol);fields.setdefault('side',item.signal.side)
            if p:
                fields['position_snapshot']=dict(symbol=p.signal.symbol,side=p.signal.side,entry=p.entry,
                    entry_at=p.opened,original_qty=p.original_qty,remaining_qty=p.qty,sl=p.stop,
                    initial_sl=p.initial_stop,tp=p.target,fees=p.fees,funding=p.funding,
                    realized_gross=p.realized,multiplier=p.rules['multiplier'])
        self.events.append(dict(at=at,event=event,experimental=True,**fields))
    def commit(self, evidence=()):
        try:
            self.journal.commit(evidence,self.events,checkpoint.dumps(self.state)); self.events.clear();self.dirty=False
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

    def interrupted(self, at, reason, symbols=None):
        affected=set(symbols or self.state['symbols'])
        self.state['last_quarantine'] = dict(at=at,reason=reason)
        self.state['pending'] = [s for s in self.state['pending'] if s.symbol not in affected]
        for symbol,s in self.state['strategies'].items():
            if symbol not in affected:continue
            s.invalidate(at,reason); s.zones=[]; s.liquidity=Liquidity(); s.quarantined=False
        for arm in self.state['arms'].values():
            for symbol in affected:arm.cancel(symbol,at,reason)
            if any(s in affected for s in arm.positions):
                arm.data_valid=False
                for symbol,p in arm.positions.items():
                    if symbol not in affected:continue
                    key=arm.name+':'+p.id+':'+str(at)
                    if key not in self.state['unresolved']: self.state['unresolved'].append(key)
                    self.log(at,'liability_quarantined',arm=arm.name,id=p.id,symbol=symbol,
                             qty=p.qty,reason=reason,disposition='RETAINED_NO_FABRICATED_EXIT')
        self.log(at,'interval_quarantined',reason=reason,symbols=sorted(affected),symbol=next(iter(affected)) if len(affected)==1 else None)

    def bootstrap(self, api, anchor):
        if self.state['previous'] is None:self.state['previous']=anchor
        for symbol in self.state['symbols']:
            s=self.state['strategies'][symbol]
            if all(s.frames[tf].bars for tf in FRAMES):continue
            evidence=[]
            for tf in FRAMES:
                bars=api.candles(symbol,tf,anchor)
                if len(bars)<250: raise ValueError('insufficient own-timeframe history '+symbol+':'+str(tf))
                self.dirty=True
                for b in bars:
                    s.install([b]); evidence.append((f'candle:{symbol}:{tf}:{b.start}','candle',b.end,asdict(b)))
                if bars[-1].end != anchor//(tf*60)*(tf*60): raise ValueError('bootstrap history stale')
            s.zones=[]; s.liquidity=Liquidity(); s.setups={}; s.sweeps={}
            self.log(anchor,'symbol_warmup_complete',symbol=symbol,bars_per_frame=300)
            self.commit(evidence)
        if not self.state.get('bootstrap_recorded_for')==self.state['symbols']:
            self.log(anchor,'bootstrap_complete' if self.journal.counts.get('bootstrap_complete',0)==0 else 'universe_warmup_complete',policy=POLICY,
                 history='Structures warmed; all historical POIs/pools ineligible due unknown visits/consumption')
            self.state['bootstrap_recorded_for']=list(self.state['symbols']);self.commit()

    def update_eligibility(self, api, at):
        state=self.state.setdefault('eligibility',{})
        if state and all(at-e.get('observed_at',0)<LIQUIDITY_POLICY['refresh_seconds'] for e in state.values()) and len(state)==len(self.state['symbols']):return
        tickers=api.tickers()
        for symbol in self.state['symbols']:
            if symbol in state and at-state[symbol]['observed_at']<LIQUIDITY_POLICY['refresh_seconds']:continue
            instrument=api.instrument(symbol);book=api.orderbook(symbol)
            when=time.time();row=assess(instrument,tickers[symbol],book,when);state[symbol]=row
            self.log(when,'eligibility_checked',symbol=symbol,eligibility=row)
            self.commit([(f'eligibility:{symbol}:{int(when)}','eligibility',int(when),dict(instrument=instrument,ticker=tickers[symbol],orderbook=book,assessment=row))])

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
        self.dirty=True
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
        bad_symbols={s for s,q in quality.items() if q['reason']}
        armed={s for s in self.state['symbols'] if any(s in a.positions or s in a.orders for a in self.state['arms'].values())}
        times={}
        for ms,symbol,_,r in ticks:
            if symbol in armed:times.setdefault(ms,set()).add(symbol)
        for tied in times.values():
            if len(tied)>1:
                for symbol in tied:
                    quality[symbol]['reason']=quality[symbol]['reason'] or 'cross_symbol_time_ambiguity';bad_symbols.add(symbol)
        for symbol in sorted(bad_symbols):self.interrupted(start,symbol+':'+quality[symbol]['reason'],[symbol])
        clean_symbols=set(self.state['symbols'])-bad_symbols
        clean=bool(clean_symbols) and not self.state['unresolved']
        for arm in self.state['arms'].values():
            if not self.state['unresolved']: arm.data_valid=True
            arm.reset(start)
        self.checkpoint_arms(start)
        if clean:
            for signal in self.state['pending']:
                s=signal.symbol; rows=streams[s][0]; b=next(x for x in frames[s] if x.tf==1)
                if s not in clean_symbols:continue
                first=rows[0]; a=self.state['arms']['A']
                if not any(s in arm.orders for arm in self.state['arms'].values()):continue
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
                if symbol not in clean_symbols:continue
                at=ms/1000; p=number(r['p']); s=self.state['strategies'][symbol]
                s.price(p,at)
                if not any(symbol in a.orders or symbol in a.positions for a in self.state['arms'].values()):continue
                self.checkpoint_arms(at)
                active=next((a.positions.get(symbol) or a.orders.get(symbol) for a in self.state['arms'].values()
                    if symbol in a.positions or symbol in a.orders),None)
                if active is None:continue
                atr=s.frames[5].atr.value
                # Liquidity eligibility gates new risk, not protection of an
                # existing liability. Its frozen native rules remain authoritative.
                rule=active.rules; qty=floor(number(r['v'])*D('.01'),rule['step'])
                for arm in self.state['arms'].values():
                    entry=None; exit_qty=None; o=arm.orders.get(symbol); pos=arm.positions.get(symbol)
                    if qty>0:
                        if o and (r['S']=='Sell' if o.signal.side==1 else r['S']=='Buy'): entry=qty
                        if pos: exit_qty=qty
                    # Never allow a one-tick B entry and a second full use of the
                    # same market volume for an exit in this event.
                    arm.price(symbol,at,p,atr,exit_qty,entry)
                    self.checkpoint_arms(at)
            if len(clean_symbols)==len(self.state['symbols']):self.state['last_clean']=end
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
                if symbol not in clean_symbols:continue
                b=next((b for b in frames[symbol] if b.tf==5),None)
                if b: signals.extend(s.five_close(b))
            self.state['pending']=sorted(signals,key=lambda s:(-s.score,s.symbol,s.sweep_at,s.id))
            for signal in self.state['pending']:
                self.log(end,'signal',id=signal.id,signal=asdict(signal),policy=self.state['policy_hash'])
                for arm in self.state['arms'].values():
                    arm.reserve(signal,end,signal.close); self.checkpoint_arms(end)
        if end%300==0:
            for symbol in self.state['symbols']:
                found=[x for x in signals if x.symbol==symbol]
                self.log(end,'signal_evaluation',symbol=symbol,disposition='QUALIFIED_SIGNAL' if found else
                    ('NO_QUALIFIED_SIGNAL' if clean and symbol in clean_symbols else 'QUARANTINED_DATA'),
                    signals=len(found),reasons=[quality[symbol]['reason']] if quality[symbol]['reason'] else [],unresolved=len(self.state['unresolved']))
        self.state['previous']=end; self.state['minutes']+=1
        self.state['quality']=quality
        self.state['clean_symbols']=sorted(clean_symbols)
        stats=self.state.setdefault('feed_counts',{})
        for symbol in self.state['symbols']:
            row=stats.setdefault(symbol,dict(clean=0,quarantined=0,last_clean=None))
            row['clean' if symbol in clean_symbols else 'quarantined']+=1
            if symbol in clean_symbols:row['last_clean']=end
        # Journal is authoritative; these lists are only recent in-memory summaries.
        for arm in self.state['arms'].values():
            arm.completed=arm.completed[-100:]; arm.opportunities=arm.opportunities[-100:]
        objects=0
        for s in self.state['strategies'].values():
            objects+=len(s.zones)+len(s.liquidity.pools)+sum(len(f.bars)+len(f.raw)+len(f.sequence_history) for f in s.frames.values())
        if objects>60000: raise RuntimeError('strategy object resource ceiling; explicit archival/reseed required')
        self.log(end,'minute_processed',clean=clean,trades_observed=len(ticks),reasons=reasons,quality=quality)
        self.commit(evidence)

class Worker:
    def __init__(self, directory, symbols=SYMBOLS, persistent=False, api=None):
        if not 1<=len(symbols)<=12 or len(set(symbols))!=len(symbols) or any(s not in META for s in symbols):
            raise ValueError('only the twelve explicitly requested native USDT contracts are allowed')
        self.stop=threading.Event(); self.api=api or PublicAPI(); self.directory=directory
        self.symbols=symbols; self.persistent=persistent; self.started=time.time(); self.heartbeat=0
        self.last_success=None; self.error=None; self.failures=0; self.fatal=False; self.thread=None
        self.journal=None; self.runtime=None; self.stream=TradeStream(symbols,self.stop); self.view={}
        self.metrics=Metrics();self.resources={}
        self.steady_started=False

    def start(self):
        self.thread=threading.Thread(target=self.run,name='rc2-paper-worker',daemon=True); self.thread.start()

    def publish(self):
        state=self.runtime.state if self.runtime else {}
        storage=self.journal.capacity() if self.journal else 0
        if not self.steady_started and state.get('bootstrap_recorded_for')==state.get('symbols'):
            self.metrics.samples=[];self.steady_started=True
        self.resources=self.metrics.sample(storage,self.journal.allocated_bytes() if self.journal else None)
        arms={}
        for name,a in state.get('arms',{}).items():
            try:equity=a.equity(state['previous'] or int(time.time())) if not state['unresolved'] else None
            except ValueError:equity=None
            arms[name]=dict(cash=str(a.cash),equity=str(equity) if equity is not None else None,
                unrealized_pnl=str(equity-a.cash) if equity is not None else None,
                total_pnl=str(equity-D(1000)) if equity is not None else None,cash_pnl=str(a.cash-D(1000)),
                positions=[asdict(p) for p in a.positions.values()],orders=[asdict(o) for o in a.orders.values()],
                marks={s:str(p) for s,p in a.marks.items()},
                locked=a.locked,streak=a.streak,data_valid=a.data_valid,virtual_capital='1000')
        self.view=dict(previous=state.get('previous'),minutes=state.get('minutes',0),
            boots=state.get('boots',0),last_clean=state.get('last_clean'),
            last_quarantine=state.get('last_quarantine'),unresolved=state.get('unresolved',[])[:30],
            quality=state.get('quality',{}),
            arms=arms,
            eligibility=state.get('eligibility',{}),clean_symbols=state.get('clean_symbols',[]),
            feed_counts=state.get('feed_counts',{}),
            warmup={s:{str(tf):f.atr.count for tf,f in x.frames.items()} for s,x in state.get('strategies',{}).items()},
            counts=dict(self.journal.counts) if self.journal else {},
            evidence_counts=dict(self.journal.evidence_counts) if self.journal else {},
            journal_head=self.journal.head if self.journal else None,
            journal_sequence=self.journal.sequence if self.journal else 0,
            storage_bytes=storage,resources=self.resources,
            journal_budget_bytes=self.journal.max_bytes if self.journal else None,
            financials=self.journal.financials() if self.journal else {})

    def status(self):
        now=time.time(); prev=self.view.get('previous')
        initializing=bool(self.runtime and any(not f.bars for s in self.runtime.state['strategies'].values() for f in s.frames.values()))
        live=bool(self.thread and self.thread.is_alive() and self.heartbeat and
            (now-self.heartbeat<180 or initializing and now-self.started<300) and not self.fatal)
        operational=bool(live and self.last_success and now-self.last_success<180 and prev and now-prev<180)
        ready=operational and self.persistent
        clean=self.view.get('clean_symbols',[self.symbols[0]] if self.view.get('last_clean')==prev else [])
        eligible=[s for s in clean if self.view.get('eligibility',{}).get(s,{}).get('liquidity_eligible')
            and now<self.view['eligibility'][s].get('expires_at',0)]
        # Legacy unit/local instances without production eligibility enforcement.
        if not self.view.get('eligibility') and len(self.symbols)==1:eligible=clean
        execution=bool(ready and self.stream.connected and eligible and not self.view.get('unresolved'))
        coins=[]
        for symbol in self.symbols:
            e=self.view.get('eligibility',{}).get(symbol,{})
            q=self.view.get('quality',{}).get(symbol,{})
            warm=self.view.get('warmup',{}).get(symbol,{})
            coins.append(dict(META[symbol],eligibility=e,
                eligibility_current=bool(e.get('available') and e.get('liquidity_eligible') and now<e.get('expires_at',0)),
                feed_status='WARMING' if not warm or min(warm.values())<250 else
                    ('CURRENT' if ready and symbol in clean else 'QUARANTINED' if ready else 'STALE'),
                reason=q.get('reason'),execution_ready=execution and symbol in eligible,
                trade_events_received=self.stream.received_by_symbol.get(symbol,0),
                feed_counts=self.view.get('feed_counts',{}).get(symbol,{})))
        return dict(self.view,worker_alive=live,forward_paper_operational=operational,
            ready=ready,execution_ready=execution,persistence_configured=self.persistent,
            status='EXPERIMENTAL_PAPER_RUNNING' if ready else ('STARTING' if not self.error else 'DEGRADED'),
            initializing=initializing,
            error=self.error,consecutive_failures=self.failures,heartbeat_at=self.heartbeat,
            last_success_at=self.last_success,worker_uptime_seconds=int(now-self.started),
            public_api_requests=self.api.requests,stream_connected=self.stream.connected,
            stream_trades_received=self.stream.total,stream_reconnects=self.stream.reconnects,
            stream_error=self.stream.last_error,symbols=list(self.symbols),policy=POLICY,
            monitored_symbol_count=len(self.symbols),execution_ready_count=len(eligible) if execution else 0,
            all_feeds_ready=ready and len(clean)==len(self.symbols),coins=coins,
            api_metrics=dict(requests=self.api.requests,by_path=dict(self.api.by_path),errors=self.api.errors,
                rate_limits=self.api.rate_limits,total_request_seconds=self.api.elapsed),
            stream_metrics=dict(buffered=self.stream.buffered,peak_buffered=self.stream.peak_buffered,
                buffer_limit=60000,overflow_records=self.stream.overflow),
            research_certified=False,profitability_certified=False)

    def cycle(self):
        began=time.monotonic();r=self.runtime; now=int(time.time()); r.update_contracts(self.api,now)
        r.update_eligibility(self.api,now)
        if r.state['previous'] is None or any(not x.frames[1440].bars for x in r.state['strategies'].values()):
            r.bootstrap(self.api,r.state['previous'] or now//60*60)
        target=min((now-7)//60*60,r.state['previous']+3600)
        if target<=r.state['previous']: return
        start=r.state['previous']; by_end={}; mark_map={}
        # One bounded page per symbol/timeframe and at most 60 minutes per cycle.
        for symbol in self.symbols:
            for tf in (1,)+FRAMES:
                first=start if tf==1 else r.state['strategies'][symbol].frames[tf].bars[-1].end
                if first+tf*60>target:continue
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
        self.metrics.cycles.append(time.monotonic()-began);self.metrics.cycles=self.metrics.cycles[-60:]

    def run(self):
        try:
            self.journal=Journal(self.directory,max_bytes=4_000_000_000); self.runtime=Runtime(self.journal,self.symbols)
            self.runtime.state['enforce_liquidity']=True;self.runtime.bind()
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
                    # Public fetch failures do not mutate strategy state. Avoid
                    # unnecessary full-graph reloads; clear failed-cycle locals
                    # before a necessary rollback to bound recovery memory.
                    traceback.clear_frames(exc.__traceback__);exc.__traceback__=None
                    if self.runtime.dirty:
                        saved=self.journal.load()
                        if saved:self.runtime.restore(saved)
                    self.runtime.events.clear()
                    if any(t in self.error for t in ('corruption','resource ceiling','storage limit','immutable evidence conflict')):
                        self.fatal=True; break
                self.stop.wait(10 if not self.failures else min(60,max(10,2**min(self.failures,6))))
        except Exception as exc:
            self.error=type(exc).__name__+': '+str(exc)[:240]; self.fatal=True
            print(canonical(dict(event='worker_fatal',error=self.error)),flush=True)
        finally:
            self.stop.set()
            if self.stream.thread: self.stream.thread.join(timeout=13)
            if self.journal: self.journal.close()
