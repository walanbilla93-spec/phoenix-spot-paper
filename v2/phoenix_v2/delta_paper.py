"""Independent long-only paper books using the worker's public evidence."""
from itertools import groupby
from . import delta
from .model import D, adverse_round, digest, floor, number, slip

CAPITAL = D(1000)


def new_state():
    return dict(version=delta.SPEC, markets={}, books={name: dict(cash=CAPITAL,
        day=None, day_start=CAPITAL, day_locked=False, streak=0, pending=None,
        position=None, unresolved=None, counts={}, last={}, wins=0, losses=0, scratches=0)
        for name in delta.NAMES})


def note(book, log, name, at, symbol, reason, **fields):
    counts = book['counts']
    counts[reason] = counts.get(reason, 0) + 1
    book['last'][symbol] = dict(at=at, reason=reason)
    log(at, 'delta_evaluation', arm=name, symbol=symbol, reason=reason, **fields)


def quarantine(book, log, name, at, reason):
    p = book['position']
    if p and not book['unresolved']:
        book['unresolved'] = reason
        event(book, log, name, at, 'liability_quarantined', reason=reason)


def invalidate(state, symbols, at, reason, log):
    for symbol in symbols:
        delta.invalidate(state['markets'].setdefault(symbol, delta.market()), reason)
        for name, book in state['books'].items():
            if book['pending'] and book['pending']['symbol'] == symbol:
                book['pending'] = None
                note(book, log, name, at, symbol, 'pending_feed_gap')
            if book['position'] and book['position']['symbol'] == symbol:
                quarantine(book, log, name, at, reason)


def event(book, log, name, at, kind, **fields):
    p = book['position']
    snap = dict(symbol=p['symbol'], side=1, entry=p['entry'], entry_at=p['opened'],
                original_qty=p['original_qty'], remaining_qty=p['qty'], sl=p['stop'],
                initial_sl=p['stop'], tp=p['target'], fees=p['fees'], funding=p['funding'],
                realized_gross=p['realized'], multiplier=p['rules']['multiplier'])
    log(at, kind, arm=name, id=p['id'], symbol=p['symbol'], side=1, position_snapshot=snap, **fields)


def equity(book):
    p = book['position']
    return book['cash'] if p is None else book['cash'] + (p['mark'] - p['entry']) * p['qty'] * p['rules']['multiplier']


def risk(book, at):
    day = int(at) // 86400
    eq = equity(book)
    if book['day'] != day:
        book.update(day=day, day_start=eq, day_locked=False)
    if eq <= 0 or eq <= book['day_start'] * D('.98'):
        book['day_locked'] = True
    return eq


def locked(book):
    return book['day_locked'] or book['streak'] >= 5 or bool(book['unresolved'])


def fill(book, name, intent, row, policy, log):
    at, symbol = row['T'] / 1000, intent['symbol']
    eq = risk(book, at)
    if locked(book):
        return 'risk_locked'
    try:
        rules = policy.rules(symbol, at)
    except (ValueError, KeyError):
        return 'contract_or_liquidity_unavailable'
    tick, m, fee = (rules[k] for k in ('tick', 'multiplier', 'taker'))
    if tick != intent['tick']:
        return 'tick_changed_since_decision'
    reference = number(row['p'])
    atr = intent['atr']
    entry = adverse_round(reference + slip(reference, atr), tick, 1)
    stop = floor(intent['stop'], tick)
    if not 0 < stop < reference or entry > intent['max_entry']:
        return 'invalid_stop_or_entry_chase'
    stop_fill = adverse_round(stop - slip(stop, atr), tick, -1)
    if stop_fill <= 0:
        return 'invalid_stop_or_entry_chase'
    loss = (entry - stop_fill + fee * (entry + stop_fill)) * m
    # D sets a deterministic 2R net target including one target tick of
    # penetration and limit execution at the rounded-up target price.
    target = (floor(intent['target'], tick) if intent['target'] is not None else
              adverse_round((entry * (1 + fee) + 2 * loss / m) / (1 - fee), tick, 1))
    reward = (target - entry - fee * (target + entry)) * m
    if reward < D('1.5') * loss:
        return 'net_reward_below_1_5r'
    budget = min(eq * D('.005'), eq - book['day_start'] * D('.98'))
    qty = floor(min(budget / loss, eq / (entry * m * (1 + fee)),
                    number(row['v']) * D('.01')), rules['step'])
    if qty <= 0 or qty < rules['minimum_qty'] or qty * entry * m < rules['minimum_notional']:
        return 'public_size_or_venue_minimum'
    charged = qty * entry * m * fee
    book['cash'] -= charged
    book['position'] = dict(id=intent['id'], symbol=symbol, entry=entry, opened=at,
        opened_received_at=row['received_at'],
        qty=qty, original_qty=qty, stop=stop, target=target, rules=rules, atr=atr,
        mark=reference, fees=charged, funding=D(0), realized=D(0), exit_mode=None,
        deadline=at + 7200, planned_risk=qty * loss)
    event(book, log, name, at, 'entry_fill', price=entry, qty=qty, fee=charged,
          source='BYBIT_PUBLIC_TRADE', trade_id=row['i'], received_at=row['received_at'],
          decision_at=intent['decision'], planned_risk=qty * loss)
    return 'entry_filled'


def exit_on_trade(book, name, p, row, mode, log):
    at = row['T'] / 1000
    rules = p['rules']
    qty = min(p['qty'], floor(number(row['v']) * D('.01'), rules['step']))
    if mode != 'target':
        p['exit_mode'] = mode
    if qty <= 0:
        return
    reference = number(row['p'])
    price = p['target'] if mode == 'target' else adverse_round(reference - slip(reference, p['atr']), rules['tick'], -1)
    if price <= 0:
        quarantine(book, log, name, at, 'nonpositive_simulated_exit')
        return
    gross = (price - p['entry']) * qty * rules['multiplier']
    fee = price * qty * rules['multiplier'] * rules['taker']
    p['realized'] += gross
    p['fees'] += fee
    p['qty'] -= qty
    book['cash'] += gross - fee
    event(book, log, name, at, 'exit_fill', price=price, qty=qty, fee=fee,
          residual=p['qty'], reason=mode, trade_id=row['i'], received_at=row['received_at'])
    if p['qty'] == 0:
        net = p['realized'] - p['fees'] + p['funding']
        book['streak'] = book['streak'] + 1 if net < 0 else 0 if net > 0 else book['streak']
        book['wins' if net > 0 else 'losses' if net < 0 else 'scratches'] += 1
        event(book, log, name, at, 'position_closed', net=net, exit_at=at, planned_risk=p['planned_risk'])
        book['position'] = None


def fresh(row):
    return 0 <= row['received_at'] - row['T'] / 1000 <= 2


def execute(book, name, symbol, rows, end, policy, log):
    """A same-ms group is atomic. Execution requires a unique actionable print."""
    for stamp, group in groupby(rows, key=lambda row: row['T']):
        at = stamp / 1000
        p, pending = book['position'], book['pending']
        if book['unresolved'] or not (p or pending):
            break
        group = list(group)
        if p:
            modes = []
            for row in group:
                price = number(row['p'])
                mode = (p['exit_mode'] or ('stop' if price <= p['stop'] else
                        'time_exit' if at >= p['deadline'] else
                        'target' if price >= p['target'] + p['rules']['tick'] else None))
                if mode:
                    modes.append((row, mode))
            if modes:
                if (len({number(row['p']) for row in group}) != 1 or
                        not all(fresh(row) and row['received_at'] >= p['opened_received_at'] for row in group)):
                    quarantine(book, log, name, at, 'ambiguous_or_stale_protection_print')
                    break
                for row, mode in sorted(modes, key=lambda pair: pair[0]['i']):
                    exit_on_trade(book, name, p, row, mode, log)
                    if not book['position'] or book['unresolved']:
                        break
        elif pending['decision'] < at < pending['expires']:
            prices = [number(row['p']) for row in group]
            trigger = pending['trigger']
            if min(prices) <= pending['stop'] or trigger is None or max(prices) >= trigger:
                book['pending'] = None
                if len(group) != 1:
                    result = 'ambiguous_entry_print'
                elif not fresh(group[0]):
                    result = 'stale_entry_print'
                elif prices[0] <= pending['stop']:
                    result = 'setup_invalidated_before_fill'
                else:
                    result = fill(book, name, pending, group[0], policy, log)
                note(book, log, name, at, symbol, result, id=pending['id'])
    pending = book['pending']
    if pending and end >= pending['expires']:
        book['pending'] = None
        note(book, log, name, end, symbol, 'intent_expired', id=pending['id'])


def settle(book, name, at, mark, settlement, log):
    p = book['position']
    p['mark'] = mark
    interval = p['rules']['funding_minutes'] * 60
    if interval and at % interval == 0:
        if settlement is None:
            quarantine(book, log, name, at, 'funding_unavailable')
            event(book, log, name, at, 'funding_missing', reason='funding_unavailable')
        else:
            flow = -p['qty'] * mark * p['rules']['multiplier'] * number(settlement['fundingRate'])
            p['funding'] += flow
            book['cash'] += flow
            event(book, log, name, at, 'funding', cashflow=flow, evidence=digest(settlement))


def minute(state, end, streams, quality, marks, settlements, policy, contracts, atrs, log, clock, entries_enabled):
    books = state['books']
    observed = clock()
    data = {}
    for symbol, stream in streams.items():
        rules = contracts.get(symbol, [{}])[0]
        m = state['markets'].setdefault(symbol, delta.market())
        rows = stream[0]
        # Use the underlying public-feed reconciliation. A/B's strategy-specific
        # price-order gates must never affect C/D; C/D has its own tie checks.
        reason = quality[symbol]['reconciliation']['reason']
        if not reason and not rules.get('tick'):
            reason = 'contract_tick_unavailable'
        tick = number(rules['tick']) if not reason else None
        if not reason and tick <= 0:
            reason = 'contract_tick_unavailable'
        if m['last'] not in (None, end - 60):
            invalidate(state, [symbol], end - 60, 'noncontiguous_minutes', log)
            m['last'] = None
        availability, evaluations = delta.ingest(m, symbol, end, rows, reason, tick, observed)
        if m['reason'] not in (None, 'awaiting_complete_utc_hour', 'ambiguous_footprint_close',
                               'insufficient_footprint_prints'):
            invalidate(state, [symbol], end - 60, m['reason'], log)
        data[symbol] = availability, evaluations, tick
        for name, book in books.items():
            p, pending = book['position'], book['pending']
            active = p or pending
            if active and active['symbol'] == symbol:
                if p:
                    p['mark'] = marks[symbol].o
                risk(book, end - 60)
                if pending and (locked(book) or not entries_enabled):
                    book['pending'] = None
                    note(book, log, name, end - 60, symbol,
                         'risk_locked' if entries_enabled else 'new_entries_paused')
                execute(book, name, symbol, rows, end, policy, log)
                if book['position']:
                    settle(book, name, end, marks[symbol].c, settlements.get(symbol), log)
                # Use the final mark/funding for the following UTC risk day.
                risk(book, end - .001)
                risk(book, end)
    if end % delta.WINDOW:
        return
    for symbol in sorted(data):
        availability, evaluations, tick = data[symbol]
        for name, book in books.items():
            risk(book, end)
            reason, setup = (evaluations[name] if evaluations is not None else ('DATA UNAVAILABLE', None))
            if setup:
                decision = clock()
                atr = atrs.get(symbol)
                if not entries_enabled:
                    reason = 'new_entries_paused'
                elif decision < end or decision >= end + 60:
                    reason = 'decision_outside_fresh_window'
                elif locked(book):
                    reason = 'risk_locked'
                elif book['position'] or book['pending']:
                    reason = 'account_occupied'
                elif atr is None or atr <= 0:
                    reason = 'execution_volatility_unavailable'
                else:
                    close = quality[symbol]['observed']['c']
                    max_entry = ((setup['trigger'] or close) + max(tick, atr / 4))
                    intent = dict(setup, id=f'{name}:{symbol}:delta:{end}', symbol=symbol, tick=tick,
                                  decision=decision, expires=end + (60 if name == 'C' else 300),
                                  atr=atr, max_entry=max_entry)
                    book['pending'] = intent
                    log(decision, 'delta_signal', arm=name, symbol=symbol, id=intent['id'],
                        signal=intent, version=delta.SPEC, boundary=end)
                    reason = 'intent_armed'
            note(book, log, name, end, symbol, reason, data_reason=availability, setup=setup)


def reports(state, financials, symbols, entries_enabled=True):
    if not state:
        return {}
    result = {}
    for name, book in state['books'].items():
        p = book['position']
        eq = None if book['unresolved'] else equity(book)
        positions = [dict(p, signal=dict(side=1, symbol=p['symbol']), initial_stop=p['stop'])] if p else []
        markets = {s: state['markets'].get(s) or delta.market() for s in symbols}
        data = {s: dict(status='DATA UNAVAILABLE' if m['reason'] else 'CURRENT', reason=m['reason'],
                        profile_start=m['reference']['start'] if m['reference'] else None)
                for s, m in markets.items()}
        result[name] = dict(strategy=delta.NAMES[name], version=state['version'], cash=book['cash'],
            entries_enabled=entries_enabled,
            equity=eq, unrealized_pnl=eq - book['cash'] if eq is not None else None,
            total_pnl=eq - CAPITAL if eq is not None else None, cash_pnl=book['cash'] - CAPITAL,
            marks={p['symbol']: p['mark']} if p else {}, positions=positions,
            orders=[dict(book['pending'])] if book['pending'] else [],
            data_valid=not book['unresolved'] and any(v['status'] == 'CURRENT' for v in data.values()),
            unresolved=book['unresolved'], locked=locked(book), streak=book['streak'], virtual_capital=CAPITAL,
            markets=data, decisions=dict(book['counts']), last_decisions=dict(book['last']),
            wins=book['wins'], losses=book['losses'], scratches=book['scratches'],
            financials=financials.get(name, {}))
    return result
