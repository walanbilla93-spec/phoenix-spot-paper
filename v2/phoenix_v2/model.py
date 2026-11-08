from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, getcontext
import hashlib
import json

getcontext().prec = 40
D = Decimal
TIMEFRAMES = (1, 5, 15, 60, 240, 1440)

def canonical(value):
    def encode(x):
        if isinstance(x, Decimal): return str(x)
        if hasattr(x, '__dataclass_fields__'): return asdict(x)
        raise TypeError(type(x).__name__)
    return json.dumps(value, default=encode, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

def number(x):
    v = D(str(x))
    if not v.is_finite(): raise ValueError('nonfinite number')
    return v

def floor(x, step): return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step
def ceil(x, step): return (x / step).to_integral_value(rounding=ROUND_CEILING) * step
def adverse_round(x, step, side): return ceil(x, step) if side == 1 else floor(x, step)
def slip(price, atr):
    if price <= 0 or atr is None or atr <= 0: raise ValueError('missing execution ATR')
    return max(D('.0005') * price, D('.05') * atr)

@dataclass(frozen=True)
class Bar:
    symbol: str
    tf: int
    start: int  # UTC seconds, inclusive; end exclusive
    o: D
    h: D
    l: D
    c: D
    v: D

    @property
    def end(self): return self.start + self.tf * 60
    @property
    def side(self): return (self.c > self.o) - (self.c < self.o)

    def validate(self, available):
        if self.tf not in TIMEFRAMES or self.start % (self.tf*60): raise ValueError('unaligned bar')
        if self.end > available: raise ValueError('unclosed/future bar')
        if any(not isinstance(x, D) or not x.is_finite() for x in (self.o,self.h,self.l,self.c,self.v)):
            raise ValueError('invalid number')
        if self.l <= 0 or self.v < 0 or self.h < max(self.o,self.c,self.l) or self.l > min(self.o,self.c):
            raise ValueError('invalid OHLCV')

def displacement(bar, prior_atr, side):
    return (prior_atr is not None and prior_atr > 0 and bar.side == side and bar.h > bar.l
            and abs(bar.c-bar.o) >= D('1.5')*prior_atr
            and abs(bar.c-bar.o)/(bar.h-bar.l) >= D('.65'))

class ATR:
    def __init__(self): self.previous = None; self.value = None; self.seed = []; self.count = 0
    def update(self, bar):
        tr = bar.h-bar.l if self.previous is None else max(bar.h-bar.l, abs(bar.h-self.previous), abs(bar.l-self.previous))
        self.previous = bar.c; self.count += 1
        if self.value is None:
            self.seed.append(tr)
            if len(self.seed) == 14: self.value = sum(self.seed)/14; self.seed = []
        else: self.value = (self.value*13+tr)/14
        return self.value
    @property
    def ready(self): return self.count >= 250 and self.value is not None and self.value > 0

class Aggregator:
    """No flat filling, partial first buckets or aggregation across missing minutes."""
    def __init__(self): self.previous = None; self.buckets = {t: [] for t in TIMEFRAMES if t != 1}
    def push(self, bar, available):
        bar.validate(available)
        if bar.tf != 1: raise ValueError('base must be 1m')
        if self.previous is not None and bar.start != self.previous + 60: raise ValueError('minute gap/duplicate/order')
        self.previous = bar.start; result = [bar]
        for tf, rows in self.buckets.items():
            if bar.start % (tf*60) == 0: rows.clear()
            rows.append(bar)
            if bar.end % (tf*60) == 0:
                if len(rows) == tf and rows[0].start % (tf*60) == 0:
                    result.append(Bar(bar.symbol,tf,rows[0].start,rows[0].o,max(x.h for x in rows),min(x.l for x in rows),bar.c,sum(x.v for x in rows)))
                rows.clear()
        return result
