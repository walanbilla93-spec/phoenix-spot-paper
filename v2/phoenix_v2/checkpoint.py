"""Versioned, allow-listed JSON object graph; no executable deserialization."""
import json
from .model import D, Bar, ATR, canonical
from .structure import Structure, Pivot
from .objects import Zone, Pool, Liquidity
from .strategy import Strategy, Signal, Setup, Sweep
from .portfolio import Arm, Order, Position

CLASSES = {c.__name__: c for c in (Bar, ATR, Structure, Pivot, Zone, Pool,
           Liquidity, Strategy, Signal, Setup, Sweep, Arm, Order, Position)}

def dumps(value):
    seen = {}
    def enc(x):
        if x is None or type(x) in (str, int, float, bool): return x
        if isinstance(x, D): return {'decimal': str(x)}
        key = id(x)
        if key in seen: return {'ref': seen[key]}
        n = len(seen); seen[key] = n
        if type(x) in (list, tuple, set):
            return {'id': n, 'type': type(x).__name__, 'value': [enc(v) for v in x]}
        if type(x) is dict:
            return {'id': n, 'type': 'dict', 'value': [[enc(k), enc(v)] for k,v in x.items()]}
        if type(x).__name__ not in CLASSES or type(x) is not CLASSES[type(x).__name__]:
            raise ValueError('unsupported checkpoint object')
        return {'id': n, 'type': type(x).__name__, 'value':
                {k: enc(v) for k,v in vars(x).items() if k not in ('log', 'manifest')}}
    return canonical({'version': 1, 'graph': enc(value)})

def loads(text):
    doc = json.loads(text)
    if doc['version'] != 1: raise ValueError('checkpoint version mismatch')
    seen = {}; definitions = {}
    def collect(x):
        if not isinstance(x,dict) or 'decimal' in x or 'ref' in x:return
        # Object attribute maps also contain business fields named "id".
        # Only the encoded node wrapper defines a graph identity; never scan
        # its raw attribute map as though it were another graph node.
        n=x['id'];t=x['type']
        if type(n) is not int or n<0:raise ValueError('invalid graph identity')
        if n in definitions:raise ValueError('duplicate graph definition')
        definitions[n]=x
        if t=='dict':
            for k,v in x['value']:collect(k);collect(v)
        elif t in ('list','tuple','set'):
            for v in x['value']:collect(v)
        elif t in CLASSES:
            for v in x['value'].values():collect(v)
        else:raise ValueError('unknown checkpoint class')
    collect(doc['graph'])
    def dec(x):
        if not isinstance(x, dict): return x
        if 'decimal' in x:
            n = D(x['decimal'])
            if not n.is_finite(): raise ValueError('nonfinite checkpoint')
            return n
        if 'ref' in x:
            n=x['ref']
            if n in seen:return seen[n]
            if n not in definitions:raise ValueError('missing graph reference')
            return dec(definitions[n])
        t = x['type']; n = x['id']
        if n in seen:return seen[n]
        if t == 'dict':
            out = {}; seen[n] = out
            out.update((dec(k), dec(v)) for k,v in x['value'])
        elif t in ('list', 'tuple', 'set'):
            out = []; seen[n] = out; out.extend(dec(v) for v in x['value'])
            if t == 'tuple': out = tuple(out)
            elif t == 'set': out = set(out)
            seen[n] = out
        else:
            if t not in CLASSES: raise ValueError('unknown checkpoint class')
            out = object.__new__(CLASSES[t]); seen[n] = out
            for k,v in x['value'].items():
                if k.startswith('__') or k in ('log', 'manifest'): raise ValueError('unsafe checkpoint field')
                object.__setattr__(out, k, dec(v))
        return out
    return dec(doc['graph'])
