"""Experimental forward paper worker; explicitly separate from research gates.

Closed public candles warm structures. Only volume/OHLC-reconciled public
trades advance price-sensitive setups. RC3F can reconstruct provable current
context from closed extrema, without replaying historical signals or fills.
"""
from dataclasses import asdict
import json
import os
import re
import threading
import time
import traceback
from . import checkpoint, delta_paper
from .bybit_public import PublicAPI, TradeStream, coverage, reconciliation, FEED_VERSION, TRADE_BUFFER_LIMIT, TRADE_BUCKET_LIMIT
from .journal import Journal
from .manifest import Manifest
from .model import D, canonical, digest, floor, ceil, number
from .objects import Liquidity
from .portfolio import Arm
from .strategy import Strategy
from .universe import SYMBOLS, META, assess, LIQUIDITY_POLICY
from .metrics import Metrics
from .diagnostics import entry_context
from .fast_strategy import FastStrategy, PROFILE, SPEC, rebuild_context

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
    def __init__(self, journal, symbols=('BTCUSDT',), strategy_profile='RC2', delta_arms=False):
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
        self.configure_strategy(strategy_profile,saved)
        self.delta_entries_enabled = delta_arms and self.fast()
        if self.delta_entries_enabled and 'delta_arms' not in self.state:
            self.state['delta_arms'] = delta_paper.new_state()
            self.log(time.time(), 'delta_arms_initialized', arms=['C', 'D'], capital_per_arm='1000')
        self.delta_enabled = self.fast() and 'delta_arms' in self.state
        if self.delta_enabled and self.state['delta_arms']['version'] != delta_paper.delta.SPEC:
            raise ValueError('explicit Delta Profile migration required')

    def configure_strategy(self,profile,saved):
        if profile not in ('RC2',PROFILE):raise ValueError('unknown strategy profile')
        current=self.state.get('strategy_version','RC2')
        if current==profile:
            if profile==PROFILE and self.state.get('strategy_hash')!=digest(SPEC):raise ValueError('strategy manifest mismatch')
            return
        if current!='RC2' or profile!=PROFILE:raise ValueError('explicit strategy migration required')
        if self.state['unresolved'] or any(a.positions for a in self.state['arms'].values()):
            raise ValueError('RC3F cutover requires resolved flat RC2 books; old journal retained')
        at=time.time();old_books={name:dict(cash=str(a.cash),day_start=str(a.day_start),locked=a.locked,
            streak=a.streak,positions=[],pending=len(a.orders)) for name,a in self.state['arms'].items()}
        for a in self.state['arms'].values():a.halt(at,'strategy_version_cutover')
        strategies={}
        recent={}
        for symbol,old in self.state['strategies'].items():
            recent[symbol]=self.journal.recent_minute_bars(symbol,self.state['previous']) if self.state['previous'] is not None else []
            strategies[symbol]=rebuild_context(old,self.log,recent[symbol],self.state['previous'])
        self.state.setdefault('archived_experiments',[]).append(dict(version='RC2',cutover_at=at,
            books=old_books,counts=dict(self.journal.counts),journal_head=self.journal.head))
        self.state.update(strategies=strategies,pending=[],arms={name:Arm(name,self.policy,self.log) for name in ('A','B')},
            strategy_version=PROFILE,strategy_hash=digest(SPEC),strategy_spec=SPEC,
            experiment_started_at=at,activation_times={},financial_baseline=self.journal.financials())
        self.state['recent_closed_1m']=recent
        self.bind()
        self.log(at,'strategy_version_cutover',from_version='RC2',to_version=PROFILE,manifest=SPEC,
            old_books=old_books,new_capital_per_arm='1000',history='RETAINED_IMMUTABLE',
            context_carried={s:dict(zones=len(x.zones),pools=len(x.liquidity.pools)) for s,x in strategies.items()})
        evidence=[]
        if saved:evidence=[(f'checkpoint-before:{PROFILE}:{at}','strategy_cutover_checkpoint',at,
            dict(format='checkpoint-v1-json',canonical_sha256=digest(saved),payload=saved))]
        self.commit(evidence)

    def fast(self):return self.state.get('strategy_version')==PROFILE

    def bind(self):
        self.policy = PaperPolicy(self.state['contracts'],self.state.setdefault('eligibility',{}) if self.state.get('enforce_liquidity') else None)
        if self.state.get('strategy_version')==PROFILE:self.policy.data['a_activation_window_seconds']=60
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
        if event=='funding_missing' and fields.get('arm') in self.state['arms'] and fields.get('symbol'):
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
        if self.fast():
            from .fast_execution import ambiguous
            return ambiguous(self,symbol,rows)
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
                if not z.valid:continue
                # Zone.update depends only on these three predicates. Equal
                # predicates produce the same idempotent operation in every
                # ordering, including visit/inside/midpoint/invalidation state.
                # Crossing a predicate boundary remains ambiguous. Pool hits
                # below remain protected, as do all existing sweeps/exposure.
                def region(p):
                    return (z.side*(p-z.mid)<=0,
                            p<z.low if z.side==1 else p>z.high,
                            p<z.high if z.side==1 else p>z.low)
                if region(lo)!=region(hi):return True
            for p in strategy.liquidity.pools:
                if not p.consumed and (hi>p.outer if p.kind==1 else lo<p.outer):return True
        # All zone operations commute; no pool can be penetrated by a tie.
        # A sweep started at an earlier unique-time pool hit cannot extend its
        # extreme here without also penetrating that pool, checked above.
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
            if self.fast():self.state['strategies'][symbol]=rebuild_context(s,self.log,as_of=anchor)
            self.log(anchor,'symbol_warmup_complete',symbol=symbol,bars_per_frame=300)
            self.commit(evidence)
        if not self.state.get('bootstrap_recorded_for')==self.state['symbols']:
            self.log(anchor,'bootstrap_complete' if self.journal.counts.get('bootstrap_complete',0)==0 else 'universe_warmup_complete',policy=POLICY,
                 history=SPEC['context'] if self.fast() else 'Structures warmed; all historical POIs/pools ineligible due unknown visits/consumption')
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

    def minute(self, end, frames, marks, streams, settlements, feed_health=None):
        if end!=self.state['previous']+60: raise ValueError('noncontiguous worker minute')
        self.dirty=True
        start=end-60; evidence=[]; reasons=[]; ticks=[]; quality={}
        if self.fast():
            recent=self.state.setdefault('recent_closed_1m',{})
            for symbol in self.state['symbols']:
                recent[symbol]=(recent.get(symbol,[])+[next(b for b in frames[symbol] if b.tf==1)])[-60:]
        if feed_health:evidence.append((f'feed-health:{end}','stream_health',end,feed_health))
        for symbol in self.state['symbols']:
            bars=frames[symbol]; b=next(x for x in bars if x.tf==1)
            if b.start!=start: raise ValueError('incorrect minute')
            for x in bars: evidence.append((f'candle:{symbol}:{x.tf}:{x.start}','candle',x.end,asdict(x)))
            mark=marks[symbol]
            evidence.append((f'mark:{symbol}:{start}','mark',end,asdict(mark)))
            rows,bad=streams[symbol][:2]
            confirmed=streams[symbol][2] if len(streams[symbol])>2 else None
            prior=self.state.get('quality',{}).get(symbol,{}).get('expected',{})
            previous_close=prior.get('c') if prior.get('start')==start-60 else None
            rebuilt=reconciliation(b,rows,bad,previous_close,confirmed);reason=rebuilt['reason']
            evidence.append((f'trades:{symbol}:{start}','public_trades',end,rows))
            if confirmed:evidence.append((f'native:{symbol}:{start}','confirmed_native_candle',end,confirmed))
            # The native opening anchor is not an observed trade at the new
            # boundary. Preserve the entry model: an uncertain activation price
            # still cannot manufacture an A/B opening fill.
            if not self.fast() and reason is None and rows and number(rows[0]['p'])!=b.o and any(
                s.symbol==symbol for s in self.state['pending']) and any(
                symbol in a.orders for a in self.state['arms'].values()):
                reason='opening_trade_reference_ambiguous'
            if self.ambiguous_ties(symbol,rows):reason=reason or 'same_millisecond_price_ambiguity'
            prices=[number(r['p']) for r in rows]
            observed=dict(o=prices[0],h=max(prices),l=min(prices),c=prices[-1],
                          v=sum((number(r['v']) for r in rows),D(0))) if rows else None
            quality[symbol]=dict(reason=reason,reconciliation=rebuilt,expected=asdict(b),observed=observed,
                trade_count=len(rows),trade_evidence_hash=digest(rows),
                first_trade_at=rows[0]['T'] if rows else None,last_trade_at=rows[-1]['T'] if rows else None)
            seqs=[int(r['seq']) for r in rows if r.get('seq') is not None]
            quality[symbol]['sequence']=dict(first=seqs[0] if seqs else None,last=seqs[-1] if seqs else None,
                distinct=len(set(seqs)),note='Cross sequences are not a consecutive per-symbol packet counter.')
            if reason: reasons.append(symbol+':'+reason)
            for i,r in enumerate(rows): ticks.append((r['T'],symbol,i,r))
            for arm in self.state['arms'].values():
                arm.marks[symbol]=mark.o; arm.mark_times[symbol]=start
        bad_symbols={s for s,q in quality.items() if q['reason']}
        armed={s for s in self.state['symbols'] if any(s in a.positions or s in a.orders for a in self.state['arms'].values())}
        times={}
        if self.fast():
            from .fast_execution import material_times
            material={s:material_times(self,s,streams[s][0]) for s in armed}
        for ms,symbol,_,r in ticks:
            if symbol in armed and (not self.fast() or ms in material[symbol]):times.setdefault(ms,set()).add(symbol)
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
                if self.fast():continue  # Receipt-aware activation occurs inside the chronological loop below.
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
                if self.fast():
                    o=self.state['arms']['A'].orders.get(symbol)
                    if o and at>o.submitted:
                        receipt=r.get('received_at')
                        reason=None
                        if at>=o.signal.at+60 or receipt is None or receipt>=o.signal.at+60:reason='a_signal_stale_or_receipt_unavailable'
                        elif receipt<=o.submitted or receipt>o.submitted+5 or not 0<=receipt-at<=2:reason='a_activation_or_receipt_uncertain'
                        if reason:self.state['arms']['A'].cancel(symbol,at,reason)
                        else:
                            rule=o.rules;qty=floor(number(r['v'])*D('.01'),rule['step'])
                            self.log(at,'a_activation_reference',id=o.signal.id,symbol=symbol,trade_id=r['i'],
                                event_at=at,received_at=receipt,activated_at=o.submitted,reference=p)
                            self.state['arms']['A'].attempt_a(o.signal,at,p,
                                dict(qty=str(qty),evidence='EXPERIMENTAL_POST_ACTIVATION_PUBLIC_TRADE:'+r['i']) if qty>0 else None)
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
        if self.fast():
            for symbol in sorted(bad_symbols-armed):
                self.state['strategies'][symbol]=rebuild_context(self.state['strategies'][symbol],self.log,
                    self.state['recent_closed_1m'][symbol],end)
                self.log(end,'flat_context_recovered',symbol=symbol,source='CLOSED_CANDLE_EXTREMA_NO_SIGNALS_REPLAYED',
                    zones=sum(z.valid for z in self.state['strategies'][symbol].zones),
                    pools=sum(not p.consumed for p in self.state['strategies'][symbol].liquidity.pools))
        self.checkpoint_arms(end)
        signals=[]
        if clean and end%300==0:
            for symbol,s in self.state['strategies'].items():
                if symbol not in clean_symbols:continue
                b=next((b for b in frames[symbol] if b.tf==5),None)
                if b: signals.extend(s.five_close(b))
            self.state['pending']=sorted(signals,key=lambda s:(-s.score,s.symbol,s.sweep_at,s.id))
            for signal in self.state['pending']:
                decision=time.time() if self.fast() else end
                self.log(end,'signal',id=signal.id,signal=asdict(signal),policy=self.state['policy_hash'],decision_completed_at=decision,strategy_version=self.state.get('strategy_version','RC2'),strategy_hash=self.state.get('strategy_hash'))
                if self.fast() and decision>=end+60:
                    self.log(decision,'signal_execution_skipped',id=signal.id,reason='decision_after_first_minute_deadline');continue
                for arm in self.state['arms'].values():
                    arm.reserve(signal,decision,signal.close); self.checkpoint_arms(decision)
        if self.fast():
            for symbol,o in list(self.state['arms']['A'].orders.items()):
                if end>=o.signal.at+60:self.state['arms']['A'].cancel(symbol,end,'a_no_timely_reference')
        if end%300==0:
            for symbol in self.state['symbols']:
                found=[x for x in signals if x.symbol==symbol]
                self.log(end,'signal_evaluation',symbol=symbol,disposition='QUALIFIED_SIGNAL' if found else
                    ('NO_QUALIFIED_SIGNAL' if clean and symbol in clean_symbols else 'QUARANTINED_DATA'),
                    signals=len(found),reasons=[quality[symbol]['reason']] if quality[symbol]['reason'] else [],unresolved=len(self.state['unresolved']),
                    context=entry_context(self.state['strategies'][symbol],end,quality[symbol]['reason']))
        if self.delta_enabled:
            delta_paper.minute(self.state['delta_arms'], end, streams, quality, marks, settlements,
                self.policy, self.state['contracts'],
                {symbol: strategy.frames[5].atr.value for symbol, strategy in self.state['strategies'].items()},
                self.log, time.time, self.delta_entries_enabled)
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
            if self.fast():
                old_count=len(s.zones)+len(s.liquidity.pools)
                def current(tf,origin):return bool(s.frames[tf].bars) and origin>=s.frames[tf].bars[0].start
                protected={x.poi.id for x in s.setups.values()}|{z.id for e in s.sweeps.values() for z,_,_ in e.candidates}
                s.zones=[z for z in s.zones if z.valid and z.visits<=2 and (current(z.tf,z.origin) or z.id in protected)]
                s.liquidity.pools=[p for p in s.liquidity.pools if not p.consumed and any(current(p.tf,q.origin) for q in p.members)]
                retained={q.id for p in s.liquidity.pools for q in p.members}
                s.liquidity.members={k:q for k,q in s.liquidity.members.items() if k in retained or current(int(q.id.split(':')[1]),q.origin)}
                s.liquidity.consumed.intersection_update(s.liquidity.members)
                s.liquidity.assigned.intersection_update(s.liquidity.members)
                retired=old_count-len(s.zones)-len(s.liquidity.pools)
                if retired:self.log(end,'context_cache_retired',symbol=s.symbol,count=retired,reason='consumed_invalid_or_300_bar_lookback',evidence='IMMUTABLE_JOURNAL_RETAINED')
            objects+=len(s.zones)+len(s.liquidity.pools)+sum(len(f.bars)+len(f.raw)+len(f.sequence_history) for f in s.frames.values())
        if objects>60000: raise RuntimeError('strategy object resource ceiling; explicit archival/reseed required')
        self.log(end,'minute_processed',clean=clean,trades_observed=len(ticks),reasons=reasons,quality=quality)
        self.commit(evidence)

class Worker:
    def __init__(self, directory, symbols=SYMBOLS, persistent=False, api=None,strategy_profile='RC2', delta_arms=False):
        if not 1<=len(symbols)<=12 or len(set(symbols))!=len(symbols) or any(s not in META for s in symbols):
            raise ValueError('only the twelve explicitly requested native USDT contracts are allowed')
        self.stop=threading.Event(); self.api=api or PublicAPI(); self.directory=directory
        self.symbols=symbols; self.persistent=persistent; self.started=time.time(); self.heartbeat=0
        self.last_success=None; self.error=None; self.failures=0; self.fatal=False; self.thread=None
        self.journal=None; self.runtime=None; self.stream=TradeStream(symbols,self.stop); self.view={}
        self.metrics=Metrics();self.resources={}
        self.steady_started=False
        self.strategy_profile=strategy_profile
        self.delta_arms=delta_arms

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
        financials=self.journal.financials() if self.journal else {}
        for name,base in state.get('financial_baseline',{}).items():
            if name in financials:
                for key,value in base.items():financials[name][key]=str(number(financials[name].get(key,'0'))-number(value))
        delta_reports = delta_paper.reports(state.get('delta_arms'), financials, self.symbols,
                                           self.delta_arms)
        self.view=dict(previous=state.get('previous'),minutes=state.get('minutes',0),
            strategy_version=state.get('strategy_version','RC2'),strategy_spec=state.get('strategy_spec'),
            experiment_started_at=state.get('experiment_started_at'),archived_experiments=state.get('archived_experiments',[]),
            boots=state.get('boots',0),last_clean=state.get('last_clean'),
            last_quarantine=state.get('last_quarantine'),unresolved=state.get('unresolved',[])[:30],
            quality=state.get('quality',{}),
            entry_context={s:entry_context(x,state.get('previous') or int(time.time()),
                state.get('quality',{}).get(s,{}).get('reason')) for s,x in state.get('strategies',{}).items()},
            arms=dict(arms, **delta_reports), delta_reports=delta_reports,
            eligibility=state.get('eligibility',{}),clean_symbols=state.get('clean_symbols',[]),
            feed_counts=state.get('feed_counts',{}),
            warmup={s:{str(tf):f.atr.count for tf,f in x.frames.items()} for s,x in state.get('strategies',{}).items()},
            counts=dict(self.journal.counts) if self.journal else {},
            evidence_counts=dict(self.journal.evidence_counts) if self.journal else {},
            journal_head=self.journal.head if self.journal else None,
            journal_sequence=self.journal.sequence if self.journal else 0,
            storage_bytes=storage,resources=self.resources,
            journal_budget_bytes=self.journal.max_bytes if self.journal else None,
            financials=financials)

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
        reports = self.view.get('delta_reports', {})
        if reports and not (ready and self.stream.connected):
            reports = {name: dict(report, data_valid=False, markets={symbol:
                dict(market, status='DATA UNAVAILABLE', reason='worker_or_stream_not_current')
                for symbol, market in report['markets'].items()}) for name, report in reports.items()}
        return dict(self.view,arms=dict(self.view.get('arms', {}), **reports), delta_reports=reports,
            worker_alive=live,forward_paper_operational=operational,
            ready=ready,execution_ready=execution,persistence_configured=self.persistent,
            status='EXPERIMENTAL_PAPER_RUNNING' if ready else ('STARTING' if not self.error else 'DEGRADED'),
            initializing=initializing,
            error=self.error,consecutive_failures=self.failures,heartbeat_at=self.heartbeat,
            last_success_at=self.last_success,worker_uptime_seconds=int(now-self.started),
            public_api_requests=self.api.requests,stream_connected=self.stream.connected,
            stream_trades_received=self.stream.total,stream_reconnects=self.stream.reconnects,
            stream_error=self.stream.last_error,symbols=list(self.symbols),policy=(dict(POLICY,
                version=PROFILE,activation=SPEC['activation'],ordering='Harmless same-ms price operations commute; ambiguous activation, protection and shared-account effects remain quarantined.',
                historical_context=SPEC['context'],data_loss='Cancel unfilled entries and erase path-dependent setups; recover only provable current closed-bar context when flat. Held uncertainties remain unresolved.',
                strategy_manifest=self.view.get('strategy_spec')) if self.view.get('strategy_version')==PROFILE else POLICY),
            monitored_symbol_count=len(self.symbols),execution_ready_count=len(eligible) if execution else 0,
            all_feeds_ready=ready and len(clean)==len(self.symbols),coins=coins,
            api_metrics=dict(requests=self.api.requests,by_path=dict(self.api.by_path),errors=self.api.errors,
                rate_limits=self.api.rate_limits,total_request_seconds=self.api.elapsed),
            stream_metrics=dict(buffered=self.stream.buffered,peak_buffered=self.stream.peak_buffered,
                buffer_limit=TRADE_BUFFER_LIMIT,per_minute_symbol_limit=TRADE_BUCKET_LIMIT,overflow_records=self.stream.overflow,
                confirmed_candles_received=self.stream.confirmed_received,
                sequence_regressions=self.stream.sequence_regressions,
                late_pending=len(self.stream.late),late_overflow=self.stream.late_overflow,
                subscription_ack_at=self.stream.subscription_ack_at,integrity=self.stream.integrity()),
            research_certified=False,profitability_certified=False)

    def cycle(self):
        began=time.monotonic();r=self.runtime; now=int(time.time())
        late=self.stream.take_late()
        if late:
            affected=sorted({x['trade']['s'] for x in late})
            r.interrupted(now,'late_trade_after_minute_sealed',affected)
            if r.delta_enabled:
                delta_paper.invalidate(r.state['delta_arms'], affected, now, 'late_trade_after_minute_sealed', r.log)
            r.state['clean_symbols']=[s for s in r.state.get('clean_symbols',[]) if s not in affected]
            r.log(now,'late_trade_evidence',symbols=affected,count=len(late),evidence_hash=digest(late))
            r.commit([(f'late-trades:{now}:{digest(late)}','late_public_trades',now,late)])
        if self.stream.late_overflow:raise RuntimeError('late trade evidence resource ceiling')
        r.update_contracts(self.api,now)
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
            streams={s:(*self.stream.take(s,end-60),self.stream.take_native(s,end-60)) for s in self.symbols}
            r.minute(end,by_end[end],mark_map[end],streams,settlements,feed_health=self.stream.integrity())
            self.heartbeat=time.time(); self.last_success=time.time(); self.publish()
        self.metrics.cycles.append(time.monotonic()-began);self.metrics.cycles=self.metrics.cycles[-60:]

    def run(self):
        try:
            self.journal=Journal(self.directory,max_bytes=4_000_000_000)
            self.runtime=Runtime(self.journal,self.symbols,self.strategy_profile,self.delta_arms)
            self.runtime.state['enforce_liquidity']=True;self.runtime.bind()
            self.runtime.state['boots']+=1
            if self.runtime.state.get('feed_reconciliation_version')!=FEED_VERSION:
                self.runtime.log(time.time(),'feed_reconciliation_changed',
                    previous=self.runtime.state.get('feed_reconciliation_version','strict-first-trade-1'),
                    version=FEED_VERSION,policy='Exact native candle reconstruction; identical zone-region operations commute; pool hits, active context/exposure and predicate crossings remain protected.',
                    trade_buffer_limit=TRADE_BUFFER_LIMIT,trade_bucket_limit=TRADE_BUCKET_LIMIT)
                self.runtime.state['feed_reconciliation_version']=FEED_VERSION
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
