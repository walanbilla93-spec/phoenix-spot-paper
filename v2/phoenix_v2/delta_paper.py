"""Independent long-only paper books using the worker's public evidence."""
from itertools import groupby
from . import delta
from .model import D, adverse_round, digest, floor, number, slip

CAPITAL = D(1000)
DECISION_WINDOW = 90
STOP_EXECUTION_POLICY = 'later_unambiguous_public_print_v1'
BOUNDED_FEED_GAPS = ('stream_not_present_at_interval_start', 'stream_disconnected',
                     'no_chronological_trades', 'trade_ohlcv_mismatch')


def new_state():
    return dict(version=delta.SPEC, rules_version=delta.RULES, markets={}, books={name: dict(cash=CAPITAL,
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


def invalidate(state, symbols, at, reason, log, gap_start=None, *, bounded_minute=None):
    for symbol in symbols:
        delta.invalidate(state['markets'].setdefault(symbol, delta.market()), reason, gap_start)
        for name, book in state['books'].items():
            if book['pending'] and book['pending']['symbol'] == symbol:
                book['pending'] = None
                note(book, log, name, at, symbol, 'pending_feed_gap')
            if book['position'] and book['position']['symbol'] == symbol:
                p = book['position']
                if bounded_minute and not book['unresolved'] and p['exit_mode'] is None:
                    low, high = bounded_minute['l'], bounded_minute['h']
                    if low > p['stop'] and high < p['target'] + p['rules']['tick'] and at + 60 <= p['deadline']:
                        event(book, log, name, at, 'protection_gap_bounded', reason=reason,
                              low=low, high=high, end=at + 60, candle_sha256=digest(bounded_minute),
                              disposition='NO_PROTECTIVE_TRIGGER_POSSIBLE')
                        continue
                quarantine(book, log, name, at, reason)


def position_snapshot(p):
    return dict(symbol=p['symbol'], side=1, entry=p['entry'], entry_at=p['opened'],
                original_qty=p['original_qty'], remaining_qty=p['qty'], sl=p['stop'],
                initial_sl=p['stop'], tp=p['target'], fees=p['fees'], funding=p['funding'],
                realized_gross=p['realized'], multiplier=p['rules']['multiplier'])


def event(book, log, name, at, kind, **fields):
    p = book['position']
    log(at, kind, arm=name, id=p['id'], symbol=p['symbol'], side=1,
        position_snapshot=position_snapshot(p), **fields)


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


def entry_terms(reference, structural_stop, atr, tick, fee):
    entry = adverse_round(reference + slip(reference, atr), tick, 1)
    # Keep structural invalidation for the setup, but give the held position
    # at least one closed 5m ATR from its simulated entry.
    stop = floor(min(structural_stop, entry - atr), tick)
    if stop <= 0:
        return entry, stop, D(0), D(0)
    stop_fill = adverse_round(stop - slip(stop, atr), tick, -1)
    return entry, stop, stop_fill, entry - stop_fill + fee * (entry + stop_fill)


def net_reward(entry, target, fee):
    return target - entry - fee * (target + entry)


def reversal_target(setup, reference, atr, rules):
    poc, value_high = setup['target'], setup.get('value_high')
    if value_high is None or value_high <= poc:
        return poc, 'point_of_control'
    tick, fee = rules['tick'], rules['taker']
    stop = floor(setup['stop'], tick)
    if not 0 < stop < reference:
        return poc, 'point_of_control'
    entry, _, stop_fill, loss = entry_terms(reference, stop, atr, tick, fee)
    required = D('1.5') * loss
    if (stop_fill > 0 and loss > 0 and net_reward(entry, floor(poc, tick), fee) < required
            <= net_reward(entry, floor(value_high, tick), fee)):
        return value_high, 'value_area_high'
    return poc, 'point_of_control'


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
    structural_stop = floor(intent['stop'], tick)
    if not 0 < structural_stop < reference:
        return 'invalid_stop_geometry'
    # Chase measures the market move. Slippage still applies to the actual
    # modeled fill, sizing and net reward calculation.
    if reference > intent['max_entry']:
        return 'entry_chase'
    entry, stop, stop_fill, unit_loss = entry_terms(reference, structural_stop, atr, tick, fee)
    if stop_fill <= 0:
        return 'invalid_stop_or_entry_chase'
    loss = unit_loss * m
    # D sets a deterministic 2R net target including one target tick of
    # penetration and limit execution at the rounded-up target price.
    target = (floor(intent['target'], tick) if intent['target'] is not None else
              adverse_round((entry * (1 + fee) + 2 * loss / m) / (1 - fee), tick, 1))
    reward = net_reward(entry, target, fee) * m
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
          decision_at=intent['decision'], planned_risk=qty * loss, rules_version=delta.RULES,
          atr_5m=atr, structural_stop=structural_stop, protection_stop=stop)
    return 'entry_filled'


def exit_on_trade(book, name, p, row, mode, log, **evidence):
    at = row['T'] / 1000
    rules = p['rules']
    qty = min(p['qty'], floor(number(row['v']) * D('.01'), rules['step']))
    if mode == 'stop' and p['exit_mode'] is None:
        p['stop_trigger_ms'] = row['T']
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
    provenance = (dict(execution_policy=STOP_EXECUTION_POLICY, stop_trigger_ms=p.get('stop_trigger_ms'),
                       last_deferred_ms=p['stop_deferred_ms'],
                       execution='SIMULATED_PUBLIC_PRINT_PROXY') if 'stop_deferred_ms' in p else {})
    event(book, log, name, at, 'exit_fill', price=price, qty=qty, fee=fee,
          residual=p['qty'], reason=mode, trade_id=row['i'], received_at=row['received_at'], **provenance, **evidence)
    if p['qty'] == 0:
        net = p['realized'] - p['fees'] + p['funding']
        book['streak'] = book['streak'] + 1 if net < 0 else 0 if net > 0 else book['streak']
        book['wins' if net > 0 else 'losses' if net < 0 else 'scratches'] += 1
        event(book, log, name, at, 'position_closed', net=net, exit_at=at, planned_risk=p['planned_risk'])
        book['position'] = None


def fresh(row):
    return 0 <= row['received_at'] - row['T'] / 1000 <= 2


def protection_fresh(p, rows):
    return all(fresh(row) and row['received_at'] >= p['opened_received_at']
               and row['T'] / 1000 > p['opened'] for row in rows)


def defer_market_exit(book, name, p, group, prices, log, mode):
    stamp = group[0]['T']
    first = p['exit_mode'] is None
    if first:
        p['exit_mode'] = mode
        if mode == 'stop':
            p['stop_trigger_ms'] = stamp
    p['stop_deferred_ms' if mode == 'stop' else 'time_deferred_ms'] = stamp
    kind = ('stop_trigger_confirmed' if first else 'stop_execution_deferred') if mode == 'stop' else 'time_execution_deferred'
    event(book, log, name, stamp / 1000, kind, reason=mode,
          execution_policy=STOP_EXECUTION_POLICY, stop_trigger_ms=p.get('stop_trigger_ms'),
          print_prices=sorted(prices), group_sha256=digest(sorted(group, key=lambda row: row['i'])),
          disposition='NO_SIMULATED_FILL_SAME_MILLISECOND',
          filled_qty=D(0))


def defer(book, log, name, at, pending, reason):
    seen = pending.setdefault('deferred', [])
    if reason not in seen:
        seen.append(reason)
        note(book, log, name, at, pending['symbol'], reason, id=pending['id'], disposition='DEFERRED')


def execute(book, name, symbol, rows, end, policy, log):
    """Use a conservative size for equal-price prints, without guessing order."""
    for stamp, group in groupby(rows, key=lambda row: row['T']):
        at = stamp / 1000
        p, pending = book['position'], book['pending']
        if book['unresolved'] or not (p or pending):
            break
        group = list(group)
        if p:
            if stamp <= max(p.get('stop_deferred_ms', -1), p.get('time_deferred_ms', -1)):
                continue
            prices = {number(row['p']) for row in group}
            modes = []
            for row in group:
                price = number(row['p'])
                mode = (p['exit_mode'] or ('stop' if price <= p['stop'] else
                        'time_exit' if at >= p['deadline'] else
                        'target' if price >= p['target'] + p['rules']['tick'] else None))
                if mode:
                    modes.append((row, mode))
            if modes:
                if not protection_fresh(p, group):
                    quarantine(book, log, name, at, 'ambiguous_or_stale_protection_print')
                    break
                evidence = {}
                if len(prices) > 1:
                    if p['exit_mode'] == 'stop' or (p['exit_mode'] is None and min(prices) <= p['stop']):
                        defer_market_exit(book, name, p, group, prices, log, 'stop')
                        continue
                    if modes[0][1] == 'time_exit':
                        defer_market_exit(book, name, p, group, prices, log, 'time_exit')
                        continue
                    # Each eligible print supports the same fixed limit price.
                    # Per-print size caps commute, regardless of exchange order.
                    evidence = dict(target_tie_policy='fixed_price_per_print_cap_v1',
                                    group_sha256=digest(sorted(group, key=lambda row: row['i'])))
                for row, mode in sorted(modes, key=lambda pair: pair[0]['i']):
                    exit_on_trade(book, name, p, row, mode, log, **evidence)
                    if not book['position'] or book['unresolved']:
                        break
        elif pending.get('boundary', pending['decision']) <= at < pending['expires']:
            prices = [number(row['p']) for row in group]
            trigger = pending['trigger']
            target = pending['target']
            cancel = ('setup_invalidated_before_fill' if min(prices) <= pending['stop']
                      else 'target_reached_before_fill' if target is not None and max(prices) >= target
                      else None)
            if cancel:
                book['pending'] = None
                note(book, log, name, at, symbol, cancel, id=pending['id'])
                continue
            if at <= pending['decision'] or (trigger is not None and max(prices) < trigger):
                continue
            if min(prices) != max(prices):
                defer(book, log, name, at, pending, 'ambiguous_entry_print')
                continue
            if not all(fresh(row) for row in group):
                defer(book, log, name, at, pending, 'stale_entry_print')
                continue
            row = min(group, key=lambda row: (number(row['v']), row['i']))
            result = fill(book, name, pending, row, policy, log)
            if result in ('entry_chase', 'net_reward_below_1_5r', 'public_size_or_venue_minimum'):
                defer(book, log, name, at, pending, result)
            else:
                book['pending'] = None
                note(book, log, name, at, symbol, result, id=pending['id'])
    pending = book['pending']
    if pending and end >= pending['expires']:
        book['pending'] = None
        note(book, log, name, end, symbol, 'intent_expired', id=pending['id'],
             deferred_reasons=pending.get('deferred', []))


def settle(book, name, at, mark, settlement, log):
    p = book['position']
    p['mark'] = mark
    interval = p['rules']['funding_minutes'] * 60
    if interval and at % interval == 0:
        if book['unresolved']:
            event(book, log, name, at, 'funding_unbooked',
                  reason='position_quantity_unverified', evidence=digest(settlement) if settlement else None)
        elif settlement is None:
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
        feed_quality = quality[symbol]
        reason = feed_quality.get('delta_reconciliation', feed_quality['reconciliation'])['reason']
        if not reason and not rules.get('tick'):
            reason = 'contract_tick_unavailable'
        tick = number(rules['tick']) if not reason else None
        if not reason and tick <= 0:
            reason = 'contract_tick_unavailable'
        if m['last'] not in (None, end - 60):
            invalidate(state, [symbol], end - 60, 'noncontiguous_minutes', log, m['last'])
            m['last'] = None
        confirmed_close = quality[symbol]['expected']['c'] if len(stream) > 2 and stream[2] and not reason else None
        availability, evaluations = delta.ingest(m, symbol, end, rows, reason, tick, observed, confirmed_close)
        invalid = m['reason'] is not None and m['reason'] not in delta.FOOTPRINT_PAUSES
        if invalid:
            bounds = None
            if m['reason'] in BOUNDED_FEED_GAPS and feed_quality.get('delta_confirmed_bounds'):
                expected = feed_quality['expected']
                if all(expected['l'] <= number(row['p']) <= expected['h'] for row in rows):
                    bounds = expected
            invalidate(state, [symbol], end - 60, m['reason'], log, end - 60, bounded_minute=bounds)
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
                if not invalid:
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
                elif decision < end or decision >= end + DECISION_WINDOW:
                    reason = 'decision_outside_fresh_window'
                elif locked(book):
                    reason = 'risk_locked'
                elif book['position'] or book['pending']:
                    reason = 'account_occupied'
                elif atr is None or atr <= 0:
                    reason = 'execution_volatility_unavailable'
                else:
                    close = quality[symbol]['observed']['c']
                    try:
                        rules = policy.rules(symbol, decision)
                    except (ValueError, KeyError):
                        reason = 'contract_or_liquidity_unavailable'
                    else:
                        target, basis = (reversal_target(setup, close, atr, rules) if name == 'C'
                                         else (None, 'net_2r'))
                        max_entry = (setup['trigger'] or close) + max(tick, atr / 4)
                        intent = dict(setup, id=f'{name}:{symbol}:delta:{end}', symbol=symbol, tick=tick,
                                      boundary=end, decision=decision, expires=end + delta.WINDOW,
                                      atr=atr, max_entry=max_entry, target=target, target_basis=basis)
                        book['pending'] = intent
                        log(decision, 'delta_signal', arm=name, symbol=symbol, id=intent['id'],
                            signal=dict(intent), version=delta.RULES, boundary=end)
                        reason = 'intent_armed'
            note(book, log, name, end, symbol, reason, data_reason=availability, setup=setup)


def isolated_liabilities(book):
    primary = book.get('isolated_liability')
    return [primary, *book.get('additional_isolated_liabilities', [])] if primary else []


def reports(state, financials, symbols, entries_enabled=True):
    if not state:
        return {}
    result = {}
    for name, book in state['books'].items():
        p = book['position']
        available_equity = None if book['unresolved'] else equity(book)
        isolated = isolated_liabilities(book)
        reserve = sum(item['reserved_cash'] for item in isolated)
        total_cash = book['cash'] + reserve
        eq = None if isolated else available_equity
        positions = [dict(p, signal=dict(side=1, symbol=p['symbol']), initial_stop=p['stop'])] if p else []
        markets = {s: state['markets'].get(s) or delta.market() for s in symbols}
        data = {s: dict(status='DATA UNAVAILABLE' if m['reason'] else 'CURRENT', reason=m['reason'],
                        profile_start=m['reference']['start'] if m['reference'] else None)
                for s, m in markets.items()}
        result[name] = dict(strategy=delta.NAMES[name], version=state['version'], cash=total_cash,
            rules_version=state.get('rules_version'),
            entries_enabled=entries_enabled,
            equity=eq, unrealized_pnl=eq - book['cash'] if eq is not None else None,
            total_pnl=eq - CAPITAL if eq is not None else None, cash_pnl=total_cash - CAPITAL,
            marks={p['symbol']: p['mark']} if p else {}, positions=positions,
            orders=[dict(book['pending'])] if book['pending'] else [],
            data_valid=not book['unresolved'] and any(v['status'] == 'CURRENT' for v in data.values()),
            unresolved=book['unresolved'], locked=locked(book), day_locked=book['day_locked'], streak=book['streak'], virtual_capital=CAPITAL,
            markets=data, decisions=dict(book['counts']), last_decisions=dict(book['last']),
            wins=book['wins'], losses=book['losses'], scratches=book['scratches'],
            financials=financials.get(name, {}))
        if isolated:
            result[name].update(isolated_liability=isolated[0], isolated_liabilities=isolated, available_cash=book['cash'],
                                available_equity=available_equity)
    return result
