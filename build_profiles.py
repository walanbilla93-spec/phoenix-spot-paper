"""Disk-backed profile reconstruction and minute-volume eligibility audit.

Usage: python build_profiles.py capture.tar --database profiles.sqlite
All payloads stream; SQLite cache 8 MiB, temporary work stays on disk.
Old approximate summaries are never promoted to exact-price footprints.
"""
import argparse
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
import tarfile

DAY = 86_400_000


def build(source, database):
    db = sqlite3.connect(database)
    db.executescript('''PRAGMA cache_size=-8192; PRAGMA temp_store=FILE;
    CREATE TABLE IF NOT EXISTS minutes(s TEXT,t INTEGER,available INTEGER,flags TEXT,v TEXT,PRIMARY KEY(s,t));
    CREATE TABLE IF NOT EXISTS candles(s TEXT,t INTEGER,v TEXT,PRIMARY KEY(s,t));
    CREATE TABLE IF NOT EXISTS levels(s TEXT,day INTEGER,p TEXT,b TEXT,a TEXT,PRIMARY KEY(s,day,p));
    CREATE TABLE IF NOT EXISTS invalid(s TEXT,t INTEGER,reason TEXT,PRIMARY KEY(s,t,reason));
    CREATE TABLE IF NOT EXISTS gaps(lo INTEGER,hi INTEGER);
    ''')
    counts = dict(records=0, footprints=0, approximate_summaries=0, corrupt_members=0)
    with tarfile.open(source, 'r|') as archive:
        for member in archive:
            if not member.name.endswith('.jsonl.gz'):
                continue
            try:
                with gzip.GzipFile(fileobj=archive.extractfile(member)) as stream:
                    for line in stream:
                        r = json.loads(line); counts['records'] += 1
                        kind = r.get('kind'); s = r.get('symbol')
                        if kind == 'candle_1m':
                            k = r['candle']; db.execute('INSERT OR IGNORE INTO candles VALUES (?,?,?)',(s,k['t'],str(k['v'])))
                        elif kind == 'flow_summary_1m':
                            counts['approximate_summaries'] += 1
                        elif kind in ('footprint_late_trade', 'footprint_quality_error'):
                            db.execute('INSERT OR IGNORE INTO invalid VALUES (?,?,?)',(s,r['minute_ms'],kind))
                        elif kind in ('gap','gap_recovered'):
                            db.execute('INSERT INTO gaps VALUES (?,?)',(r.get('since_ms',r['receive_ms']),r.get('until_ms',r['receive_ms'])))
                        elif kind == 'boot':
                            db.execute('INSERT INTO gaps VALUES (?,?)',(r['receive_ms'],r['receive_ms']))
                        elif kind == 'footprint_1m':
                            counts['footprints'] += 1; t=r['minute_ms']; day=t//DAY*DAY
                            v=Decimal(r['buy_volume'])+Decimal(r['sell_volume'])
                            flags=r['quality_flags']
                            old=db.execute('SELECT v,flags FROM minutes WHERE s=? AND t=?',(s,t)).fetchone()
                            if old:
                                db.execute('INSERT OR IGNORE INTO invalid VALUES (?,?,?)',(s,t,'OVERLAPPING_FOOTPRINT_RECORDS'))
                                continue
                            db.execute('INSERT INTO minutes VALUES (?,?,?,?,?)',(s,t,r['available_ms'],json.dumps(flags),str(v)))
                            total=Decimal(0)
                            for p,b,a,*_ in r['levels']:
                                total += Decimal(b)+Decimal(a)
                                previous=db.execute('SELECT b,a FROM levels WHERE s=? AND day=? AND p=?',(s,day,p)).fetchone()
                                if previous:b=str(Decimal(b)+Decimal(previous[0]));a=str(Decimal(a)+Decimal(previous[1]))
                                db.execute('INSERT OR REPLACE INTO levels VALUES (?,?,?,?,?)',(s,day,p,b,a))
                            if total != v:
                                db.execute('INSERT OR IGNORE INTO invalid VALUES (?,?,?)',(s,t,'LEVEL_SUM_MISMATCH'))
                        if counts['records'] % 10000 == 0: db.commit()
            except (ValueError, OSError, EOFError, KeyError):
                counts['corrupt_members'] += 1
            db.commit()
    sessions=[]
    for s,day in db.execute('SELECT DISTINCT s,(t/86400000)*86400000 FROM candles ORDER BY s,2'):
        n=0; good=0; mismatch=0; missing=0; flagged=0
        for t,v,mv,flags in db.execute('SELECT c.t,c.v,m.v,m.flags FROM candles c LEFT JOIN minutes m ON c.s=m.s AND c.t=m.t WHERE c.s=? AND c.t>=? AND c.t<? ORDER BY c.t',(s,day,day+DAY)):
            n+=1
            invalid=db.execute('SELECT 1 FROM invalid WHERE s=? AND t=? LIMIT 1',(s,t)).fetchone()
            if invalid or flags not in (None,'[]'):flagged+=1;continue
            if mv is None:
                if Decimal(v)==0:good+=1
                else:missing+=1
            elif abs(Decimal(v)-Decimal(mv)) > max(Decimal('0.00000001'),abs(Decimal(v))*Decimal('0.000001')):mismatch+=1
            else:good+=1
        gaps=db.execute('SELECT count(*) FROM gaps WHERE lo<? AND hi>=?',(day+DAY,day)).fetchone()[0]
        qualified=n==1440 and good==1440 and gaps==0 and counts['corrupt_members']==0
        sessions.append(dict(symbol=s,utc_day_ms=day,candle_minutes=n,validated_minutes=good,
            missing_footprint_minutes=missing,mismatched_minutes=mismatch,flagged_minutes=flagged,
            overlapping_gaps=gaps,complete_session_profile=qualified))
    db.close()
    return dict(**counts, sessions=sessions,
        complete_session_profiles=sum(s['complete_session_profile'] for s in sessions),
        note='A partial UTC day, a gap or missing exact footprints prevents full-session eligibility.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--database',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.database.exists():raise SystemExit('Use a new database path; refusing to mix audits')
    report=build(a.source,a.database);a.output.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
