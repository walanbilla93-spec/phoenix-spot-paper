import argparse
import json
from pathlib import Path
from .manifest import Manifest
from .engine import Engine
from .store import Store
from .model import canonical

def main():
    p=argparse.ArgumentParser(description='Offline Phoenix V2 paper replay; never connects to an exchange')
    p.add_argument('--manifest',required=True); p.add_argument('--input',required=True); p.add_argument('--database',required=True)
    a=p.parse_args(); path=Path(a.manifest)
    m=Manifest(json.loads(path.read_text()),path.parent)
    if m.missing(): p.error('verification evidence missing: '+','.join(m.missing()))
    e=Engine(m); store=Store(a.database,e)
    try:
        with open(a.input,encoding='utf-8') as f:
            for line in f:
                if line.strip(): store.apply(json.loads(line))
        print(canonical(dict(state=store.engine.state(),audit_head=store.head,acceptance='NOT_EVALUATED')))
    finally: store.close()
if __name__=='__main__': main()
