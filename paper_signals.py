"""Unchanged two-confirmation detector from the previous offline trial."""
from research_engine import footprint_permission

def confirmations(w,bar,levels,ts):
    """Only post-touch candles; reference/liquidity fixed at original touch."""
    side=w['side']; span=bar['h']-bar['l']; out={}
    if bar['t']<w['touch_ms'] or span<=0:return out
    ref=w.get('reference'); tick=(w.get('instrument') or {}).get('tick')
    directional=side*(bar['c']-bar['o'])/span>=.6
    outer=(bar['c']-bar['l'])/span>=.75 if side==1 else (bar['h']-bar['c'])/span>=.75
    crossed=(w.get('reference_unbroken') and ref and tick and w.get('last_close') is not None
      and side*(w['last_close']-ref['price'])<=0 and side*(bar['c']-ref['price'])>=tick
      and ref['available_ms']<=w['touch_ms'])
    if directional and outer and crossed:
        out['mss']={'passed':True,'available_ms':ts,'reference':ref,'bar':bar}
    liq=w.get('liquidity')
    if liq and liq['available_ms']<=w['touch_ms']:
        swept=(bar['l']<liq['price']<bar['c'] if side==1 else bar['h']>liq['price']>bar['c'])
        half=(bar['c']-bar['l'])/span>=.5 if side==1 else (bar['h']-bar['c'])/span>=.5
        if swept and half:
            out['liquidity']={'passed':True,'available_ms':ts,'reference':liq,'bar':bar,
                'failed_nearer_poi':w.get('nearer')}
    if levels and w.get('row_width'):
        fp=footprint_permission(levels,side,w['row_width'])
        if fp['passed']:out['footprint']={**fp,'available_ms':ts}
    return out
