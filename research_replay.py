"""Bounded capture replay and decision-ledger verification.

python research_replay.py archive.tar --work NEW_DIRECTORY --output result.json
python research_replay.py --audit snapshot.sqlite --output result.json

Archive records are sorted/deduplicated in SQLite (8MiB cache), never a
whole-history Python list. The export snapshot is audited independently.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
from research_engine import ResearchEngine, POLICY_HASH, LAYERS, canonical

MAX_LINE = 2_000_000


def audit(database):
    db = sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)
    db.execute('PRAGMA cache_size=-4096')
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    counts = Counter(); previous = '0'*64; chain_errors = 0; invalid_qualifications = 0
    first = last = None
    for ts, kind, payload, digest in db.execute('SELECT at,kind,payload,hash FROM ledger ORDER BY id'):
        counts[kind] += 1; first = ts if first is None else min(first, ts); last = ts if last is None else max(last, ts)
        expected = hashlib.sha256((previous+payload).encode()).hexdigest()
        chain_errors += expected != digest; previous = digest
        r = json.loads(payload)
        if r.get('policy_sha256') != POLICY_HASH:
            chain_errors += 1
        if kind == 'research_qualified':
            gates = r['plan']['setup']['gates']
            invalid_qualifications += any(not gates.get(layer, {}).get('passed') for layer in LAYERS)
            invalid_qualifications += any(g.get('available_ms', ts) > ts for g in gates.values())
    orphan_entries = db.execute("""SELECT count(*) FROM ledger e WHERE e.kind='paper_entry' AND NOT EXISTS
        (SELECT 1 FROM ledger q WHERE q.kind='research_qualified' AND q.id<e.id
         AND json_extract(q.payload,'$.campaign_id')=json_extract(e.payload,'$.campaign_id'))""").fetchone()[0]
    orphan_closes = db.execute("""SELECT count(*) FROM ledger c WHERE c.kind='paper_closed' AND NOT EXISTS
        (SELECT 1 FROM ledger e WHERE e.kind='paper_entry' AND e.id<c.id
         AND json_extract(e.payload,'$.campaign_id')=json_extract(c.payload,'$.campaign_id'))""").fetchone()[0]
    outcomes = dict(closed=0, wins=0, losses=0, flat=0, net_R=0., gross_R=0., model_cost_R=0.)
    peak = drawdown = 0.
    for payload, in db.execute("SELECT payload FROM ledger WHERE kind='paper_closed' AND json_extract(payload,'$.campaign_id') NOT IN (SELECT campaign_id FROM revoked) ORDER BY id"):
        p = json.loads(payload); net = p['net_R']; outcomes['closed'] += 1
        outcomes['wins'] += net > 1e-12; outcomes['losses'] += net < -1e-12; outcomes['flat'] += abs(net) <= 1e-12
        outcomes['net_R'] += net; outcomes['gross_R'] += p['gross_R']; outcomes['model_cost_R'] += p['cost_R']
        peak = max(peak, outcomes['net_R']); drawdown = max(drawdown, peak-outcomes['net_R'])
    outcomes['win_rate_pct'] = 100*outcomes['wins']/outcomes['closed'] if outcomes['closed'] else None
    outcomes['max_drawdown_R'] = drawdown
    blockers = Counter()
    for payload, in db.execute("SELECT payload FROM ledger WHERE kind='research_decision'"):
        blockers.update(json.loads(payload).get('blockers', []))
    minutes = db.execute('SELECT count(*),coalesce(sum(valid),0) FROM minutes').fetchone()
    profile_count = db.execute('SELECT count(*) FROM profiles').fetchone()[0]
    policy = db.execute("SELECT v FROM meta WHERE k='policy_hash'").fetchone()
    valid = integrity == 'ok' and chain_errors == invalid_qualifications == orphan_entries == orphan_closes == 0 and policy and policy[0] == POLICY_HASH
    profile_mismatches=0
    for s,day,payload in db.execute('SELECT s,day,payload FROM profiles'):
        expected=db.execute("SELECT payload FROM ledger WHERE kind='research_profile' AND s=? AND json_extract(payload,'$.profile.day')=? ORDER BY id DESC LIMIT 1",(s,day)).fetchone()
        profile_mismatches += not expected or canonical(json.loads(expected[0])['profile']) != payload
    valid=valid and profile_mismatches==0
    result = dict(audit_ok=bool(valid), integrity=integrity, chain_errors=chain_errors,
        frozen_profile_mismatches=profile_mismatches,
        invalid_qualifications=invalid_qualifications, orphan_entries=orphan_entries,
        orphan_closes=orphan_closes, ledger_hash=previous, policy_sha256=POLICY_HASH,
        counts=dict(counts), blockers=dict(blockers), first_ms=first, last_ms=last,
        reconciled_minutes=minutes[0], valid_minutes=minutes[1], frozen_profiles=profile_count,
        outcomes=outcomes, decision_state=('INVALID_LEDGER' if not valid else
            'NO_CLOSED_ELIGIBLE_TRADES' if not outcomes['closed'] else 'DESCRIPTIVE_PAPER_RESULTS'),
        interpretation='WR is conditional on fully closed, non-censored paper campaigns under the frozen research cost/fill assumptions. A small sample does not establish an edge.')
    db.close()
    return result


def replay(source, work):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=False)
    index = sqlite3.connect(work/'input.sqlite')
    index.executescript('PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; CREATE TABLE events(id INTEGER PRIMARY KEY,t INTEGER,priority INTEGER,hash TEXT UNIQUE,payload TEXT);')
    stats = Counter(); symbols = set(); snapshot = None
    with tarfile.open(source, 'r|*') as archive:
        for member in archive:
            if member.name == 'research-export.sqlite':
                snapshot = work/'export-snapshot.sqlite'
                with snapshot.open('wb') as f:
                    stream = archive.extractfile(member)
                    while chunk := stream.read(65536):
                        f.write(chunk)
                continue
            if not member.name.endswith('.jsonl.gz'):
                continue
            try:
                with gzip.GzipFile(fileobj=archive.extractfile(member)) as stream:
                    while line := stream.readline(MAX_LINE+1):
                        if len(line) > MAX_LINE:
                            raise ValueError('CAPTURE_LINE_LIMIT')
                        r = json.loads(line); stats['records'] += 1
                        kind = r.get('kind')
                        if kind == 'raw':
                            topic = r.get('payload', {}).get('topic', '')
                            if not topic.startswith('orderbook.1.'):
                                continue
                            r['symbol'] = topic.rsplit('.', 1)[-1]
                        elif kind not in ('boot','gap','gap_recovered','research_boundary','public_metadata','research_bar_5m','research_watch','candle_1m',
                                          'footprint_1m','footprint_quality_error','footprint_late_trade'):
                            continue
                        s = r.get('symbol')
                        if s:
                            symbols.add(s)
                            if len(symbols) > 16:
                                raise ValueError('REPLAY_SYMBOL_LIMIT')
                        encoded = canonical(r); key = hashlib.sha256(encoded.encode()).hexdigest()
                        priority = 3 if kind == 'raw' else 2 if kind=='research_watch' else 1
                        before = index.total_changes
                        index.execute('INSERT OR IGNORE INTO events(t,priority,hash,payload) VALUES (?,?,?,?)',
                                      (r['receive_ms'], priority, key, encoded))
                        stats['duplicates_removed'] += index.total_changes == before
                        if stats['records'] % 10000 == 0:
                            index.commit()
            except (EOFError, OSError, ValueError, KeyError) as e:
                stats['corrupt_members'] += 1
                # Fail closed: do not claim a decision from a partially read member.
                raise ValueError('CAPTURE_MEMBER_INVALID: '+member.name) from e
            index.commit()
    index.execute('CREATE INDEX events_time ON events(t,priority,id)'); index.commit()
    engine = ResearchEngine(work/'replay', sorted(symbols) or ['SOLUSDT'])
    compact_mode=index.execute("SELECT 1 FROM events WHERE json_extract(payload,'$.kind')='research_bar_5m' LIMIT 1").fetchone() is not None
    snapshot_audit=audit(snapshot) if snapshot else None
    if compact_mode:
        if not snapshot_audit or not snapshot_audit['audit_ok']:
            raise ValueError('COMPACT_REPLAY_REQUIRES_VERIFIED_RESEARCH_SNAPSHOT')
        source_db=sqlite3.connect(f'file:{snapshot.resolve()}?mode=ro',uri=True)
        for payload, in source_db.execute("SELECT payload FROM ledger WHERE kind='research_profile' ORDER BY id"):
            row=json.loads(payload); profile=row['profile']
            index.execute('INSERT OR IGNORE INTO events(t,priority,hash,payload) VALUES (?,?,?,?)',
                (row['receive_ms'],0,hashlib.sha256(payload.encode()).hexdigest(),payload))
        index.commit(); source_db.close()
    for payload, in index.execute('SELECT payload FROM events ORDER BY t,priority,id'):
        r = json.loads(payload); ts = r['receive_ms']; kind = r['kind']
        if kind in ('boot','gap','gap_recovered','research_boundary'):
            engine.boundary(ts, kind.upper())
        elif kind == 'public_metadata':
            engine.metadata(r['name'], r['payload'], ts)
        elif compact_mode and kind=='research_profile':
            engine.last_ms=max(engine.last_ms,ts); profile=r['profile']
            engine.db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?,?)',(r['symbol'],profile['day'],canonical(profile)))
            engine._event('research_profile',ts,r['symbol'],profile=profile)
        elif compact_mode and kind=='research_bar_5m':
            if r.get('coverage')!='FIVE_RECONCILED_EXACT_MINUTES':
                raise ValueError('INVALID_RECORDED_BAR_COVERAGE')
            engine.last_ms=max(engine.last_ms,ts)
            engine._closed_bar(r['symbol'],r['bar'],r.get('footprint_levels') or [],ts)
        elif compact_mode and kind=='research_watch':
            frozen=r['frozen']; engine.quote(r['symbol'],frozen['gates']['poi']['quote'],ts)
        elif kind == 'raw':
            payload = r['payload']; book = payload.get('data', {})
            if book.get('b') and book.get('a'):
                engine.quote(r['symbol'], dict(bid=float(book['b'][0][0]), ask=float(book['a'][0][0]),
                    bid_size=float(book['b'][0][1]), ask_size=float(book['a'][0][1]),
                    exchange_ms=payload.get('cts', payload.get('ts'))), ts)
        else:
            stats[kind] += 1
            if not compact_mode or kind in ('footprint_quality_error','footprint_late_trade'):
                engine.record(r)
        if engine.halted:
            raise ValueError('REPLAY_HALTED: '+engine.halted)
    result = dict(input=dict(stats), measured=engine.status(), audit=audit(engine.path),
                  exported_snapshot_audit=snapshot_audit,adapter='COMPACT_VERIFIED_EVIDENCE' if compact_mode else 'LEGACY_NORMALIZED_RECORDS',
                  historical_coverage='Compact replay uses recorded reconciled bars, frozen prior-session profiles, arrival quotes and full detail for eligible contexts/campaigns. All required archive parts must be supplied. Older archives without exact footprints cannot certify full confluence.')
    engine.db.close(); index.close()
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('source', nargs='?', type=Path)
    p.add_argument('--work', type=Path); p.add_argument('--audit', type=Path)
    p.add_argument('--output', type=Path, required=True); args = p.parse_args()
    if args.audit:
        result = audit(args.audit)
    elif args.source and args.work:
        result = replay(args.source, args.work)
    else:
        p.error('Use --audit DATABASE or ARCHIVE --work NEW_DIRECTORY')
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False))
    print(json.dumps(result, indent=2, allow_nan=False))
