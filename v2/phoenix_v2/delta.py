"""Causal profiles and setups from reconciled Bybit linear public trades."""
from .model import D, ceil, number

HOUR = 3600
WINDOW = 300
MAX_BINS = 512
SPEC = 'bybit-linear-delta-1'
NAMES = {'C': 'Delta Value Area reversal', 'D': 'Delta trapped-seller breakout'}


def market():
    return dict(hour=None, reference=None, window=None, recent=[], last=None,
                reason='awaiting_complete_utc_hour')


def invalidate(state, reason):
    state.update(hour=None, reference=None, window=None, recent=[], reason=reason)


def new_bucket(start, width):
    return dict(start=start, width=width, levels={}, count=0, low=None, high=None, close=None)


def add_trades(bucket, trades):
    width = bucket['width']
    levels = bucket['levels']
    for price, size, side in trades:
        index = int(price // width)
        volume = levels.setdefault(index, [D(0), D(0), 0])
        volume[side] += size
        volume[2] += 1
        bucket['low'] = price if bucket['low'] is None else min(bucket['low'], price)
        bucket['high'] = price if bucket['high'] is None else max(bucket['high'], price)
    bucket['count'] += len(trades)
    bucket['close'] = trades[-1][0]
    return len(levels) <= MAX_BINS and max(levels) - min(levels) < MAX_BINS


def profile(bucket):
    levels = bucket['levels']
    total = sum(buy + sell for buy, sell, _ in levels.values())
    poc = min(levels, key=lambda k: (-sum(levels[k][:2]), k))
    low = high = poc
    covered = sum(levels[poc][:2])
    first, last = min(levels), max(levels)

    def volume(index):
        return sum(levels.get(index, (0, 0))[:2])

    while covered < D('.7') * total:
        if low > first and (high == last or volume(low - 1) >= volume(high + 1)):
            low -= 1
            covered += volume(low)
        else:
            high += 1
            covered += volume(high)
    width = bucket['width']
    return dict(start=bucket['start'], width=width, low=low, high=high,
                poc=poc, volume=total, count=bucket['count'], levels=levels)


def seller_levels(bucket, total):
    # Minimum 3 prints and net selling >=40% of that bin's volume and
    # >=1% of the full period's volume, in native contract units.
    return sorted(((i, sell - buy) for i, (buy, sell, n) in bucket['levels'].items()
                   if n >= 3 and sell - buy >= max((buy + sell) * D('.4'), total * D('.01'))),
                  key=lambda level: (-level[1], level[0]))


def evaluate(reference, bar, recent, tick):
    width = reference['width']
    buffer = max(tick, width / 4)
    poc = (reference['poc'] + D('.5')) * width
    low, high, close = (bar[k] for k in ('low', 'high', 'close'))
    result = {'C': ('no_significant_sell_level_below_value', None),
              'D': ('needs_three_prior_footprints', None)}
    outside = [(i, imbalance) for i, imbalance in seller_levels(reference, reference['volume'])
               if i < reference['low']]
    if outside:
        i, imbalance = outside[0]
        lower, upper = i * width, (i + 1) * width
        result['C'] = ('no_rejection_reclaim', None)
        if recent and recent[-1]['close'] >= upper and lower - width <= low < upper <= close < poc:
            result['C'] = ('qualified', dict(stop=low - buffer, target=poc, trigger=None,
                                            level=lower, imbalance=imbalance))
    if len(recent) == 3:
        resistance = max(b['high'] for b in recent)
        total = sum(buy + sell for buy, sell, _ in bar['levels'].values())
        sellers = [(i, imbalance) for i, imbalance in seller_levels(bar, total)
                   if resistance - width <= (i + 1) * width and i * width <= resistance + width
                   and i * width >= low + (high - low) / 2 and close >= i * width]
        result['D'] = ('no_absorption_near_resistance', None)
        if (sellers and recent[-1]['delta_fraction'] >= D('.2') and high >= resistance - width
                and high > low and close >= low + (high - low) * D('.75')):
            i, imbalance = sellers[0]
            result['D'] = ('qualified', dict(stop=low - buffer, target=None,
                trigger=ceil(max(resistance, high, (i + 1) * width), tick) + tick,
                level=i * width, imbalance=imbalance))
    return result


def ingest(state, symbol, end, rows, reason, tick, decision):
    """Return a setup evaluation only at a completed five-minute boundary."""
    start = end - 60
    if state['last'] not in (None, start):
        invalidate(state, 'noncontiguous_minutes')
    state['last'] = end
    if not reason:
        reason = validate_rows(symbol, start, rows, decision)
    if reason:
        invalidate(state, reason)
        return reason, None
    if start % HOUR == 0:
        first = min(number(row['p']) for row in rows if row['T'] == rows[0]['T'])
        width = max(tick, ceil(first * D('.001'), tick))
        state['hour'] = new_bucket(start, width)
    hour, reference = state['hour'], state['reference']
    trades = [(number(r['p']), number(r['v']), int(r['S'] == 'Sell')) for r in rows]
    if start % WINDOW == 0 and reference:
        state['window'] = new_bucket(start, reference['width'])
    window = state['window']
    for bucket in (hour, window):
        if bucket and not add_trades(bucket, trades):
            invalidate(state, 'profile_bin_capacity')
            return state['reason'], None
    evaluation = None
    footprint_reason = None
    if window and end % WINDOW == 0:
        if ambiguous_edge(rows, rows[-1]['T']):
            state['recent'] = []
            footprint_reason = 'ambiguous_footprint_close'
        elif window['count'] >= 30:
            evaluation = evaluate(reference, window, state['recent'], tick)
            buy = sum(level[0] for level in window['levels'].values())
            sell = sum(level[1] for level in window['levels'].values())
            prior = {k: window[k] for k in ('high', 'close')}
            prior['delta_fraction'] = (buy - sell) / (buy + sell)
            state['recent'] = (state['recent'] + [prior])[-3:]
        else:
            state['recent'] = []
            footprint_reason = 'insufficient_footprint_prints'
        state['window'] = None
    if hour and end % HOUR == 0:
        state['reference'] = profile(hour) if hour['count'] >= 100 else None
        state['hour'] = None
        state['recent'] = []
    state['reason'] = footprint_reason or (None if state['reference'] else 'awaiting_complete_utc_hour')
    return (footprint_reason or ('awaiting_complete_utc_hour' if not reference else None)), evaluation


def ambiguous_edge(rows, timestamp):
    return len({number(r['p']) for r in rows if r['T'] == timestamp}) > 1


def validate_rows(symbol, start, rows, decision):
    if not rows:
        return 'no_public_trades'
    try:
        ids = set()
        previous = start * 1000
        for row in rows:
            stamp, received = row['T'], row['received_at']
            if (type(stamp) is not int or not previous <= stamp < (start + 60) * 1000
                    or row['s'] != symbol or row['S'] not in ('Buy', 'Sell')
                    or not row['i'] or row['i'] in ids or not stamp / 1000 <= received <= decision
                    or received - stamp / 1000 > 120 or min(number(row['v']), number(row['p'])) <= 0):
                return 'invalid_trade_provenance'
            ids.add(row['i'])
            previous = stamp
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 'invalid_trade_provenance'
    return None
