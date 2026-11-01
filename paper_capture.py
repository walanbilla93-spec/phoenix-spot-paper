"""Unified two-confirmation research source. No strict strategy is run.

Reuse causal minute reconciliation and unchanged OB/FVG formation helpers.
The sole signal decision and three parallel paper plans live in PaperTrials.
"""
from research_engine import ResearchEngine,atr,pivots,DAY,FIVE
import math,hashlib

SOURCE_POLICY={'id':'UNIFIED_TWO_CONFIRMATION_CAPTURE_SOURCE_R1',
 'role':'Validated price/footprint bars and predeclared zone arrivals only; PaperTrials is the only signal/entry engine.',
 'signals':'POI plus at least one independent MSS, liquidity sweep/reclaim, or footprint confirmation. No Elliott or strict seven-gate rejection.'}

class UnifiedCapture(ResearchEngine):
    def __init__(self,root,symbols,trials,emit=None,recover_ms=None):
        self.trials=trials
        super().__init__(root,symbols,emit,recover_ms,policy=SOURCE_POLICY,ledger_name='research_capture.sqlite')
    def metadata(self,name,payload,ts):
        super().metadata(name,payload,ts)
        for s in self.symbols:
            if name=='warmup_5m_'+s:
                for k in sorted(payload.get('result',{}).get('list',[]),key=lambda x:int(x[0])):
                    t=int(k[0])
                    if t+FIVE<=ts and (not self.m5[s] or t>self.m5[s][-1]['t']):
                        self.m5[s].append(dict(t=t,o=float(k[1]),h=float(k[2]),l=float(k[3]),c=float(k[4]),v=k[5],available_ms=ts,source='REST_WARMUP'))
    def _watch(self,s,zone,quote,ts):
        side=zone['side'];bars=list(self.m5[s]);instrument=self.instruments.get(s)
        local=atr(bars);ps=[p for p in pivots(bars[-28:]) if p['available_ms']<=ts]
        ref=next((p for p in reversed(ps) if p['type']==('H' if side==1 else 'L')),None)
        unbroken=bool(ref) and not any(k['available_ms']>=ref['available_ms'] and side*(k['c']-ref['price'])>0 for k in bars)
        liq=next((p for p in reversed(ps) if p['type']==('L' if side==1 else 'H')),None)
        if liq and any(k['available_ms']>=liq['available_ms'] and (k['l']<liq['price'] if side==1 else k['h']>liq['price']) for k in bars):liq=None
        nearer=next((z for z in reversed(self.failed_zones[s]) if z['side']==side and z['failed_ms']<ts and ts-z['failed_ms']<DAY and (z['low']>zone['high'] if side==1 else z['high']<zone['low'])),None)
        gates=dict(poi=dict(passed=True,zone=dict(zone),touch_ms=ts,quote=quote),smc_context=dict(passed=False,reason='OPTIONAL_LOGGED_BY_TRIALS'),profile=dict(passed=False,reason='NOT_REQUIRED'),mss=dict(passed=False),liquidity=dict(passed=False),footprint=dict(passed=False))
        w=dict(id=hashlib.sha256(f'{s}:{zone["id"]}:{ts}'.encode()).hexdigest()[:24],side=side,touch_ms=ts,expires_ms=min(ts+90*60000,ts//DAY*DAY+DAY),zone=dict(zone),gates=gates,
            reference=ref,reference_unbroken=unbroken,liquidity=liq,nearer=nearer,h1_atr=atr(self.h1[s]),local_atr=local,
            instrument=dict(instrument) if instrument else None,row_width=instrument['tick']*max(1,math.ceil(.05*local/instrument['tick'])) if instrument and local else None,
            rejection=None,last_close=bars[-1]['c'] if bars else None)
        zone['touched']=True
        self._event('research_watch',ts,s,setup_id=w['id'],frozen=w,price_context_5m=bars[-30:],source='UNIFIED_TWO_CONFIRMATION_SOURCE')
        # No hidden strict rejection and no second entry engine.
        self.watches[s]=None
    def _closed_bar(self,s,bar,levels,ts):
        super()._closed_bar(s,bar,levels,ts)
        self.trials.guarded('base_event',dict(kind='research_bar_5m',symbol=s,receive_ms=ts,bar=bar,coverage='FIVE_RECONCILED_EXACT_MINUTES',footprint_levels=levels))
    def _advance_watch(self,*args):pass
    def _prepare_add(self,*args):pass
    def _try_entry(self,*args):raise RuntimeError('SECOND_ENTRY_ENGINE_DISABLED')
    def status(self):
        value=super().status();value.update(policy_id=self.policy['id'],policy_sha256=self.policy_hash,
            mode='UNIFIED_SIGNAL_SOURCE',readiness='Validated source for the single two-confirmation paper engine. No strict engine runs.')
        return value
