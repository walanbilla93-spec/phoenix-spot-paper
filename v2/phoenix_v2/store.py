"""Transactional append-only input journal. Restart reproduces all decisions."""
import copy
import json
import sqlite3
from .model import canonical, digest

class Store:
    def __init__(self,path,engine):
        self.engine=engine; self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, k TEXT UNIQUE NOT NULL, input TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL, decisions TEXT NOT NULL, state_hash TEXT NOT NULL)')
        self.head='0'*64
        try:
            for k,raw,previous,h,decisions,state_hash in self.db.execute('SELECT k,input,previous,hash,decisions,state_hash FROM batches ORDER BY id'):
                if previous!=self.head or digest(dict(k=k,input=raw,previous=previous,decisions=decisions,state_hash=state_hash))!=h: raise ValueError('corrupt audit chain')
                n=len(engine.events); engine.batch(json.loads(raw))
                if canonical(engine.events[n:])!=decisions or digest(engine.state())!=state_hash: raise ValueError('replay mismatch/source version changed')
                self.head=h
        except Exception:
            self.db.close(); raise
    def apply(self,batch):
        raw=canonical(batch); k=str(batch['end'])
        old=self.db.execute('SELECT input FROM batches WHERE k=?',(k,)).fetchone()
        if old:
            if old[0]!=raw: raise ValueError('conflicting duplicate batch')
            return False
        saved=copy.deepcopy(self.engine); n=len(self.engine.events)
        try:
            self.engine.batch(batch); decisions=canonical(self.engine.events[n:]); state_hash=digest(self.engine.state())
            h=digest(dict(k=k,input=raw,previous=self.head,decisions=decisions,state_hash=state_hash))
            with self.db:
                self.db.execute('INSERT INTO batches(k,input,previous,hash,decisions,state_hash) VALUES(?,?,?,?,?,?)',(k,raw,self.head,h,decisions,state_hash))
            self.head=h; return True
        except Exception:
            self.engine=saved; raise
    def close(self): self.db.close()
