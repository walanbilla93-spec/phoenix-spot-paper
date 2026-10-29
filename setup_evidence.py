"""Compact evidence for setup research. No orders, keys or trading decisions.

Price levels are exact Decimal strings, not logarithmic buckets. Every cap,
gap, late trade or missing start becomes an explicit eligibility failure.
"""
from collections import OrderedDict, deque
from decimal import Decimal, InvalidOperation
import json

MINUTE = 60_000


class ExactFootprints:
    def __init__(self, symbols, grace_ms=5_000, level_limit=4096, id_limit=12000):
        self.symbols = tuple(symbols)
        self.grace = grace_ms
        self.level_limit = level_limit
        self.id_limit = id_limit
        self.bins = {s: {} for s in symbols}
        self.ids = {s: OrderedDict() for s in symbols}
        self.started = {s: None for s in symbols}
        self.watermark = {s: -1 for s in symbols}
        self.stats = dict(minutes=0, duplicate_trades=0, late_trades=0,
                          invalid_trades=0, capped_minutes=0)

    def _flush(self, s, ts, force=False, reason=None):
        out = []
        for minute in sorted(tuple(self.bins[s])):
            if not force and minute + MINUTE + self.grace > ts:
                continue
            b = self.bins[s].pop(minute)
            flags = sorted(b['flags'] | ({reason} if reason else set()))
            out.append(dict(kind='footprint_1m', schema_version=1, symbol=s,
                receive_ms=ts, minute_ms=minute, available_ms=ts,
                price_resolution='EXACT_TRADED_PRICE', deduplication='BOUNDED_TRADE_ID',
                complete_observation=not flags, quality_flags=flags,
                buy_volume=str(b['buy']), sell_volume=str(b['sell']),
                trade_count=b['count'], levels=[
                    [p, str(v[0]), str(v[1]), v[2], v[3]]
                    for p, v in sorted(b['levels'].items(), key=lambda x: Decimal(x[0]))]))
            self.watermark[s] = max(self.watermark[s], minute)
            self.stats['minutes'] += 1
            self.stats['capped_minutes'] += 'LEVEL_CAP' in flags or 'DEDUPE_CAP' in flags
        return out

    def process(self, s, payload, ts):
        if self.started[s] is None:
            self.started[s] = ts
        out = self._flush(s, ts)
        seen = self.ids[s]
        # IDs for the current and preceding two exchange minutes only.
        while seen and next(iter(seen.values())) < ts - 180_000:
            seen.popitem(last=False)
        if not payload.get('topic', '').startswith('publicTrade.'):
            return out
        for t in payload.get('data', []):
            try:
                trade_ts = int(t['T']); minute = trade_ts // MINUTE * MINUTE
                p, volume = Decimal(t['p']), Decimal(t['v'])
                side, tid = t['S'], str(t['i'])
                if not p.is_finite() or not volume.is_finite() or p <= 0 or volume <= 0 or side not in ('Buy', 'Sell') or not tid:
                    raise ValueError('INVALID_TRADE')
                if trade_ts > ts + 5_000:
                    raise ValueError('FUTURE_EXCHANGE_TIMESTAMP')
            except (KeyError, ValueError, TypeError, InvalidOperation):
                self.stats['invalid_trades'] += 1
                out.append(dict(kind='footprint_quality_error', symbol=s, receive_ms=ts,
                                reason='INVALID_TRADE', minute_ms=ts//MINUTE*MINUTE))
                continue
            if tid in seen:
                self.stats['duplicate_trades'] += 1
                continue
            if minute <= self.watermark[s] or minute + MINUTE + self.grace <= ts:
                self.stats['late_trades'] += 1
                out.append(dict(kind='footprint_late_trade', symbol=s, receive_ms=ts,
                                minute_ms=minute, reason='INVALIDATE_PREVIOUS_FOOTPRINT'))
                continue
            # At most two exchange minutes fit inside the 5s grace. Fail closed
            # on unexpected timestamp ranges instead of growing historical maps.
            if minute not in self.bins[s] and len(self.bins[s]) >= 3:
                out.append(dict(kind='footprint_quality_error', symbol=s, receive_ms=ts,
                                minute_ms=minute, reason='OPEN_MINUTE_CAP'))
                continue
            if minute not in self.bins[s]:
                self.bins[s][minute] = dict(levels={}, buy=Decimal(0), sell=Decimal(0),
                    count=0, flags={'STARTUP_OR_RECOVERY_PARTIAL'} if self.started[s] > minute else set())
            b = self.bins[s][minute]
            if len(seen) >= self.id_limit:
                seen.popitem(last=False)
                # Loss of duplicate protection invalidates every open minute.
                for current in self.bins[s].values():
                    current['flags'].add('DEDUPE_CAP')
            seen[tid] = trade_ts
            pkey = format(p.normalize(), 'f')
            if pkey not in b['levels'] and len(b['levels']) >= self.level_limit:
                b['flags'].add('LEVEL_CAP')
            else:
                v = b['levels'].setdefault(pkey, [Decimal(0), Decimal(0), 0, 0])
                pos = 0 if side == 'Buy' else 1
                v[pos] += volume; v[2 + pos] += 1
            b['buy' if side == 'Buy' else 'sell'] += volume
            b['count'] += 1
        return out

    def interrupt(self, ts, reason):
        out = []
        for s in self.symbols:
            out.extend(self._flush(s, ts, True, reason))
            self.ids[s].clear(); self.started[s] = None
            self.watermark[s] = max(self.watermark[s], ts//MINUTE*MINUTE - MINUTE)
        return out

    def status(self):
        return dict(schema='EXACT_PRICE_FOOTPRINT_V1', **self.stats,
            open_minutes=sum(len(v) for v in self.bins.values()),
            retained_levels=sum(len(b['levels']) for v in self.bins.values() for b in v.values()),
            retained_trade_ids=sum(len(v) for v in self.ids.values()),
            level_limit_per_minute=self.level_limit, id_limit_per_coin=self.id_limit,
            grace_ms=self.grace, session_profile='RECONSTRUCT_OFFLINE_FROM_VALIDATED_MINUTES',
            note='Reconcile with exchange candle volume; no complete session across a gap or startup.')


class EvidenceMixin:
    def __init__(self, symbols):
        super().__init__(symbols)
        self.exact = ExactFootprints(symbols)
        self.context = {s: deque(maxlen=300) for s in symbols}
        self.setup_watches = {s: None for s in symbols}
        self.setup_counts = dict(watched=0, sweep_break_candidates=0, expired=0, invalidated=0)

    def process(self, s, payload, data, ts):
        exact = [json.dumps(r, separators=(',', ':')).encode()+b'\n'
                 for r in self.exact.process(s, payload, ts)]
        selected = super().process(s, payload, data, ts)
        out = exact + selected
        for encoded in selected:
            try:
                r = json.loads(encoded)
            except (ValueError, UnicodeDecodeError):
                continue
            if r.get('kind') == 'candle_1m':
                c = dict(r['candle'], available_ms=ts)
                self.context[s].append(c)
                out.extend(self._advance_watch(s, c, ts))
            if r.get('kind') == 'capture_window_start' and self.setup_watches[s] is None:
                # A review packet freezes what was known at touch. It does not
                # assert that a provisional zone is the intended higher-TF POI.
                zones = r['zones']; side = 1 if zones[0]['side']=='bull' else -1
                same = [z for z in zones if (z['side']=='bull') == (side==1)]
                watch = dict(packet_id=r['window_id'], touch_ms=ts,
                    expires_ms=ts+3_600_000, side=side, zones=same,
                    phase='WAITING_SWEEP_AND_STRUCTURE', sweep=None)
                self.setup_watches[s] = watch; self.setup_counts['watched'] += 1
                out.append(self._record('setup_context', s, ts, **watch,
                    closed_1m_context=list(self.context[s]), quote=self.latest_quotes[s],
                    confluence={k:'UNCONFIRMED' for k in ('elliott','smc_context','inducement','mss','profile','footprint')},
                    qualification='NOT_QUALIFIED', rule_version='REVIEW_SEQUENCE_PROXY_V1'))
        return out

    def _record(self, kind, s, ts, **fields):
        return json.dumps(dict(kind=kind, symbol=s, receive_ms=ts, **fields),
                          separators=(',', ':')).encode()+b'\n'

    def _advance_watch(self, s, c, ts):
        w = self.setup_watches[s]
        if not w:
            return []
        reason = None
        if ts >= w['expires_ms']:
            reason = 'EXPIRED'; self.setup_counts['expired'] += 1
        elif all(c['c'] < z['low'] if w['side']==1 else c['c'] > z['high'] for z in w['zones']):
            reason = 'POI_CLOSE_INVALIDATION'; self.setup_counts['invalidated'] += 1
        if reason:
            self.setup_watches[s] = None
            return [self._record('setup_watch_end', s, ts, packet_id=w['packet_id'],
                reason=reason, phase=w['phase'], trade_outcome='NOT_A_TRADE',
                qualification='NOT_QUALIFIED')]
        # Use the same causal screen as offline review, including the
        # already-broken-reference rejection. No retrospective pivot revision.
        from setup_review import observable_sequence
        seq = observable_sequence(list(self.context[s]), w['touch_ms'], ts+1, w['side'])
        if seq['state']=='SWEEP_THEN_CLOSE_BREAK_PROXY' and w['phase']!='REVIEW_CANDIDATE':
            w['phase']='REVIEW_CANDIDATE'; self.setup_counts['sweep_break_candidates'] += 1
            return [self._record('setup_review_candidate', s, ts, packet_id=w['packet_id'],
                evidence=seq, quote=self.latest_quotes[s], flow=self.flow_metrics(s,ts),
                qualification='NEEDS_FULL_CONFLUENCE_REVIEW',
                missing=['ELLIOTT_COUNT','HIGHER_TIMEFRAME_POI','INDUCEMENT_INTERPRETATION',
                         'VALIDATED_SESSION_PROFILE','FOOTPRINT_CONFIRMATION','FROZEN_EXECUTION_PLAN'],
                note='Observable sequence only; not a signal, MSS classification or paper entry.')]
        return []

    def interrupt(self, ts, reason):
        exact = [json.dumps(r, separators=(',', ':')).encode()+b'\n'
                 for r in self.exact.interrupt(ts, reason)]
        for s, w in self.setup_watches.items():
            if w:
                exact.append(self._record('setup_watch_end',s,ts,packet_id=w['packet_id'],
                    reason=reason,qualification='CENSORED_DATA_GAP',trade_outcome='NOT_A_TRADE'))
            self.setup_watches[s] = None; self.context[s].clear()
        return exact + super().interrupt(ts, reason)

    def status(self):
        result = super().status()
        result['setup_evidence'] = self.exact.status()
        result['setup_review'] = dict(**self.setup_counts,
            active=sum(w is not None for w in self.setup_watches.values()),
            max_context_candles_per_coin=300, watch_expiry_minutes=60,
            qualified_setups=None, qualification_source='PERSISTENT_RESEARCH_LEDGER', automatic_trading=False)
        result['setup_readiness'] = 'REQUIRES_CAUSAL_ANNOTATIONS_AND_OFFLINE_QUALITY_CHECK'
        return result

