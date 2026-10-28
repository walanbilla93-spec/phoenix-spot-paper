"""Causal review of candidate clips. Explicit unknowns block qualification.

This does not claim that a mechanical pivot is the trader's MSS or wave count.
It tests observable prerequisites and preserves missing semantic annotations.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

LAYERS = ('elliott', 'smc_context', 'poi', 'inducement', 'mss', 'profile', 'footprint')


def observable_sequence(candles, touch, end, side):
    before = [k for k in candles if k['available_ms'] <= touch]
    pivots = []
    for i in range(2, len(before)-2):
        k = before[i]; others = before[i-2:i]+before[i+1:i+3]
        if side == 1 and k['h'] > max(x['h'] for x in others):
            pivots.append(k['h'])
        if side == -1 and k['l'] < min(x['l'] for x in others):
            pivots.append(k['l'])
    level = pivots[-1] if pivots else None
    # A structure level already broken before the touch is not a new shift.
    if level is not None and side*(before[-1]['c']-level) > 0:
        return dict(reference=level, state='REFERENCE_ALREADY_BROKEN')
    sweep = None
    for i, k in enumerate(candles):
        if i < 5 or not touch < k['available_ms'] < end:
            continue
        previous = candles[i-5:i]
        liquidity = min(x['l'] for x in previous) if side == 1 else max(x['h'] for x in previous)
        reclaimed = k['l'] < liquidity < k['c'] if side == 1 else k['h'] > liquidity > k['c']
        if sweep is None and reclaimed:
            sweep = dict(available_ms=k['available_ms'], level=liquidity,
                         extreme=k['l' if side == 1 else 'h'])
        if sweep and level is not None and k['available_ms'] > sweep['available_ms']:
            if side*(k['c']-level) > 0 and side*(candles[i-1]['c']-level) <= 0:
                return dict(state='SWEEP_THEN_CLOSE_BREAK_PROXY', reference=level,
                            sweep=sweep, break_available_ms=k['available_ms'])
    return dict(state='NO_ORDERED_SWEEP_AND_BREAK', reference=level, sweep=sweep)


def validate_annotation(a, decision_ms):
    """Annotations must exist before entry; paths/hashes identify frozen evidence."""
    if not a:
        return ['MISSING_CAUSAL_ANNOTATIONS']
    missing = []
    if not isinstance(a.get('recorded_ms'), int) or a['recorded_ms'] > decision_ms:
        missing.append('ANNOTATION_NOT_AVAILABLE_BEFORE_ENTRY')
    for layer in LAYERS:
        e = a.get(layer, {})
        if e.get('state') != 'CONFIRMED' or not e.get('evidence_ref'):
            missing.append('MISSING_'+layer.upper())
        if not isinstance(e.get('available_ms'), int) or e['available_ms'] > decision_ms:
            missing.append('NONCAUSAL_'+layer.upper())
    if not a.get('rule_version'):
        missing.append('MISSING_RULE_VERSION')
    return missing


def paper_outcome(plan, candles, fees_bps=6, slippage_bps=2):
    """Conservative OHLC replay. Ambiguous candles remain unresolved.

    Units are frozen quantities, not account leverage. No inference of limit
    fills, intrabar target order, or unobserved ladder additions is allowed.
    """
    entry_ms, expiry = plan['entry_ms'], plan['expiry_ms']
    side = 1 if plan['side'] == 'LONG' else -1
    entry, stop = plan['entry'], plan['stop']
    targets = plan['targets']
    if not targets or side*(entry-stop) <= 0 or any(side*(t-entry) <= 0 for t in targets) or expiry <= entry_ms:
        raise ValueError('INVALID_FROZEN_PLAN')
    if len(plan.get('legs', [])) > 3:
        raise ValueError('LADDER_LIMIT')
    risk = abs(entry-stop)
    # OHLC cannot distinguish extrema before and after an intraminute fill.
    # Require raw event replay for that entry instead of ignoring the risk.
    if entry_ms % 60000:
        return dict(state='CENSORED_INTRAMINUTE_ENTRY_REQUIRES_RAW_REPLAY')
    expected = entry_ms
    seen = 0
    for k in candles:
        # A minute containing entry has unknown pre/post-entry extrema.
        if k['t'] < expected or k['t'] >= expiry:
            continue
        if k['t'] != expected:
            return dict(state='CENSORED_DATA_GAP')
        seen += 1; expected += 60000
        sl = k['l'] <= stop if side == 1 else k['h'] >= stop
        hit = [t for t in targets if (k['h'] >= t if side == 1 else k['l'] <= t)]
        if sl and hit:
            return dict(state='AMBIGUOUS_STOP_AND_TARGET_SAME_MINUTE')
        if sl or hit:
            exit_price = stop if sl else targets[0]
            gross = side*(exit_price-entry)
            costs = (entry+exit_price)*(fees_bps+slippage_bps)/10000
            return dict(state='STOP' if sl else 'FIRST_TARGET_TOUCHED',
                        at_ms=k['t']+60000, gross_R=gross/risk,
                        illustrative_net_R=(gross-costs)/risk,
                        note='First-touch diagnostic only; not a realized partial-exit ledger.')
    return dict(state='CENSORED_INSUFFICIENT_FOLLOWUP' if not seen or expected < expiry else 'EXPIRED',
                note='No unobserved fills or ladder additions inferred.')


def review_existing(root):
    observations = json.loads((root/'clip-observations.json').read_text())
    rows = []
    for item in observations:
        clip = json.loads((root/f"clip-{item['clip']:02d}-{item['symbol']}.json").read_text())
        touch = item.get('touch_ms')
        seq = observable_sequence(clip['candles'], touch, item['end_ms'],
                1 if item['side'] == 'LONG_CONTEXT' else -1) if touch else {'state':'NO_ZONE_TOUCH'}
        rows.append(dict(clip=item['clip'], symbol=item['symbol'], sequence=seq,
            status='NOT_QUALIFIED', blockers=['MISSING_FROZEN_ELLIOTT_AND_HTF_CONTEXT',
            'MISSING_VALIDATED_SESSION_PROFILE', 'NO_FROZEN_ENTRY_STOP_TARGET_LADDER'],
            existing_clip_usable_for='PRICE_STRUCTURE_AND_LOCAL_FLOW_OBSERVATION'))
    return dict(kind='EXISTING_CLIP_FEASIBILITY_REVIEW', source_date='2026-10-27',
        clips=len(rows), qualified_setups=0,
        states=dict(Counter(r['sequence']['state'] for r in rows)),
        conclusion='Existing clips cannot certify full-confluence setups. Missing history is not reconstructed.', rows=rows)


def main():
    p = argparse.ArgumentParser(); p.add_argument('--clips', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True); args = p.parse_args()
    result = review_existing(args.clips)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != 'rows'}, indent=2))


if __name__ == '__main__':
    main()
