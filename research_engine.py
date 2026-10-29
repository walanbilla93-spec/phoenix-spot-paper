"""Causal confluence research and hypothetical execution. Never places orders.

The policy is a frozen research interpretation, not recovered private rules.
SQLite contains the bounded derived index and an append-only decision ledger;
raw capture remains authoritative. No historical payload arrays are rebuilt.
"""
from collections import Counter, deque
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import math
import sqlite3
import threading

MINUTE = 60_000
FIVE = 5 * MINUTE
HOUR = 60 * MINUTE
DAY = 24 * HOUR
LAYERS = ('elliott', 'smc_context', 'poi', 'inducement', 'mss', 'profile', 'footprint')
POLICY = json.loads(Path(__file__).with_name('research_policy.json').read_text())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


POLICY_HASH = hashlib.sha256(canonical(POLICY).encode()).hexdigest()


def finite(value, positive=False):
    n = float(value)
    if not math.isfinite(n) or (positive and n <= 0):
        raise ValueError('NONFINITE_OR_NONPOSITIVE')
    return n


def atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    values = [max(k['h'] - k['l'], abs(k['h'] - p['c']), abs(k['l'] - p['c']))
              for p, k in zip(bars, list(bars)[1:])]
    value = sum(values[:period]) / period
    for tr in values[period:]:
        value = (value * (period - 1) + tr) / period
    return value if value > 0 else None


def pivots(bars):
    rows = list(bars)
    result = []
    for i in range(2, len(rows) - 2):
        k = rows[i]
        other = rows[i-2:i] + rows[i+1:i+3]
        for tag, key, sign in [('H', 'h', 1), ('L', 'l', -1)]:
            if all(sign * (k[key] - x[key]) > 0 for x in other):
                result.append(dict(type=tag, price=k[key], t=k['t'],
                                   available_ms=rows[i+2]['available_ms']))
    return result


def context_gates(bars, side, at_ms, mark=None):
    rows = [k for k in bars if k['available_ms'] <= at_ms]
    ps = pivots(rows)
    contiguous = bool(rows) and at_ms-rows[-1]['t']<=2*HOUR+15000 and all(b['t']-a['t'] == HOUR for a,b in zip(rows,rows[1:]))
    high = [p for p in ps if p['type'] == 'H'][-2:]
    low = [p for p in ps if p['type'] == 'L'][-2:]
    smc = contiguous and len(high) == len(low) == 2 and all(side * (x[1]['price'] - x[0]['price']) > 0 for x in (high, low))
    # Alternating confirmed pivots; repeated same-kind pivots replace only the
    # current candidate. All anchors are frozen into the watch at arrival.
    alternating = []
    for p in ps:
        if alternating and alternating[-1]['type'] == p['type']:
            sign = 1 if p['type'] == 'H' else -1
            if sign * (p['price'] - alternating[-1]['price']) > 0:
                alternating[-1] = p
        else:
            alternating.append(p)
    anchors = alternating[-3:]
    expected = ['L', 'H', 'L'] if side == 1 else ['H', 'L', 'H']
    wave = False
    retracement = None
    if len(anchors) == 3 and [p['type'] for p in anchors] == expected:
        a, b, c = [p['price'] for p in anchors]
        impulse = side * (b - a)
        retracement = side * (b - c) / impulse if impulse > 0 else None
        current = mark if mark is not None else rows[-1]['c']
        wave = (contiguous and retracement is not None and .382 <= retracement <= .786
                and side * (c - a) > 0 and side * (current - c) >= 0)
    return dict(elliott=dict(passed=bool(wave), rule='CONFIRMED_H1_WAVE12_HYPOTHESIS',
                            anchors=anchors, retracement=retracement,
                            invalidation=anchors[-1]['price'] if len(anchors) == 3 else None,
                            alternate='WAVE2_CONTINUES_UNCONFIRMED',
                            alternate_invalidation=anchors[0]['price'] if len(anchors) == 3 else None),
                smc_context=dict(passed=bool(smc), highs=high, lows=low),
                available_ms=at_ms)


def footprint_permission(levels, side, width):
    if not width or width <= 0 or len(levels) > 4096:
        return dict(passed=False, reason='FOOTPRINT_WIDTH_OR_CAP')
    grid = {}
    total = buy = sell = 0.
    for p, b, a in levels:
        price, bv, sv = finite(p, True), finite(b), finite(a)
        if bv < 0 or sv < 0:
            return dict(passed=False, reason='NEGATIVE_FOOTPRINT_VOLUME')
        row = math.floor(price / width + 1e-9)
        q = grid.setdefault(row, [0., 0.])
        q[0] += price * bv; q[1] += price * sv
        buy += price * bv; sell += price * sv
    total = buy + sell
    delta = (buy - sell) / total if total else 0.
    qualifying = []
    for row, value in sorted(grid.items()):
        numerator = value[0 if side == 1 else 1]
        denominator = grid.get(row - side, [0., 0.])[1 if side == 1 else 0]
        if denominator > 0 and numerator >= 3 * denominator and numerator >= .01 * total:
            qualifying.append(row)
    stack = any(qualifying[i:i+3] == list(range(qualifying[i], qualifying[i] + 3))
                for i in range(max(0, len(qualifying)-2)))
    return dict(passed=bool(side * delta >= .15 and stack), delta_ratio=delta,
                stacked_rows=qualifying[:64], row_width=width, turnover=total,
                row_count=len(grid), measurement='BYBIT_TAKER_SIDE_QUOTE_TURNOVER')


class ResearchEngine:
    def __init__(self, root, symbols, emit=None, recover_ms=None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'research.sqlite'
        self.lock = threading.RLock()
        self.symbols = tuple(symbols)
        self.emit = emit
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript('''PRAGMA cache_size=-4096; PRAGMA temp_store=FILE;
        PRAGMA synchronous=FULL; PRAGMA journal_mode=DELETE;
        PRAGMA max_page_count=8192;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,at INTEGER,kind TEXT,s TEXT,payload TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS minutes(s TEXT,t INTEGER,v TEXT,valid INTEGER,reason TEXT,PRIMARY KEY(s,t));
        CREATE TABLE IF NOT EXISTS levels(s TEXT,day INTEGER,p TEXT,v TEXT,PRIMARY KEY(s,day,p));
        CREATE TABLE IF NOT EXISTS days(s TEXT,day INTEGER,invalid INTEGER DEFAULT 0,PRIMARY KEY(s,day));
        CREATE TABLE IF NOT EXISTS profiles(s TEXT,day INTEGER,payload TEXT,PRIMARY KEY(s,day));
        CREATE TABLE IF NOT EXISTS position(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT);
        CREATE TABLE IF NOT EXISTS revoked(campaign_id TEXT PRIMARY KEY,reason TEXT);
        CREATE INDEX IF NOT EXISTS ledger_kind ON ledger(kind);
        CREATE INDEX IF NOT EXISTS ledger_symbol_kind ON ledger(s,kind);
        ''')
        self.counts = Counter(dict(self.db.execute('SELECT kind,count(*) FROM ledger GROUP BY kind')))
        self.blockers = Counter()
        for text, in self.db.execute("SELECT payload FROM ledger WHERE kind='research_decision'"):
            self.blockers.update(json.loads(text).get('blockers', []))
        row = self.db.execute('SELECT hash FROM ledger ORDER BY id DESC LIMIT 1').fetchone()
        self.previous_hash = row[0] if row else '0' * 64
        self.halted = None; self.last_ms = 0; self.last_housekeeping_day = None
        self.instruments = {}; self.h1 = {s: deque(maxlen=240) for s in symbols}
        self.m1 = {s: deque(maxlen=60) for s in symbols}
        self.m5 = {s: deque(maxlen=300) for s in symbols}
        self.hour_bars = {s: deque(maxlen=12) for s in symbols}
        self.pending = {s: {} for s in symbols}; self.five = {s: None for s in symbols}
        self.zones = {s: deque(maxlen=8) for s in symbols}
        self.failed_zones = {s: deque(maxlen=8) for s in symbols}
        self.watches = {s: None for s in symbols}; self.quotes = {}
        self.ready = {}; self.last_quote_ms = {}; self.position = None
        self.net_R = self.gross_R = self.cost_R = 0.
        self.wins = self.losses = self.flat = 0; self.equity_peak = self.max_drawdown_R = 0.
        for text, in self.db.execute("SELECT payload FROM ledger WHERE kind='paper_closed' AND json_extract(payload,'$.campaign_id') NOT IN (SELECT campaign_id FROM revoked) ORDER BY id"):
            p = json.loads(text); n = p['net_R']; self.net_R += n
            self.gross_R += p['gross_R']; self.cost_R += p['cost_R']
            self.wins += n > 1e-12; self.losses += n < -1e-12; self.flat += abs(n) <= 1e-12
            self.equity_peak = max(self.equity_peak, self.net_R)
            self.max_drawdown_R = max(self.max_drawdown_R, self.equity_peak-self.net_R)
        old = self.db.execute('SELECT payload FROM position WHERE id=1').fetchone()
        if old:
            p = json.loads(old[0])
            self.db.execute('DELETE FROM position')
            self._event('paper_censored', recover_ms or p['last_ms'], p['symbol'],
                        campaign_id=p['campaign_id'], reason='SERVICE_RESTART_UNOBSERVED_INTERVAL')
        policy = self.db.execute("SELECT v FROM meta WHERE k='policy_hash'").fetchone()
        if policy and policy[0] != POLICY_HASH:
            raise ValueError('RESEARCH_POLICY_CHANGED_REQUIRES_SEPARATE_LEDGER')
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('policy_hash', POLICY_HASH))
        self.db.commit()

    def _event(self, kind, ts, symbol=None, **fields):
        if sum(self.counts.values()) >= 200_000:
            raise RuntimeError('RESEARCH_LEDGER_CAPACITY')
        record = dict(kind=kind, receive_ms=ts, symbol=symbol, rule_version=POLICY['id'],
                      policy_sha256=POLICY_HASH, **fields)
        encoded = canonical(record)
        digest = hashlib.sha256((self.previous_hash + encoded).encode()).hexdigest()
        self.db.execute('INSERT INTO ledger(at,kind,s,payload,hash) VALUES (?,?,?,?,?)',
                        (ts, kind, symbol, encoded, digest))
        # Commit the associated position change before publishing the event.
        # A crash can censor an open campaign, never erase a completed outcome.
        self.db.commit()
        self.previous_hash = digest; self.counts[kind] += 1
        if self.emit:
            self.emit(dict(record, ledger_hash=digest))
        return record

    def _save_position(self):
        if self.position:
            self.db.execute('INSERT OR REPLACE INTO position VALUES (1,?)', (canonical(self.position),))
        else:
            self.db.execute('DELETE FROM position')

    def metadata(self, name, payload, ts):
        with self.lock, self.db:
            for s in self.symbols:
                if name == 'instrument_' + s:
                    rows = payload.get('result', {}).get('list', [])
                    if rows and rows[0].get('symbol') == s:
                        r = rows[0]; pf = r.get('priceFilter', {}); lot = r.get('lotSizeFilter', {})
                        self.instruments[s] = dict(tick=finite(pf['tickSize'], True),
                            step=finite(lot['qtyStep'], True), minimum=finite(lot['minOrderQty'], True),
                            min_notional=finite(lot.get('minNotionalValue', '5'), True), available_ms=ts)
                if name == 'warmup_1h_' + s:
                    rows = payload.get('result', {}).get('list', [])
                    bars = []
                    for k in rows:
                        t = int(k[0])
                        if t + HOUR <= ts:
                            bars.append(dict(t=t, o=finite(k[1], True), h=finite(k[2], True),
                                l=finite(k[3], True), c=finite(k[4], True), available_ms=ts))
                    # Metadata is available NOW; never backdate its availability.
                    if not self.h1[s]:
                        self.h1[s].extend(sorted(bars, key=lambda k:k['t'])[-240:])

    def _invalidate_day(self, s, minute, reason):
        day = minute // DAY * DAY
        self.db.execute('INSERT INTO days VALUES (?,?,1) ON CONFLICT(s,day) DO UPDATE SET invalid=1', (s, day))
        self.db.execute('UPDATE minutes SET valid=0,reason=? WHERE s=? AND t=?', (reason, s, minute))
        self.db.execute('DELETE FROM profiles WHERE s=? AND day=?', (s, day))
        p = self.position
        if p and p['symbol'] == s and (p.get('profile_day') == day or minute + MINUTE >= p['setup'].get('touch_ms',p['signal_ms'])):
            self._censor(s, self.last_ms, reason)
        if s in self.ready and (self.ready[s]['profile_day'] == day or minute+MINUTE >= self.ready[s]['setup'].get('touch_ms',self.ready[s]['signal_ms'])):
            self._censor(s, self.last_ms, reason)
        w = self.watches[s]
        if w and w['gates']['profile'].get('day') == day:
            w['gates']['profile'].update(passed=False, reason=reason)
        if w and minute+MINUTE>=w['touch_ms']:
            self._decision(s,self.last_ms,w,[reason],'DATA_INSUFFICIENT');self.watches[s]=None
        changed = False
        for payload, in self.db.execute("SELECT payload FROM ledger WHERE s=? AND kind='paper_closed' AND json_extract(payload,'$.campaign_id') NOT IN (SELECT campaign_id FROM revoked)", (s,)):
            c = json.loads(payload)
            if c['profile_day'] == day or (minute+MINUTE > c.get('touch_ms',c['entry_ms']) and minute <= c['closed_ms']):
                self.db.execute('INSERT INTO revoked VALUES (?,?)', (c['campaign_id'], reason))
                self._event('paper_revoked', self.last_ms, s, campaign_id=c['campaign_id'], reason=reason,
                            outcome='REMOVED_FROM_CLOSED_DENOMINATOR')
                changed = True
        if changed:
            self.net_R = self.gross_R = self.cost_R = self.equity_peak = self.max_drawdown_R = 0.
            self.wins = self.losses = self.flat = 0
            for payload, in self.db.execute("SELECT payload FROM ledger WHERE kind='paper_closed' AND json_extract(payload,'$.campaign_id') NOT IN (SELECT campaign_id FROM revoked) ORDER BY id"):
                c = json.loads(payload); n = c['net_R']; self.net_R += n
                self.gross_R += c['gross_R']; self.cost_R += c['cost_R']
                self.wins += n > 1e-12; self.losses += n < -1e-12; self.flat += abs(n) <= 1e-12
                self.equity_peak = max(self.equity_peak, self.net_R)
                self.max_drawdown_R = max(self.max_drawdown_R, self.equity_peak-self.net_R)

    def boundary(self, ts, reason):
        with self.lock, self.db:
            self.last_ms = max(self.last_ms, ts)
            self._censor(None, ts, reason)
            for s in self.symbols:
                self._invalidate_day(s, ts, reason)
                if self.watches[s]:
                    self._decision(s, ts, self.watches[s], [reason], 'OUTCOME_CENSORED')
                self.pending[s].clear(); self.five[s] = None; self.m1[s].clear(); self.m5[s].clear()
                self.hour_bars[s].clear()
                self.zones[s].clear(); self.failed_zones[s].clear(); self.watches[s] = None
            for s, p in tuple(self.ready.items()):
                self._event('paper_unfilled', ts, s, campaign_id=p['campaign_id'], reason=reason)
            self.ready.clear(); self.quotes.clear(); self.last_quote_ms.clear()
            self._event('research_boundary', ts, reason=reason)

    def _profile(self, s, day, tick):
        cached = self.db.execute('SELECT payload FROM profiles WHERE s=? AND day=?', (s, day)).fetchone()
        if cached:
            value=json.loads(cached[0])
            return value if value['available_ms']<=self.last_ms else dict(passed=False,day=day,reason='PROFILE_NOT_YET_AVAILABLE')
        n, valid = self.db.execute('SELECT count(*),sum(valid) FROM minutes WHERE s=? AND t>=? AND t<?',
                                  (s, day, day+DAY)).fetchone()
        bad = self.db.execute('SELECT invalid FROM days WHERE s=? AND day=?', (s, day)).fetchone()
        if n != 1440 or valid != 1440 or (bad and bad[0]):
            return dict(passed=False, day=day, minute_count=n, validated_minutes=valid or 0,
                        reason='PREVIOUS_UTC_SESSION_INCOMPLETE_OR_INVALID')
        low, high = self.db.execute('SELECT min(CAST(p AS REAL)),max(CAST(p AS REAL)) FROM levels WHERE s=? AND day=?', (s, day)).fetchone()
        if low is None:
            return dict(passed=False, day=day, reason='EMPTY_SESSION')
        width = tick * max(1, math.ceil((high-low)/(100*tick)))
        base = math.floor(low/width); buckets = [0.] * 102
        digest = hashlib.sha256()
        for price, volume in self.db.execute('SELECT p,v FROM levels WHERE s=? AND day=? ORDER BY CAST(p AS REAL)', (s, day)):
            digest.update((price+':'+volume+'\n').encode())
            index = math.floor(float(price)/width)-base
            if not 0 <= index < len(buckets):
                raise ValueError('PROFILE_ROW_BOUND')
            buckets[index] += float(volume)
        total = sum(buckets); poc = max(range(len(buckets)), key=lambda i:(buckets[i], -i))
        a = b = poc; included = buckets[poc]
        while included < .7*total and (a > 0 or b < len(buckets)-1):
            left = buckets[a-1] if a > 0 else -1
            right = buckets[b+1] if b < len(buckets)-1 else -1
            if right >= left:
                b += 1; included += buckets[b]
            else:
                a -= 1; included += buckets[a]
        result = dict(passed=True, day=day, available_ms=self.last_ms, row_width=width,
            val=(base+a)*width, vah=(base+b+1)*width, poc=(base+poc+.5)*width,
            rows=buckets, first_row=base, input_sha256=digest.hexdigest(),
            validated_minutes=1440, volume='QUOTE_TURNOVER')
        self.db.execute('INSERT INTO profiles VALUES (?,?,?)', (s, day, canonical(result)))
        self._event('research_profile', self.last_ms, s, profile=result)
        return result

    def record(self, r):
        if self.halted:
            return
        with self.lock:
            try:
                with self.db:
                    ts = int(r['receive_ms']); self.last_ms = max(self.last_ms, ts)
                    s = r.get('symbol'); kind = r.get('kind')
                    if s not in self.pending:
                        return
                    if kind in ('footprint_quality_error', 'footprint_late_trade'):
                        self._invalidate_day(s, r.get('minute_ms', ts//MINUTE*MINUTE), kind)
                        return
                    if kind == 'candle_1m':
                        k = r['candle']; t = int(k['t'])
                        prices=[finite(k[x],True) for x in ('o','h','l','c')]
                        if (t % MINUTE or t + MINUTE > ts or finite(k['v']) < 0
                            or prices[2] > min(prices[0],prices[3]) or prices[1] < max(prices[0],prices[3]) or prices[1] < prices[2]):
                            self._invalidate_day(s, t, 'INVALID_OR_FUTURE_CANDLE'); return
                        item = self.pending[s].setdefault(t, {})
                        if 'candle' in item:
                            self._invalidate_day(s, t, 'DUPLICATE_CANDLE'); return
                        item['candle'] = dict(k, available_ms=ts)
                    elif kind == 'footprint_1m':
                        t = int(r['minute_ms']); item = self.pending[s].setdefault(t, {})
                        if 'footprint' in item:
                            self._invalidate_day(s, t, 'OVERLAPPING_FOOTPRINT'); return
                        item['footprint'] = r
                    self._drain(s, ts)
                    self._housekeeping(ts)
            except (ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as e:
                self.halted = type(e).__name__ + ': ' + str(e)
                # Capture remains available, but this state is conspicuous and
                # cannot manufacture new entries or a successful outcome.

    def _housekeeping(self, ts):
        day = ts // DAY * DAY
        if self.last_housekeeping_day == day:
            return
        self.last_housekeeping_day = day
        self.db.execute('DELETE FROM levels WHERE day<?', (day-DAY,))
        self.db.execute('DELETE FROM minutes WHERE t<?', (day-DAY,))
        self.db.execute('DELETE FROM days WHERE day<?', (day-DAY,))

    def _drain(self, s, ts):
        pending = self.pending[s]
        if not pending:
            return
        finalized = False
        for t in sorted(tuple(pending)):
            item = pending[t]; c = item.get('candle'); f = item.get('footprint')
            if not c or (not f and (Decimal(str(c['v'])) != 0 or ts < t+MINUTE+5000)):
                if ts > t+MINUTE+15_000:
                    self._invalidate_day(s, t, 'MISSING_CANDLE_OR_FOOTPRINT')
                    pending.pop(t); self.five[s] = None
                continue
            pending.pop(t)
            finalized = True
            valid = not f or (f.get('complete_observation') is True and not f.get('quality_flags'))
            levels = f.get('levels', []) if f else []
            volume = Decimal(str(c['v'])); observed = sum((Decimal(str(b))+Decimal(str(a)) for _, b, a, *_ in levels), Decimal(0))
            valid = valid and volume >= 0 and abs(volume-observed) <= max(Decimal('0.00000001'), volume*Decimal('0.000001'))
            if f:
                valid = valid and observed == Decimal(f['buy_volume'])+Decimal(f['sell_volume'])
            if self.db.execute('SELECT 1 FROM minutes WHERE s=? AND t=?', (s, t)).fetchone():
                self._invalidate_day(s, t, 'DUPLICATE_RECONCILED_MINUTE'); valid = False
            self.db.execute('INSERT OR IGNORE INTO minutes VALUES (?,?,?,?,?)', (s, t, str(volume), int(valid), '' if valid else 'VOLUME_OR_FOOTPRINT_QUALITY'))
            if valid:
                for price, buy, sell, *_ in levels:
                    turnover = Decimal(price) * (Decimal(buy)+Decimal(sell)); day = t//DAY*DAY
                    old = self.db.execute('SELECT v FROM levels WHERE s=? AND day=? AND p=?', (s, day, price)).fetchone()
                    value = turnover + (Decimal(old[0]) if old else Decimal(0))
                    self.db.execute('INSERT OR REPLACE INTO levels VALUES (?,?,?,?)', (s, day, price, str(value)))
            else:
                self._invalidate_day(s, t, 'VOLUME_OR_FOOTPRINT_QUALITY')
                self._censor(s, ts, 'INVALID_MINUTE_DURING_POSITION')
            self._aggregate(s, c, levels, valid, ts)
        # The feed cannot cause arbitrary historical map growth.
        if len(pending) > 3:
            self._invalidate_day(s, ts, 'PENDING_MINUTE_CAP')
            pending.clear(); self.five[s] = None
        if finalized and self.db.execute('SELECT count(*) FROM levels').fetchone()[0] > 200_000:
            raise RuntimeError('PROFILE_PRICE_LEVEL_CAP')

    def _aggregate(self, s, c, levels, valid, ts):
        bucket = c['t']//FIVE*FIVE; current = self.five[s]
        if not current or current['t'] != bucket:
            current = dict(t=bucket, bars=[], levels={}, valid=True)
            self.five[s] = current
        current['bars'].append(c); current['valid'] &= valid
        for price, buy, sell, *_ in levels:
            if price not in current['levels'] and len(current['levels']) >= 4096:
                current['valid'] = False; continue
            entry = current['levels'].setdefault(price, [Decimal(0), Decimal(0)])
            entry[0] += Decimal(buy); entry[1] += Decimal(sell)
        if c['t'] != bucket+4*MINUTE:
            return
        bars = current['bars']; self.five[s] = None
        if len(bars) != 5 or [k['t'] for k in bars] != list(range(bucket, bucket+FIVE, MINUTE)) or not current['valid']:
            self.m5[s].clear()
            if self.watches[s]:
                self._decision(s, ts, self.watches[s], ['INVALID_5M_COVERAGE'], 'DATA_INSUFFICIENT')
                self.watches[s] = None
            return
        bar = dict(t=bucket, o=bars[0]['o'], h=max(k['h'] for k in bars),
            l=min(k['l'] for k in bars), c=bars[-1]['c'], available_ms=ts,
            v=str(sum(Decimal(str(k['v'])) for k in bars)))
        exact = [[p, str(v[0]), str(v[1])] for p, v in current['levels'].items()]
        detail=self.watches[s] is not None or s in self.ready or (self.position and self.position['symbol']==s)
        self._event('research_bar_5m',ts,s,bar=bar,coverage='FIVE_RECONCILED_EXACT_MINUTES',
            footprint_sha256=hashlib.sha256(canonical(exact).encode()).hexdigest(),
            footprint_levels=exact if detail else None)
        self._closed_bar(s, bar, exact, ts)

    def _decision(self, s, ts, watch, blockers=None, state=None):
        gates = watch['gates']
        blockers = list(dict.fromkeys((blockers or []) + [name.upper() for name in LAYERS
                                     if not gates.get(name, {}).get('passed')]))
        self.blockers.update(blockers)
        return self._event('research_decision', ts, s, setup_id=watch['id'],
            state=state or ('PAPER_ENTRY_ELIGIBLE' if not blockers else 'PREDECLARED_SETUP_REJECTED'),
            blockers=blockers, gates=gates)

    def _watch(self, s, zone, quote, ts):
        side = zone['side']; bars = list(self.m5[s]); instrument = self.instruments.get(s)
        context = context_gates(self.h1[s], side, ts, (quote['bid']+quote['ask'])/2)
        hour_atr = atr(self.h1[s]); local_atr = atr(bars)
        ps = [p for p in pivots(bars[-28:]) if p['available_ms'] <= ts]
        reference = next((p for p in reversed(ps) if p['type'] == ('H' if side == 1 else 'L')), None)
        reference_unbroken = bool(reference) and not any(k['available_ms'] >= reference['available_ms']
            and side*(k['c']-reference['price']) > 0 for k in bars)
        liquidity = next((p for p in reversed(ps) if p['type'] == ('L' if side == 1 else 'H')), None)
        if liquidity and any(k['available_ms']>=liquidity['available_ms'] and
            (k['l']<liquidity['price'] if side==1 else k['h']>liquidity['price']) for k in bars):liquidity=None
        nearer = next((z for z in reversed(self.failed_zones[s]) if z['side'] == side
                       and z['failed_ms'] < ts and ts-z['failed_ms'] < DAY
                       and (z['low'] > zone['high'] if side == 1 else z['high'] < zone['low'])), None)
        profile = self._profile(s, ts//DAY*DAY-DAY, instrument['tick']) if instrument else dict(passed=False, reason='INSTRUMENT_NOT_AVAILABLE')
        level = profile.get('val' if side == 1 else 'vah')
        aligned = profile['passed'] and hour_atr and zone['low']-.25*hour_atr <= level <= zone['high']+.25*hour_atr
        gates = {k:dict(passed=False, reason='NOT_YET_OBSERVED') for k in LAYERS}
        gates.update(elliott=context['elliott'], smc_context=context['smc_context'],
            poi=dict(passed=True, zone=dict(zone), touch_ms=ts, quote=quote),
            profile=dict(profile, passed=bool(aligned), profile_valid=profile['passed'],
                         location_aligned=bool(aligned)))
        watch = dict(id=hashlib.sha256(f"{s}:{zone['id']}:{ts}".encode()).hexdigest()[:24],
            side=side, touch_ms=ts, expires_ms=min(ts+90*MINUTE, ts//DAY*DAY+DAY),
            zone=dict(zone), gates=gates, reference=reference, reference_unbroken=reference_unbroken, liquidity=liquidity,
            nearer=nearer, h1_atr=hour_atr, local_atr=local_atr,
            instrument=dict(instrument) if instrument else None,
            row_width=(instrument['tick']*max(1, math.ceil(.05*local_atr/instrument['tick']))) if instrument and local_atr else None,
            rejection=None, last_close=bars[-1]['c'] if bars else None)
        self.watches[s] = watch; zone['touched'] = True
        self._event('research_watch', ts, s, setup_id=watch['id'], frozen=watch,
                    price_context_5m=bars[-30:], source='OUR_TESTABLE_INTERPRETATION')
        early=[name.upper() for name in ('elliott','smc_context','profile') if not gates[name]['passed']]
        if not reference_unbroken:early.append('MISSING_UNBROKEN_PRETOUCH_STRUCTURE')
        if not liquidity or not nearer:early.append('MISSING_PREDECLARED_INDUCEMENT_CONTEXT')
        if not instrument or not hour_atr or not local_atr:early.append('MISSING_INSTRUMENT_OR_ATR')
        if early:
            self._decision(s,ts,watch,early,'CONTEXT_REJECTED_BEFORE_RAW_CAPTURE')
            self.watches[s]=None

    def _closed_bar(self, s, bar, levels, ts):
        previous = list(self.m5[s])
        if previous and bar['t'] != previous[-1]['t']+FIVE:
            self.m5[s].clear(); previous = []
            if self.watches[s]:
                self._decision(s, ts, self.watches[s], ['NONCONTIGUOUS_5M_BARS'], 'DATA_INSUFFICIENT')
                self.watches[s] = None
            self._censor(s, ts, 'NONCONTIGUOUS_5M_BARS')
        self.m5[s].append(bar)
        hour = self.hour_bars[s]
        if hour and bar['t'] != hour[-1]['t']+FIVE:
            hour.clear()
        hour.append(bar)
        if bar['t'] % HOUR == 55*MINUTE and len(hour) == 12 and hour[0]['t'] % HOUR == 0:
            k = dict(t=hour[0]['t'], o=hour[0]['o'], h=max(x['h'] for x in hour),
                l=min(x['l'] for x in hour), c=bar['c'], available_ms=ts)
            if not self.h1[s] or k['t'] > self.h1[s][-1]['t']:
                self.h1[s].append(k)
            hour.clear()
        # A closed invalidation is a failed POI, never a trade stop outcome.
        for z in tuple(self.zones[s]):
            if ts-z['available_ms'] > DAY:
                self.zones[s].remove(z)
            elif (bar['c'] < z['low'] if z['side'] == 1 else bar['c'] > z['high']):
                self.failed_zones[s].append(dict(z, failed_ms=ts))
                self.zones[s].remove(z)
        w = self.watches[s]
        if w:
            self._advance_watch(s, w, bar, levels, ts)
        self._prepare_add(s, bar, levels, ts)
        if len(previous) >= 2:
            old = previous[-2]
            candidates = []
            if bar['l'] > old['h'] and (bar['l']-old['h'])/old['h'] >= .0005:
                candidates.append(('FVG', 1, old['h'], bar['l']))
            if bar['h'] < old['l'] and (old['l']-bar['h'])/old['l'] >= .0005:
                candidates.append(('FVG', -1, bar['h'], old['l']))
            if len(previous) >= 20 and bar['h'] > bar['l'] and abs(bar['c']-bar['o'])/(bar['h']-bar['l']) >= .6:
                for side in (1, -1):
                    reference = max(k['h'] for k in previous[-20:]) if side == 1 else min(k['l'] for k in previous[-20:])
                    if side*(bar['c']-bar['o']) > 0 and side*(bar['c']-reference) > 0 and side*(previous[-1]['c']-reference) <= 0:
                        origin = next((k for k in reversed(previous[-10:]) if side*(k['c']-k['o']) < 0), None)
                        if origin:
                            candidates.append(('OB', side, origin['l'], origin['h']))
            for kind, side, low, high in candidates:
                zone = dict(id=f'{s}:{kind}:{side}:{bar["t"]}', kind=kind, side=side,
                            low=low, high=high, available_ms=ts, formed_ms=bar['t'], touched=False)
                self.zones[s].append(zone)

    def _advance_watch(self, s, w, bar, levels, ts):
        side = w['side']; z = w['zone']; rejection = w['rejection']
        reason = None
        if ts >= w['expires_ms']:
            reason = 'WATCH_EXPIRED'
        elif (bar['c'] < z['low'] if side == 1 else bar['c'] > z['high']):
            reason = 'POI_CLOSE_INVALIDATION_NOT_A_STOP'
        invalidation = w['gates']['elliott'].get('invalidation')
        if invalidation and side*(bar['c']-invalidation) <= 0:
            w['gates']['elliott']['passed'] = False
            reason = 'ELLIOTT_HYPOTHESIS_INVALIDATED'
        if reason:
            self._decision(s, ts, w, [reason]); self.watches[s] = None; return
        # Never use the extrema of a candle which began before zone arrival.
        if bar['t'] < w['touch_ms']:
            return
        liq = w['liquidity']
        if not rejection and liq and w['nearer'] and bar['h'] > bar['l']:
            sweep = bar['l'] < liq['price'] < bar['c'] if side == 1 else bar['h'] > liq['price'] > bar['c']
            close_half = (bar['c']-bar['l'])/(bar['h']-bar['l']) >= .5 if side == 1 else (bar['h']-bar['c'])/(bar['h']-bar['l']) >= .5
            if sweep and close_half:
                w['rejection'] = dict(bar=bar, extreme=bar['l' if side == 1 else 'h'])
                w['gates']['inducement'] = dict(passed=True, predeclared_liquidity=liq,
                    failed_nearer_poi=w['nearer'], sweep_reclaim=bar)
                self._event('research_reclaim', ts, s, setup_id=w['id'], evidence=w['gates']['inducement'])
        rejection = w['rejection']
        if rejection and bar['t'] > rejection['bar']['t']:
            if bar['t']-rejection['bar']['t'] > 3*FIVE:
                self._decision(s, ts, w, ['MSS_CONFIRMATION_EXPIRED']); self.watches[s] = None; return
            reference = w['reference']; ran = bar['h']-bar['l']
            tick = w['instrument']['tick'] if w['instrument'] else None
            directional = ran > 0 and side*(bar['c']-bar['o'])/ran >= .6
            outer = (bar['c']-bar['l'])/ran >= .75 if ran > 0 and side == 1 else (bar['h']-bar['c'])/ran >= .75 if ran > 0 else False
            crossed = w['reference_unbroken'] and reference and tick and w['last_close'] is not None and side*(w['last_close']-reference['price']) <= 0 and side*(bar['c']-reference['price']) >= tick
            if directional and outer and crossed:
                w['gates']['mss'] = dict(passed=True, reference=reference, displacement=bar)
                w['gates']['footprint'] = footprint_permission(levels, side, w['row_width'])
                w['gates']['footprint'].update(available_ms=ts, exact_levels=levels)
                blocked = []
                if not w['instrument'] or not w['h1_atr'] or not w['local_atr']:
                    blocked.append('MISSING_INSTRUMENT_OR_ATR')
                decision = self._decision(s, ts, w, blocked)
                if not decision['blockers']:
                    stop = (min(z['low'], rejection['extreme'])-.1*w['h1_atr'] if side == 1
                            else max(z['high'], rejection['extreme'])+.1*w['h1_atr'])
                    stop = (math.floor(stop/tick) if side == 1 else math.ceil(stop/tick))*tick
                    p = dict(campaign_id=w['id'], symbol=s, side=side, setup=w,
                        stop=stop, original_stop=stop, trigger_close=bar['c'], signal_ms=ts,
                        expires_ms=min(ts+FIVE, w['expires_ms']), profile_day=w['gates']['profile']['day'])
                    if self.position or self.ready:
                        self._event('paper_unfilled', ts, s, campaign_id=w['id'], reason='PORTFOLIO_BUSY')
                    else:
                        self.ready[s] = p
                    self._event('research_qualified', ts, s, campaign_id=w['id'], plan=p,
                                execution='WAIT_FOR_NEXT_ELIGIBLE_QUOTE')
                self.watches[s] = None
        w['last_close'] = bar['c']

    def _prepare_add(self, s, bar, levels, ts):
        p = self.position
        if not p or p['symbol'] != s or p['no_adds'] or p.get('pending_exit') or p.get('pending_add'):
            return
        if ts-p['entry_ms'] >= 60*MINUTE or p['legs'] >= 3 or bar['t'] <= p['last_fill_ms']:
            return
        rung = p['legs']; at = p['planned_prices'][rung]; width = p['setup']['row_width']
        if not bar['l'] <= at+width or not bar['h'] >= at-width or bar['h'] <= bar['l']:
            return
        side = p['side']; outer = (bar['c']-bar['l'])/(bar['h']-bar['l']) >= .75 if side == 1 else (bar['h']-bar['c'])/(bar['h']-bar['l']) >= .75
        flow = footprint_permission(levels, side, width)
        if outer and side*(bar['c']-bar['o']) > 0 and flow['passed']:
            p['pending_add'] = dict(at=at, signal_ms=ts, rung=rung, expires_ms=ts+FIVE,
                                    evidence=dict(bar=bar, footprint=flow, exact_levels=levels))
            self._event('paper_add_confirmation', ts, s, campaign_id=p['campaign_id'], confirmation=p['pending_add'])
            self._save_position()

    def quote(self, s, q, ts):
        if self.halted:
            return
        with self.lock:
            try:
                with self.db:
                    self.last_ms = max(self.last_ms, ts)
                    bid, ask = finite(q['bid'], True), finite(q['ask'], True)
                    age = ts-int(q['exchange_ms'])
                    previous = self.last_quote_ms.get(s)
                    if ask < bid or age < 0 or age > POLICY['quote_max_age_ms'] or (previous is not None and ts < previous):
                        self._censor(s, ts, 'INVALID_OR_STALE_EXECUTABLE_QUOTE'); return
                    if previous is not None and ts-previous > POLICY['quote_gap_ms']:
                        self._censor(s, ts, 'QUOTE_COVERAGE_GAP')
                        if self.watches[s]:
                            self._decision(s, ts, self.watches[s], ['QUOTE_COVERAGE_GAP'], 'DATA_INSUFFICIENT')
                            self.watches[s] = None
                    self.last_quote_ms[s] = ts; self.quotes[s] = dict(q)
                    self._drain(s, ts)
                    w=self.watches[s]
                    if w and w['gates']['elliott'].get('invalidation') is not None and w['side']*((bid+ask)/2-w['gates']['elliott']['invalidation'])<0:
                        w['gates']['elliott']['passed']=False
                        self._decision(s,ts,w,['ELLIOTT_HYPOTHESIS_INVALIDATED']);self.watches[s]=None
                    self._position_quote(s, q, ts)
                    if s in self.ready:
                        self._try_entry(s, q, ts)
                    if self.watches[s] is None and s not in self.ready and not (self.position and self.position['symbol'] == s):
                        mid = (bid+ask)/2
                        for z in reversed(self.zones[s]):
                            if not z['touched'] and z['available_ms'] < ts and z['low'] <= mid <= z['high']:
                                self._watch(s, z, q, ts); break
                    if self.watches[s] and ts >= self.watches[s]['expires_ms']:
                        self._decision(s, ts, self.watches[s], ['WATCH_EXPIRED'])
                        self.watches[s] = None
            except (ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as e:
                self.halted = type(e).__name__+': '+str(e)

    @staticmethod
    def _fill(q, side, entry=True):
        base = q['ask' if (side == 1) == entry else 'bid']
        return base*(1+(side if entry else -side)*POLICY['adverse_slippage_bps']/10000)

    @staticmethod
    def _size_ok(q, side, quantity, entry=True):
        key = 'ask_size' if (side == 1) == entry else 'bid_size'
        try:
            return finite(q[key]) >= quantity > 0
        except (KeyError, TypeError, ValueError):
            return False

    def _try_entry(self, s, q, ts):
        p = self.ready[s]; w = p['setup']; side = p['side']
        if ts < p['signal_ms']+POLICY['latency_ms']:
            return
        price = self._fill(q, side); z = w['zone']; reason = None
        if ts > p['expires_ms']:
            reason = 'ENTRY_EXPIRED'
        elif self.position:
            reason = 'PORTFOLIO_BUSY'
        elif (q['ask']-q['bid'])/((q['ask']+q['bid'])/2)*10000 > POLICY['spread_max_bps']:
            reason = 'SPREAD_TOO_WIDE'
        elif not z['low'] <= price <= z['high'] or side*(price-p['trigger_close']) > .25*w['local_atr']:
            reason = 'MISSED_NO_CHASE'
        elif side*(price-p['stop']) <= 0:
            reason = 'INVALID_STOP_GEOMETRY'
        elif side*(w['gates']['profile']['poc']-(price+side*1.5*abs(price-p['stop']))) <= 0:
            reason = 'INSUFFICIENT_ROOM_BEFORE_POC'
        if reason:
            self.ready.pop(s); self._event('paper_unfilled', ts, s, campaign_id=p['campaign_id'], reason=reason, quote=q); return
        middle = (z['low']+z['high'])/2; far = z['low' if side == 1 else 'high']
        prices = [price, middle+side*w['row_width'], far+side*w['row_width']]
        weights = POLICY['ladder_weights']; stop_fill = p['stop']*(1-side*.0005)
        cost = sum(v*(abs(e-stop_fill)+e*.0016+stop_fill*.0006) for v,e in zip(weights, prices))
        maximum = min(POLICY['risk_budget_usdt']/cost, 1000/sum(v*e for v,e in zip(weights, prices)))
        inst = w['instrument']; quantities = [math.floor(maximum*v/inst['step'])*inst['step'] for v in weights]
        quantity = quantities[0]
        if quantity < inst['minimum'] or price*quantity < inst['min_notional'] or not self._size_ok(q, side, quantity):
            self.ready.pop(s); self._event('paper_unfilled', ts, s, campaign_id=p['campaign_id'], reason='MINIMUM_OR_DISPLAYED_SIZE', quote=q, planned_quantity=quantity); return
        self.ready.pop(s)
        p.update(entry_ms=ts, last_fill_ms=ts, last_ms=ts, entry=price, average=price,
            quantity=quantity, remaining=quantity, planned_quantities=quantities,
            planned_prices=[price, middle, far], legs=1, no_adds=side*(price-middle) <= 0,
            gross=0., costs=price*quantity*.0016, mfe=0., mae=0., target1=price+side*1.5*abs(price-p['stop']),
            target2=price+side*3*abs(price-p['stop']), target1_done=False,
            pending_exit=None, pending_add=None, fills=[dict(at_ms=ts, price=price, quantity=quantity)],
            risk_budget=POLICY['risk_budget_usdt'])
        self.position = p; self._save_position()
        self._event('paper_entry', ts, s, campaign_id=p['campaign_id'], position=p, quote=q)

    def _position_quote(self, s, q, ts):
        p = self.position
        if not p or p['symbol'] != s:
            return
        side = p['side']; mark = q['bid' if side == 1 else 'ask']; p['last_ms'] = ts
        unrealized = (p['gross']+side*(mark-p['average'])*p['remaining'])/p['risk_budget']
        p['mfe'] = max(p['mfe'], unrealized); p['mae'] = min(p['mae'], unrealized)
        pending = p['pending_exit']
        if pending and ts >= pending['signal_ms']+POLICY['latency_ms']:
            self._exit(q, ts, pending['reason']); return
        if pending:
            return
        reason = None
        if side*(mark-p['stop']) <= 0:
            reason = 'STOP'
        elif ts-p['entry_ms'] >= POLICY['hold_hours']*HOUR:
            reason = 'TIME_EXPIRY'
        elif not p['target1_done'] and side*(mark-p['target1']) >= 0:
            reason = 'TARGET1'
        elif p['target1_done'] and side*(mark-p['target2']) >= 0:
            reason = 'TARGET2'
        if reason:
            p['pending_exit'] = dict(reason=reason, signal_ms=ts)
            p['pending_add'] = None; p['no_adds'] = True
            self._event('paper_exit_trigger', ts, s, campaign_id=p['campaign_id'], trigger=p['pending_exit'], quote=q)
            self._save_position(); return
        add = p.get('pending_add')
        if add and ts >= add['signal_ms']+POLICY['latency_ms']:
            p['pending_add'] = None
            price = self._fill(q, side); quantity = p['planned_quantities'][add['rung']]
            band = p['setup']['row_width']; stop_fill = p['original_stop']*(1-side*.0005)
            risk = sum(f['quantity']*(side*(f['price']-stop_fill)+f['price']*.0016+stop_fill*.0006) for f in p['fills'])
            risk += quantity*(side*(price-stop_fill)+price*.0016+stop_fill*.0006)
            inst = p['setup']['instrument']
            if (ts <= add['expires_ms'] and ts-p['entry_ms'] < 60*MINUTE
                and abs(price-add['at']) <= band and side*(p['average']-price) > 0
                and risk <= p['risk_budget']+1e-8 and quantity >= inst['minimum']
                and price*quantity >= inst['min_notional'] and self._size_ok(q, side, quantity)):
                p['average'] = (p['average']*p['quantity']+price*quantity)/(p['quantity']+quantity)
                p['quantity'] += quantity; p['remaining'] += quantity; p['legs'] += 1
                p['costs'] += price*quantity*.0016; p['last_fill_ms'] = ts
                p['fills'].append(dict(at_ms=ts, price=price, quantity=quantity))
                distance = abs(p['average']-p['stop'])
                p['target1'] = p['average']+side*1.5*distance; p['target2'] = p['average']+side*3*distance
                self._event('paper_add', ts, s, campaign_id=p['campaign_id'], price=price,
                    quantity=quantity, reserved_stop_loss=risk, position=p, quote=q)
            else:
                self._event('paper_add_missed', ts, s, campaign_id=p['campaign_id'], reason='BAND_COST_SIZE_OR_RISK_CHECK', quote=q)
            self._save_position()

    def _exit(self, q, ts, reason):
        p = self.position; side = p['side']
        quantity = p['remaining']*.5 if reason == 'TARGET1' else p['remaining']
        if not self._size_ok(q, side, quantity, False):
            self._censor(p['symbol'], ts, 'INSUFFICIENT_DISPLAYED_EXIT_SIZE'); return
        price = self._fill(q, side, False)
        p['gross'] += side*(price-p['average'])*quantity
        p['costs'] += price*quantity*.0006; p['remaining'] -= quantity
        p['pending_exit'] = None
        self._event('paper_exit_fill', ts, p['symbol'], campaign_id=p['campaign_id'],
                    reason=reason, price=price, quantity=quantity, quote=q)
        if reason == 'TARGET1':
            p['target1_done'] = True; p['stop'] = p['average']; p['no_adds'] = True
            self._save_position()
            # A gap through both targets can close the remaining leg only on a
            # later executable quote. No repeated use of the same L1 size.
            return
        gross = p['gross']/p['risk_budget']; cost = p['costs']/p['risk_budget']; net = gross-cost
        self.position = None; self._save_position()
        self._event('paper_closed', ts, p['symbol'], campaign_id=p['campaign_id'],
            reason=reason, entry_ms=p['entry_ms'], closed_ms=ts, profile_day=p['profile_day'],
            signal_ms=p['signal_ms'],touch_ms=p['setup'].get('touch_ms',p['signal_ms']),
            gross_R=gross, cost_R=cost, net_R=net, mfe_R=p['mfe'], mae_R=p['mae'],
            legs=p['legs'], fills=p['fills'], risk_budget=p['risk_budget'])
        self.net_R += net; self.gross_R += gross; self.cost_R += cost
        self.wins += net > 1e-12; self.losses += net < -1e-12; self.flat += abs(net) <= 1e-12
        self.equity_peak = max(self.equity_peak, self.net_R)
        self.max_drawdown_R = max(self.max_drawdown_R, self.equity_peak-self.net_R)

    def _censor(self, s, ts, reason):
        p = self.position
        if p and (s is None or p['symbol'] == s):
            self.position = None; self._save_position()
            self._event('paper_censored', ts, p['symbol'], campaign_id=p['campaign_id'],
                        reason=reason, last_observed_position=p, outcome='EXCLUDED_FROM_WIN_RATE')
        for symbol, pending in tuple(self.ready.items()):
            if s is None or symbol == s:
                self.ready.pop(symbol)
                self._event('paper_unfilled', ts, symbol, campaign_id=pending['campaign_id'], reason=reason)

    def snapshot(self, path):
        with self.lock:
            target = sqlite3.connect(path)
            try:
                self.db.backup(target, pages=64)
            finally:
                target.close()
            return self.status()

    def status(self):
        with self.lock:
            closed = self.wins+self.losses+self.flat
            day=self.last_ms//DAY*DAY
            progress={}
            for s in self.symbols:
                n,valid=self.db.execute('SELECT count(*),coalesce(sum(valid),0) FROM minutes WHERE s=? AND t>=? AND t<?',(s,day-DAY,day)).fetchone()
                current,current_valid=self.db.execute('SELECT count(*),coalesce(sum(valid),0) FROM minutes WHERE s=? AND t>=? AND t<?',(s,day,day+DAY)).fetchone()
                bad=self.db.execute('SELECT invalid FROM days WHERE s=? AND day=?',(s,day-DAY)).fetchone()
                progress[s]=dict(instrument_ready=s in self.instruments,h1_context_bars=len(self.h1[s]),
                    valid_5m_bars=len(self.m5[s]),prior_utc_day_ms=day-DAY,
                    prior_session_minutes=n,prior_valid_minutes=valid,
                    prior_session_ready=n==1440 and valid==1440 and not (bad and bad[0]),
                    current_session_minutes=current,current_valid_minutes=current_valid)
            return dict(mode='PAPER_RESEARCH', policy_id=POLICY['id'], policy_sha256=POLICY_HASH,
                state='HALTED' if self.halted else 'RUNNING', error=self.halted,
                automatic_exchange_trading=False, qualified_setups=self.counts['research_qualified'],
                evaluated_setups=self.counts['research_decision'], watched=self.counts['research_watch'],
                entered=self.counts['paper_entry'], additions=self.counts['paper_add'], closed=closed,
                open=int(self.position is not None), pending_entries=len(self.ready),
                censored=self.counts['paper_censored'], missed=self.counts['paper_unfilled'],
                revoked_outcomes=self.counts['paper_revoked'],
                wins=self.wins, losses=self.losses, flat=self.flat,
                win_rate_pct=100*self.wins/closed if closed else None,
                net_R=self.net_R, gross_R=self.gross_R, model_cost_R=self.cost_R,
                max_drawdown_R=self.max_drawdown_R, blockers=dict(self.blockers),
                active_watches=sum(w is not None for w in self.watches.values()),
                ledger_hash=self.previous_hash, ledger_events=sum(self.counts.values()),
                database_bytes=self.path.stat().st_size, database_limit_bytes=32*1024*1024,
                per_symbol=progress,
                open_position={k:self.position[k] for k in ('symbol','side','entry_ms','average','remaining','legs','stop','target1','target2','target1_done','mfe','mae')} if self.position else None,
                readiness='Measured stage counts; full prior UTC session and frozen seven-layer confirmation required.',
                outcome_model=POLICY['cost_disclosure'])
