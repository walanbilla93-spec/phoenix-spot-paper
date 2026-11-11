"""Rebuildable history views; immutable events remain the accounting evidence."""
import json
from .model import D, number, canonical

TRADE_EVENTS=('entry_fill','exit_fill','position_closed','funding','be_effective','liability_quarantined','funding_missing')
REJECTIONS=('opportunity','order_cancelled','setup_reject','sweep_reject','setup_cancel','setup_expired',
            'sweep_expired','liability_quarantined','funding_missing','interval_quarantined',
            'penetration_evaluation','setup_waiting','signal_execution_skipped')

def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS history_context(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS paper_history(id TEXT PRIMARY KEY,sequence INTEGER NOT NULL,
        arm TEXT,symbol TEXT,closed INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS paper_history_page ON paper_history(sequence);
      CREATE TABLE IF NOT EXISTS financial_summary(arm TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS projection_version(id INTEGER PRIMARY KEY,version INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS events_symbol_page ON events(json_extract(payload,'$.symbol'),seq);
      CREATE INDEX IF NOT EXISTS events_signal_symbol_page ON events(coalesce(json_extract(payload,'$.symbol'),json_extract(payload,'$.signal.symbol')),seq);
    ''')
    if not db.execute('SELECT 1 FROM projection_version WHERE id=1').fetchone():
        for seq,payload in db.execute('SELECT seq,payload FROM events ORDER BY seq'):project(db,json.loads(payload),seq)
        db.execute('INSERT INTO projection_version VALUES(1,1)');db.commit()

def _context(db,key):
    row=db.execute('SELECT payload FROM history_context WHERE id=?',(key,)).fetchone()
    return json.loads(row[0]) if row else {}

def _totals(db,arm,fees=D(0),funding=D(0),gross=D(0)):
    old=db.execute('SELECT payload FROM financial_summary WHERE arm=?',(arm,)).fetchone()
    t=json.loads(old[0]) if old else dict(fees='0',funding='0',realized_gross='0')
    for key,value in (('fees',fees),('funding',funding),('realized_gross',gross)):
        t[key]=str(number(t[key])+value)
    db.execute('INSERT OR REPLACE INTO financial_summary VALUES(?,?)',(arm,canonical(t)))

def project(db,e,seq):
    kind=e['event'];key=e.get('id')
    if kind=='signal':
        db.execute('INSERT OR REPLACE INTO history_context VALUES(?,?)',('signal:'+key,canonical(e['signal'])));return
    if kind=='order_reserved':
        signal=_context(db,'signal:'+key.split(':',1)[-1]);ctx=dict(e)
        ctx.setdefault('symbol',signal.get('symbol'));ctx.setdefault('side',signal.get('side'))
        db.execute('INSERT OR REPLACE INTO history_context VALUES(?,?)',(key,canonical(ctx)));return
    if kind not in TRADE_EVENTS or not key:return
    old=db.execute('SELECT sequence,payload FROM paper_history WHERE id=?',(key,)).fetchone()
    if not old and kind!='entry_fill':return
    ctx=_context(db,key);snap=e.get('position_snapshot',{})
    if old:first,row=old[0],json.loads(old[1])
    else:
        first=seq
        row=dict(id=key,arm=e.get('arm'),symbol=e.get('symbol') or ctx.get('symbol'),
            side=e.get('side') or ctx.get('side'),entry_at=e['at'],exit_at=None,
            entry_price=e['price'],exit_price=None,size=e['qty'],remaining_size=e['qty'],
            initial_sl=ctx.get('stop'),sl=ctx.get('stop'),tp=ctx.get('target'),
            fees='0',funding='0',realized_gross='0',net_pnl=None,exit_reason=None,
            exits=[],state='OPEN',evidence_complete=bool(snap),multiplier=ctx.get('rules',{}).get('multiplier','1'))
    fee=number(e.get('fee','0'));flow=number(e.get('cashflow','0'));gross=D(0)
    row['fees']=str(number(row['fees'])+fee);row['funding']=str(number(row['funding'])+flow)
    if kind=='exit_fill':
        qty=number(e['qty']);price=number(e['price'])
        gross=number(row['side'])*(price-number(row['entry_price']))*qty*number(row['multiplier']) if row['side'] is not None else D(0)
        row['realized_gross']=str(number(row['realized_gross'])+gross)
        row['remaining_size']=e['residual'];row['exits'].append(dict(at=e['at'],price=e['price'],size=e['qty'],fee=e['fee'],reason=e['reason']))
        exited=sum((number(x['size']) for x in row['exits']),D(0))
        row['exit_price']=str(sum((number(x['price'])*number(x['size']) for x in row['exits']),D(0))/exited)
        row['exit_reason']=e['reason']
        if number(e['residual'])==0:row['exit_at']=e['at'];row['state']='CLOSED'
    if snap:
        row.update(symbol=snap['symbol'],side=snap['side'],entry_at=snap['entry_at'],
            entry_price=snap['entry'],size=snap['original_qty'],remaining_size=snap['remaining_qty'],
            initial_sl=snap['initial_sl'],sl=snap['sl'],tp=snap['tp'],fees=snap['fees'],funding=snap['funding'],
            realized_gross=snap['realized_gross'],multiplier=snap['multiplier'])
    if kind in ('liability_quarantined','funding_missing'):
        row['evidence_complete']=False;row['unresolved_reason']=e.get('reason',kind)
        row['state']='QUARANTINED_OPEN' if number(row['remaining_size']) else 'QUARANTINED_CLOSED'
    if kind=='position_closed':
        row['exit_at']=e['exit_at'];row['state']='CLOSED' if row.get('evidence_complete') else 'QUARANTINED_CLOSED'
    row['net_pnl']=str(number(row['realized_gross'])-number(row['fees'])+number(row['funding'])) if row.get('evidence_complete') else None
    row['sequence']=first
    db.execute('INSERT OR REPLACE INTO paper_history VALUES(?,?,?,?,?,?)',
        (key,first,row['arm'],row['symbol'],int(number(row['remaining_size'])==0),canonical(row)))
    _totals(db,row['arm'],fee,flow,gross)

def page(db,table='events',before=None,limit=25,arm=None,symbol=None,kinds=None):
    if table not in ('events','paper_history'):raise ValueError('unsupported history')
    limit=max(1,min(int(limit),100));column='seq' if table=='events' else 'sequence'
    where=[];args=[]
    if before is not None:where.append(column+'<?');args.append(int(before))
    for key,value in (('arm',arm),('symbol',symbol)):
        if value:
            expr="coalesce(json_extract(payload,'$.symbol'),json_extract(payload,'$.signal.symbol'))" if key=='symbol' and table=='events' else key
            where.append('(arm=? OR arm IS NULL)' if key=='arm' and table=='events' and value in ('A','B') else expr+'=?');args.append(value)
    if kinds:
        where.append('kind IN ('+','.join('?' for _ in kinds)+')');args.extend(kinds)
    query='SELECT '+column+',payload FROM '+table+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY '+column+' DESC LIMIT ?'
    rows=db.execute(query,(*args,limit+1)).fetchall();more=len(rows)>limit;rows=rows[:limit]
    return dict(items=[dict(json.loads(p),sequence=n) for n,p in rows],
        next_cursor=rows[-1][0] if more and rows else None,limit=limit)
