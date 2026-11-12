"""Offline recovery for explicitly verified legacy Delta quarantines."""
import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
import time

from . import checkpoint, delta_paper
from .journal import Journal
from .model import D, canonical, digest, number
from .service import safety

POSITION_ID = 'D:AVAXUSDT:delta:1794467700'
SYMBOL = 'AVAXUSDT'
TRIGGER_MS = 1794471086073
REASON = 'ambiguous_or_stale_protection_print'


INCIDENTS = {
    'AVAX': ('D', POSITION_ID, SYMBOL, TRIGGER_MS, D('8.637'), D('8.595'), D('8.760'), D('.8'), 'stop'),
    'ZEC': ('C', 'C:ZECUSDT:delta:1794480600', 'ZECUSDT', 1794481789240,
            D('987.82'), D('986.45'), D('994.45'), D('.01'), 'target'),
}


def recover(journal, expected_head, at, *, apply=False, incident='AVAX', backup=False):
    arm, position_id, symbol, trigger_ms, entry, stop, target, quantity, mode = INCIDENTS[incident]
    if journal.head != expected_head:
        raise ValueError('journal head changed; inspect the current checkpoint and retry')
    saved = journal.load()
    if saved is None:
        raise ValueError('checkpoint missing')
    state = checkpoint.loads(saved)
    book = state['delta_arms']['books'][arm]
    if book.get('isolated_liability'):
        raise ValueError('legacy liability already isolated')
    p = book['position']
    if (book['unresolved'] != REASON or book['pending'] is not None or not p
            or p['id'] != position_id or p['symbol'] != symbol or p['entry'] != entry
            or p['stop'] != stop or p['target'] != target
            or p['original_qty'] != quantity or p['qty'] != quantity
            or p['exit_mode'] is not None or p['realized'] != 0):
        raise ValueError(f'checkpoint does not match the exact unresolved {incident} incident')

    quarantines = journal.db.execute(
        "SELECT seq,payload,hash FROM events WHERE kind='liability_quarantined' AND arm=? AND at=?",
        (arm, trigger_ms / 1000),
    ).fetchall()
    matches = [(seq, item, event_hash) for seq, payload, event_hash in quarantines
               if (item := json.loads(payload)).get('id') == position_id and item.get('reason') == REASON]
    if len(matches) != 1:
        raise ValueError('unique original quarantine event missing')
    seq, original, event_hash = matches[0]
    if canonical(delta_paper.position_snapshot(p)) != canonical(original.get('position_snapshot')):
        raise ValueError('checkpoint economics differ from original quarantine snapshot')
    totals = journal.financials().get(arm, {})
    if (not {'fees', 'funding', 'realized_gross'} <= totals.keys()
            or book['cash'] != delta_paper.CAPITAL + number(totals['realized_gross'])
            - number(totals['fees']) + number(totals['funding'])):
        raise ValueError(f'Arm {arm} cash does not reconcile to the journal')

    minute = trigger_ms // 60000 * 60
    evidence = journal.minute_evidence(symbol, minute)
    retained = [item for item in evidence['items'] if item['kind'] == 'public_trades']
    if len(retained) != 1:
        raise ValueError('retained public trades missing')
    item = retained[0]
    rows = item['value']
    if delta_paper.delta.validate_rows(symbol, minute, rows, minute + 180):
        raise ValueError('invalid retained public trade provenance')
    group = [row for row in rows if row['T'] == trigger_ms]
    prices = {number(row['p']) for row in group}
    if len(prices) < 2 or not delta_paper.protection_fresh(p, group):
        raise ValueError('fresh mixed-price protection evidence not verified')
    if mode == 'stop' and not {D('8.598'), D('8.597'), D('8.596'), stop} <= prices:
        raise ValueError('AVAX stop trigger not verified')
    if mode == 'target' and (len(group) != 19 or min(prices) != D('994.56')
                             or max(prices) != D('994.87') or min(prices) < target + p['rules']['tick']):
        raise ValueError('ZEC target trigger not verified')

    # Segregate existing capital at the remaining long's full entry notional.
    # The historical execution and funding remain unknown in the isolated book.
    reserve = p['entry'] * p['qty'] * p['rules']['multiplier']
    if not 0 < reserve < book['cash'] or not 0 <= p['rules']['taker'] <= 1:
        raise ValueError('invalid reserve or insufficient existing cash')
    event = dict(at=at, event='liability_isolated', experimental=True, arm=arm,
                 symbol=symbol, id=position_id, reason=f'legacy_ambiguous_{mode}',
                 disposition='UNRESOLVED_RESEARCH_LIABILITY_RESERVED_FROM_EXISTING_CASH',
                 reserved_cash=reserve, position=p, position_snapshot=original['position_snapshot'],
                 print_prices=sorted(prices), **{f'{mode}_trigger_ms': trigger_ms},
                 fill_price=None, filled_qty=None, funding_disposition='UNRESOLVED_ISOLATED',
                 quarantine_sequence=seq, quarantine_hash=event_hash,
                 evidence_key=item['key'], evidence_hash=item['stored_sha256'],
                 group_sha256=digest(sorted(group, key=lambda row: row['i'])),
                 prior_head=expected_head, prior_checkpoint_sha256=digest(saved),
                 cash_before=book['cash'], available_cash_after=book['cash'] - reserve)
    if apply:
        if backup:
            event['backup_name'] = backup_journal(journal, arm).name
        book.update(cash=book['cash'] - reserve, position=None, unresolved=None,
                    isolated_liability=event)
        # Keep the day baseline, the existing lock and the loss streak. The next
        # normal UTC risk check sizes only from the unreserved active cash.
        journal.commit([], [event], checkpoint.dumps(state))
    return event


def backup_journal(journal, arm):
    if shutil.disk_usage(journal.directory).free < journal.allocated_bytes() + 128_000_000:
        raise ValueError('insufficient space for a journal backup')
    folder = journal.directory / 'reconciliation-backups'
    folder.mkdir(exist_ok=True)
    path = folder / f'pre-arm-{arm.lower()}-{journal.sequence}.sqlite3'
    if not path.exists():
        temporary = path.with_suffix('.tmp')
        db = sqlite3.connect(temporary)
        try:
            journal.db.backup(db, pages=4096)
        finally:
            db.close()
        temporary.replace(path)
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if (db.execute('PRAGMA quick_check').fetchone() != ('ok',) or
                db.execute('SELECT head,sequence FROM checkpoint WHERE id=1').fetchone()
                != (journal.head, journal.sequence)):
            raise ValueError('journal backup verification failed')
    finally:
        db.close()
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--incident', choices=INCIDENTS, default='AVAX')
    parser.add_argument('--backup', action='store_true')
    parser.add_argument('--apply', action='store_true', help='append the recovery event and checkpoint')
    args = parser.parse_args()
    safety(os.environ)
    if not (Path(args.data_dir) / 'paper.sqlite3').is_file():
        parser.error('existing paper.sqlite3 required')
    journal = Journal(args.data_dir, max_bytes=4_000_000_000)
    try:
        print(canonical(recover(journal, args.expected_head, time.time(), apply=args.apply, incident=args.incident, backup=args.backup)))
    finally:
        journal.close()


if __name__ == '__main__':
    main()
