"""Read-only integrity verification; never opens prospective outcome metrics."""
import argparse,hashlib,json,sqlite3
from pathlib import Path
from paper_trials import HASH,LEGACY_HASH,VARIANTS

def audit(path):
    db=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True);db.execute('PRAGMA cache_size=-4096')
    integrity=db.execute('PRAGMA integrity_check').fetchone()[0];previous='0'*64;errors=0;n=0
    for text,digest in db.execute('SELECT payload,hash FROM events ORDER BY id'):
        expected=hashlib.sha256((previous+text).encode()).hexdigest();errors+=digest!=expected or json.loads(text)['protocol_sha256'] not in (HASH,LEGACY_HASH);previous=digest;n+=1
    row=db.execute("SELECT v FROM meta WHERE k='protocol_hash'").fetchone();valid=bool(row and json.loads(row[0])==HASH)
    amendment=db.execute("SELECT v FROM meta WHERE k='amendment_first_signal_id'").fetchone();start=json.loads(amendment[0]) if amendment else 1
    incomplete=db.execute('SELECT count(*) FROM (SELECT signal,count(*) n FROM trades GROUP BY signal HAVING n<>CASE WHEN signal<? THEN 3 ELSE ? END)',(start,len(VARIANTS))).fetchone()[0]
    db.close();return dict(audit_ok=integrity=='ok' and errors==0 and valid and incomplete==0,integrity=integrity,chain_errors=errors,protocol_matches=valid,incomplete_variant_sets=incomplete,events=n,ledger_hash=previous)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('snapshot');args=p.parse_args();print(json.dumps(audit(args.snapshot),indent=2))
