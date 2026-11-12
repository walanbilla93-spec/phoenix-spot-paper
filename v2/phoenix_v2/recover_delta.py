"""Offline, one-incident recovery for the legacy Arm D AVAX quarantine."""
import argparse
import json
import os
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


def recover(journal, expected_head, at, *, apply=False):
    if journal.head != expected_head:
        raise ValueError('journal head changed; inspect the current checkpoint and retry')
    saved = journal.load()
    if saved is None:
        raise ValueError('checkpoint missing')
    state = checkpoint.loads(saved)
    book = state['delta_arms']['books']['D']
    if book.get('isolated_liability'):
        raise ValueError('legacy liability already isolated')
    p = book['position']
    if (book['unresolved'] != REASON or book['pending'] is not None or not p
            or p['id'] != POSITION_ID or p['symbol'] != SYMBOL or p['entry'] != D('8.637')
            or p['stop'] != D('8.595') or p['target'] != D('8.760')
            or p['original_qty'] != D('.8') or p['qty'] != D('.8')
            or p['exit_mode'] is not None or p['realized'] != 0):
        raise ValueError('checkpoint does not match the exact unresolved AVAX incident')

    quarantines = journal.db.execute(
        "SELECT seq,payload,hash FROM events WHERE kind='liability_quarantined' AND arm='D' AND at=?",
        (TRIGGER_MS / 1000,),
    ).fetchall()
    matches = [(seq, item, event_hash) for seq, payload, event_hash in quarantines
               if (item := json.loads(payload)).get('id') == POSITION_ID and item.get('reason') == REASON]
    if len(matches) != 1:
        raise ValueError('unique original quarantine event missing')
    seq, original, event_hash = matches[0]
    if canonical(delta_paper.position_snapshot(p)) != canonical(original.get('position_snapshot')):
        raise ValueError('checkpoint economics differ from original quarantine snapshot')
    totals = journal.financials().get('D', {})
    if (not {'fees', 'funding', 'realized_gross'} <= totals.keys()
            or book['cash'] != delta_paper.CAPITAL + number(totals['realized_gross'])
            - number(totals['fees']) + number(totals['funding'])):
        raise ValueError('Arm D cash does not reconcile to the journal')

    minute = TRIGGER_MS // 60000 * 60
    evidence = journal.minute_evidence(SYMBOL, minute)
    retained = [item for item in evidence['items'] if item['kind'] == 'public_trades']
    if len(retained) != 1:
        raise ValueError('retained public trades missing')
    item = retained[0]
    rows = item['value']
    if delta_paper.delta.validate_rows(SYMBOL, minute, rows, minute + 180):
        raise ValueError('invalid retained public trade provenance')
    group = [row for row in rows if row['T'] == TRIGGER_MS]
    prices = {number(row['p']) for row in group}
    if (not {D('8.598'), D('8.597'), D('8.596'), D('8.595')} <= prices
            or not delta_paper.protection_fresh(p, group)):
        raise ValueError('fresh mixed-price stop evidence not verified')

    # Segregate existing capital at the remaining long's full entry notional.
    # The historical execution and funding remain unknown in the isolated book.
    reserve = p['entry'] * p['qty'] * p['rules']['multiplier']
    if not 0 < reserve < book['cash'] or not 0 <= p['rules']['taker'] <= 1:
        raise ValueError('invalid reserve or insufficient existing cash')
    event = dict(at=at, event='liability_isolated', experimental=True, arm='D',
                 symbol=SYMBOL, id=POSITION_ID, reason='legacy_ambiguous_stop',
                 disposition='UNRESOLVED_RESEARCH_LIABILITY_RESERVED_FROM_EXISTING_CASH',
                 reserved_cash=reserve, position=p, position_snapshot=original['position_snapshot'],
                 stop_trigger_ms=TRIGGER_MS, print_prices=sorted(prices),
                 fill_price=None, filled_qty=None, funding_disposition='UNRESOLVED_ISOLATED',
                 quarantine_sequence=seq, quarantine_hash=event_hash,
                 evidence_key=item['key'], evidence_hash=item['stored_sha256'],
                 group_sha256=digest(sorted(group, key=lambda row: row['i'])),
                 prior_head=expected_head, prior_checkpoint_sha256=digest(saved),
                 cash_before=book['cash'], available_cash_after=book['cash'] - reserve)
    if apply:
        book.update(cash=book['cash'] - reserve, position=None, unresolved=None,
                    isolated_liability=event)
        # Keep the day baseline, the existing lock and the loss streak. The next
        # normal UTC risk check sizes only from the unreserved active cash.
        journal.commit([], [event], checkpoint.dumps(state))
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--apply', action='store_true', help='append the recovery event and checkpoint')
    args = parser.parse_args()
    safety(os.environ)
    if not (Path(args.data_dir) / 'paper.sqlite3').is_file():
        parser.error('existing paper.sqlite3 required')
    journal = Journal(args.data_dir)
    try:
        print(canonical(recover(journal, args.expected_head, time.time(), apply=args.apply)))
    finally:
        journal.close()


if __name__ == '__main__':
    main()
