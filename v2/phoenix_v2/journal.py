"""Dedicated RC2 durable journal, atomic checkpoints and immutable evidence."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import zlib
from .model import canonical, digest
from . import history

def sha(blob): return hashlib.sha256(blob).hexdigest()

class Journal:
    def __init__(self, directory, max_bytes=1_000_000_000):
        self.directory = Path(directory); self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes; self.lock = threading.RLock()
        self.guard = open(self.directory/'worker.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.guard.seek(0); self.guard.write(b'0'); self.guard.flush(); self.guard.seek(0)
                msvcrt.locking(self.guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.guard.close(); raise RuntimeError('another RC2 worker owns this directory')
        self.db = sqlite3.connect(str(self.directory/'paper.sqlite3'), check_same_thread=False, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA wal_autocheckpoint=256')
        self.db.execute('PRAGMA max_page_count='+str(max_bytes//4096))
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS evidence(
          key TEXT PRIMARY KEY, kind TEXT NOT NULL, at INTEGER NOT NULL,
          payload BLOB NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS evidence_kind_at ON evidence(kind,at);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, arm TEXT,
          payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_kind_seq ON events(kind,seq);
        CREATE TABLE IF NOT EXISTS checkpoint(
          id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL,
          hash TEXT NOT NULL, head TEXT NOT NULL, sequence INTEGER NOT NULL);
        ''')
        for table in ('evidence','events'):
            for operation in ('UPDATE','DELETE'):
                self.db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                  BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable journal'); END''')
        self.db.commit(); self.verify()
        history.initialize(self.db)
        self.counts = dict(self.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
        self.evidence_counts = dict(self.db.execute('SELECT kind,count(*) FROM evidence GROUP BY kind'))

    def verify(self):
        if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('database corruption')
        head = '0'*64; sequence = 0
        for seq,payload,prev,h in self.db.execute('SELECT seq,payload,previous,hash FROM events ORDER BY seq'):
            if seq != sequence+1 or prev != head or digest([prev,seq,payload]) != h:
                raise ValueError('journal chain corruption')
            sequence = seq; head = h
        for blob,h in self.db.execute('SELECT payload,hash FROM evidence'):
            if sha(blob) != h: raise ValueError('evidence corruption')
        row = self.db.execute('SELECT payload,hash,head,sequence FROM checkpoint WHERE id=1').fetchone()
        if row and (sha(row[0]) != row[1] or row[2:] != (head,sequence)):
            raise ValueError('checkpoint/journal mismatch')
        if sequence and not row: raise ValueError('missing checkpoint')
        self.head = head; self.sequence = sequence

    def load(self):
        row = self.db.execute('SELECT payload FROM checkpoint WHERE id=1').fetchone()
        return zlib.decompress(row[0]).decode() if row else None

    def capacity(self):
        size = sum(p.stat().st_size for p in self.directory.glob('paper.sqlite3*'))
        if size > self.max_bytes*0.9 or shutil.disk_usage(self.directory).free < 64_000_000:
            raise RuntimeError('immutable journal storage limit; archive/expand storage before continuing')
        return size

    def commit(self, evidence, events, state):
        """All evidence, decisions and next state commit together, or none do."""
        with self.lock:
            self.capacity(); head = self.head; seq = self.sequence
            event_counts = dict(self.counts); evidence_counts = dict(self.evidence_counts)
            try:
                self.db.execute('BEGIN IMMEDIATE')
                for key,kind,at,value in evidence:
                    encoded = canonical(value).encode()
                    old = self.db.execute('SELECT kind,at,payload,hash FROM evidence WHERE key=?',(key,)).fetchone()
                    if old:
                        old_kind, old_at, old_blob, old_hash = old
                        if (old_kind != kind or old_at != at or sha(old_blob) != old_hash
                                or zlib.decompress(old_blob) != encoded):
                            raise ValueError('immutable evidence conflict: '+key)
                        continue
                    # Keep the same lossless zlib format for older readers and rollbacks.
                    blob = zlib.compress(encoded, 3)
                    if kind in ('public_trades', 'late_public_trades'):
                        tighter = zlib.compress(encoded, 6)
                        if len(tighter) < len(blob):
                            blob = tighter
                    h = sha(blob)
                    self.db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(key,kind,at,blob,h))
                    evidence_counts[kind] = evidence_counts.get(kind,0)+1
                for event in events:
                    seq += 1; payload = canonical(event); h = digest([head,seq,payload])
                    self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                        (seq,event['at'],event['event'],event.get('arm'),payload,head,h))
                    history.project(self.db,event,seq)
                    head = h; k = event['event']; event_counts[k] = event_counts.get(k,0)+1
                blob = zlib.compress(state.encode(), 3)
                if len(state) > 32_000_000: raise RuntimeError('strategy checkpoint resource ceiling')
                self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES(1,?,?,?,?)',(blob,sha(blob),head,seq))
                self.db.commit()
            except BaseException:
                self.db.rollback(); raise
            self.sequence = seq; self.head = head
            self.counts = event_counts; self.evidence_counts = evidence_counts

    def recent(self, kinds=None, limit=50):
        with self.lock:
            if kinds:
                rows = self.db.execute('SELECT payload FROM events WHERE kind IN ('+','.join('?' for _ in kinds)+') ORDER BY seq DESC LIMIT ?',(*kinds,limit))
            else: rows = self.db.execute('SELECT payload FROM events ORDER BY seq DESC LIMIT ?',(limit,))
            return [json.loads(r[0]) for r in rows]

    def candles(self):
        with self.lock:
            return [json.loads(zlib.decompress(r[0])) for r in self.db.execute(
                "SELECT payload FROM evidence WHERE kind='candle' ORDER BY at DESC LIMIT 30")]

    def page(self, **filters):
        with self.lock:return history.page(self.db,**filters)

    def recent_minute_bars(self,symbol,end):
        from .model import Bar,number
        with self.lock:
            rows=self.db.execute("SELECT payload FROM evidence WHERE kind='candle' AND at>? AND at<=? AND key GLOB ? ORDER BY at LIMIT 60",
                (end-3600,end,'candle:'+symbol+':1:*')).fetchall()
            result=[]
            for row in rows:
                b=json.loads(zlib.decompress(row[0]));result.append(Bar(b['symbol'],1,b['start'],*[number(b[k]) for k in ('o','h','l','c','v')]))
            return result

    def financials(self):
        with self.lock:return {a:json.loads(p) for a,p in self.db.execute('SELECT arm,payload FROM financial_summary')}

    def allocated_bytes(self):
        with self.lock:return self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]

    def minute_evidence(self,symbol,start):
        if int(start)!=start or start%60:raise ValueError('unaligned minute')
        keys=[f'candle:{symbol}:1:{start}',f'trades:{symbol}:{start}',f'mark:{symbol}:{start}',f'native:{symbol}:{start}']
        with self.lock:
            rows=self.db.execute('SELECT key,kind,at,payload,hash FROM evidence WHERE key IN (?,?,?,?)',keys).fetchall()
            items=[]
            for key,kind,at,blob,h in rows:
                value=json.loads(zlib.decompress(blob))
                items.append(dict(key=key,kind=kind,at=at,stored_sha256=h,canonical_sha256=digest(value),value=value))
            return dict(symbol=symbol,start=start,items=items)

    def close(self):
        self.db.close(); self.guard.close()
