"""Regression for the retained AVAX stop tie and its legacy checkpoint."""
import json
from pathlib import Path
import tempfile
import unittest

from phoenix_v2 import checkpoint, delta_paper
from phoenix_v2.fast_strategy import PROFILE
from phoenix_v2.forward import Runtime
from phoenix_v2.journal import Journal
from phoenix_v2.model import D, canonical
from phoenix_v2.recover_delta import POSITION_ID, REASON, SYMBOL, TRIGGER_MS, recover

AT = TRIGGER_MS / 1000
MINUTE = TRIGGER_MS // 60000 * 60
TIE = json.loads(Path(__file__).with_name('fixtures').joinpath('avax_stop_tie_20261112.json').read_text())
FEE = D('.003800280')
FUNDING = D('-.00006082272')
CASH = D(1000) - FEE + FUNDING


def incident(book):
    book.update(cash=CASH, day=int(AT) // 86400, position=dict(
        id=POSITION_ID, symbol=SYMBOL, entry=D('8.637'), opened=1794467971.295,
        opened_received_at=1794467971.4836268, qty=D('.8'), original_qty=D('.8'),
        stop=D('8.595'), target=D('8.760'), mark=D('8.609'),
        rules=dict(tick=D('.001'), step=D('.1'), multiplier=D(1), taker=D('.00055'),
                   funding_minutes=480),
        atr=D('.02768838741211450521767087727'), fees=FEE, funding=FUNDING,
        realized=D(0), exit_mode=None, deadline=1794475171.295, planned_risk=D('.045179880')))


def later(stamp, price, size):
    return dict(TIE[0], T=stamp, p=price, v=size, i=f'later-{stamp}', received_at=stamp / 1000 + .1)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.state = delta_paper.new_state()
        incident(self.book)
        self.events = []

    @property
    def book(self):
        return self.state['books']['D']

    def log(self, at, event, **fields):
        self.events.append(dict(at=at, event=event, **fields))

    def run_rows(self, rows):
        delta_paper.execute(self.book, 'D', SYMBOL, rows, MINUTE + 60, None, self.log)

    def test_exact_tie_confirms_stop_without_using_any_tied_volume_or_order(self):
        self.run_rows(TIE)
        trigger, = self.events
        self.assertEqual(trigger['event'], 'stop_trigger_confirmed')
        self.assertEqual(trigger['stop_trigger_ms'], TRIGGER_MS)
        self.assertEqual(trigger['filled_qty'], 0)
        self.assertEqual(trigger['print_prices'], list(map(D, ('8.595', '8.596', '8.597', '8.598'))))
        self.assertEqual(self.book['cash'], CASH)
        self.assertEqual(self.book['position']['qty'], D('.8'))
        self.assertFalse(delta_paper.locked(self.book))
        self.setUp()
        self.run_rows(reversed(TIE))
        self.assertEqual(self.events, [trigger])

    def test_partial_fills_and_funding_survive_restoration(self):
        self.run_rows(TIE + [later(TRIGGER_MS + 1, '8.594', '30'), later(TRIGGER_MS + 2, '8.598', '9')])
        self.assertEqual(self.book['position']['qty'], D('.5'))
        partial = self.events[-1]
        self.assertEqual((partial['price'], partial['qty']), (D('8.589'), D('.3')))
        settlement = (int(AT) // 28800 + 1) * 28800
        delta_paper.settle(self.book, 'D', settlement, D('8.600'), {'fundingRate': '.001'}, self.log)
        flow = -D('.5') * D('8.600') * D('.001')
        self.assertEqual(self.events[-1]['cashflow'], flow)
        self.state = checkpoint.loads(checkpoint.dumps(self.state))
        self.run_rows(TIE + [later(settlement * 1000 + 1, '8.650', '100')])
        close = self.events[-1]
        fills = [e for e in self.events if e['event'] == 'exit_fill']
        gross = sum((e['price'] - D('8.637')) * e['qty'] for e in fills)
        fees = FEE + sum(e['fee'] for e in fills)
        self.assertEqual([e['qty'] for e in fills], [D('.3'), D('.5')])
        self.assertTrue(all(e['reason'] == 'stop' and e['stop_trigger_ms'] == TRIGGER_MS for e in fills))
        self.assertEqual(close['net'], gross - fees + FUNDING + flow)
        self.assertEqual(self.book['cash'], D(1000) + close['net'])
        self.assertEqual((self.book['losses'], self.book['streak']), (1, 1))
        self.assertIsNone(self.book['position'])
        self.assertEqual(sum(e['event'] == 'stop_trigger_confirmed' for e in self.events), 1)

    def test_stale_protection_and_feed_gaps_keep_unresolved_residual(self):
        self.run_rows([dict(row, received_at=AT + 3) for row in TIE])
        self.assertEqual(self.book['unresolved'], REASON)
        self.assertEqual([e['event'] for e in self.events], ['liability_quarantined'])
        self.setUp()
        self.run_rows(TIE + [later(TRIGGER_MS + 1, '8.594', '30')])
        delta_paper.invalidate(self.state, [SYMBOL], MINUTE + 60, 'stream_disconnected', self.log)
        self.state = checkpoint.loads(checkpoint.dumps(self.state))
        self.run_rows([later(TRIGGER_MS + 60000, '8.570', '100')])
        self.assertEqual(self.book['position']['qty'], D('.5'))
        self.assertEqual(self.book['unresolved'], 'stream_disconnected')
        self.assertTrue(delta_paper.locked(self.book))

    def test_stop_precedes_same_millisecond_target_and_later_mixed_group_cannot_fill(self):
        self.run_rows(TIE + [later(TRIGGER_MS, '8.761', '100')] + [
            later(TRIGGER_MS + 1, '8.594', '100'),
            dict(later(TRIGGER_MS + 1, '8.593', '100'), i='later-mixed'),
        ])
        self.assertEqual([e['event'] for e in self.events],
                         ['stop_trigger_confirmed', 'stop_execution_deferred'])
        self.assertEqual(self.book['position']['qty'], D('.8'))
        self.assertEqual(self.book['position']['exit_mode'], 'stop')
        stale = later(TRIGGER_MS + 2, '8.591', '100')
        stale['received_at'] += 3
        self.run_rows([stale])
        self.assertEqual(self.book['unresolved'], REASON)
        self.assertEqual(self.book['cash'], CASH)


class RecoveryTests(unittest.TestCase):
    def test_append_only_recovery_preserves_history_risk_and_independent_books(self):
        with tempfile.TemporaryDirectory() as path:
            journal = Journal(path)
            try:
                self.check_recovery(journal)
            finally:
                journal.close()

    def check_recovery(self, journal):
        runtime = Runtime(journal, symbols=(SYMBOL,), strategy_profile=PROFILE, delta_arms=True)
        delta = runtime.state['delta_arms']
        book = delta['books']['D']
        incident(book)
        p = book['position']
        p['funding'] = D(0)
        delta_paper.event(book, runtime.log, 'D', p['opened'], 'entry_fill',
                          qty=p['qty'], price=p['entry'], fee=FEE)
        p['funding'] = FUNDING
        delta_paper.event(book, runtime.log, 'D', 1794470400, 'funding', cashflow=FUNDING)
        delta_paper.quarantine(book, runtime.log, 'D', AT, REASON)
        book.update(day_locked=True, streak=5)
        runtime.commit([(f'trades:{SYMBOL}:{MINUTE}', 'public_trades', MINUTE + 60, TIE)])
        head, seq, before = journal.head, journal.sequence, journal.load()
        finances, history = journal.financials(), journal.page(table='paper_history')
        with self.assertRaisesRegex(ValueError, 'head changed'):
            recover(journal, '0' * 64, MINUTE + 120, apply=True)
        recovery = recover(journal, head, MINUTE + 120)
        self.assertEqual(journal.load(), before)
        self.assertEqual(recovery['reserved_cash'], D('6.9096'))
        self.assertIsNone(recovery['fill_price'])
        recover(journal, head, MINUTE + 120, apply=True)
        self.assertEqual(journal.sequence, seq + 1)
        self.assertEqual(journal.db.execute('SELECT hash FROM events WHERE seq=?', (seq,)).fetchone()[0], head)
        journal.verify()
        self.assertEqual(journal.financials(), finances)
        self.assertEqual(journal.page(table='paper_history'), history)
        restored = Runtime(journal, symbols=(SYMBOL,), strategy_profile=PROFILE, delta_arms=True)
        after = restored.state['delta_arms']
        self.assertEqual(canonical(after['books']['C']), canonical(delta['books']['C']))
        for name in ('A', 'B'):
            fields = lambda arm: {k: v for k, v in vars(arm).items() if k not in ('log', 'manifest')}
            self.assertEqual(fields(restored.state['arms'][name]), fields(runtime.state['arms'][name]))
        d = after['books']['D']
        self.assertIsNone(d['position'])
        self.assertEqual(d['cash'], CASH - D('6.9096'))
        self.assertTrue(d['day_locked'])
        self.assertEqual(d['streak'], 5)
        self.assertTrue(delta_paper.locked(d))
        self.assertEqual(d['wins'] + d['losses'], 0)
        report = delta_paper.reports(after, finances, [SYMBOL])['D']
        self.assertEqual(report['cash'], CASH)
        self.assertEqual(report['available_equity'], d['cash'])
        self.assertIsNone(report['equity'])
        with self.assertRaisesRegex(ValueError, 'already isolated'):
            recover(journal, journal.head, MINUTE + 180, apply=True)
