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
from phoenix_v2.recover_delta import (INCIDENTS, POSITION_ID, REASON, SYMBOL, TRIGGER_MS,
                                      XRP_QUARANTINE, XRP_REASON, recover, recover_xrp_on_startup)

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
    def test_xrp_isolation_requires_reconciled_evidence_and_keeps_avax_reserve(self):
        from test_feed import proof
        from phoenix_v2.model import Bar
        from dataclasses import asdict

        _, position_id, symbol, trigger, entry, stop, target, qty, _ = INCIDENTS['XRP']
        fee, funding, previous_reserve = D('.0036537600'), D('-.000666304'), D('6.9096')
        with tempfile.TemporaryDirectory() as path:
            journal = Journal(path)
            try:
                runtime = Runtime(journal, symbols=(symbol,), strategy_profile=PROFILE, delta_arms=True)
                book = runtime.state['delta_arms']['books']['D']
                book['isolated_liability'] = dict(id=POSITION_ID, reserved_cash=previous_reserve)
                book['cash'] = D(1000) - fee + funding - previous_reserve
                book['position'] = dict(id=position_id, symbol=symbol, entry=entry,
                    opened=1794524190.87, opened_received_at=1794524191.011, qty=qty, original_qty=qty,
                    stop=stop, target=target, fees=fee, funding=funding, realized=D(0), exit_mode=None,
                    mark=stop, rules=dict(multiplier=D(1), taker=D('.00055')))
                delta_paper.event(book, runtime.log, 'D', book['position']['opened'], 'entry_fill',
                                  qty=qty, price=entry, fee=fee)
                delta_paper.event(book, runtime.log, 'D', 1794528000, 'funding', cashflow=funding)
                delta_paper.quarantine(book, runtime.log, 'D', XRP_QUARANTINE, XRP_REASON)
                runtime.commit()
                with self.assertRaisesRegex(ValueError, 'retained public trades missing'):
                    recover(journal, journal.head, trigger / 1000 + 60, incident='XRP', apply=True)
                for stamp, price in ((XRP_QUARANTINE * 1000 + 51, D('1.0402')), (trigger, stop)):
                    start = stamp // 60000 * 60
                    bar = Bar(symbol, 1, start, price, price, price, price, D(1))
                    prior = Bar(symbol, 1, start - 60, price, price, price, price, D(1))
                    row = dict(T=stamp, p=str(price), v='1', S='Sell', i=str(stamp), s=symbol,
                               received_at=stamp / 1000 + .1)
                    runtime.commit([(f'candle:{symbol}:1:{start - 60}', 'candle', start, asdict(prior)),
                                    (f'candle:{symbol}:1:{start}', 'candle', start + 60, asdict(bar)),
                                    (f'trades:{symbol}:{start}', 'public_trades', start + 60, [row]),
                                    (f'native:{symbol}:{start}', 'confirmed_native_candle', start + 60, proof(bar))])
                history, financials = journal.page(table='paper_history'), journal.financials()
                head, sequence = journal.head, journal.sequence
                recover_xrp_on_startup(journal, trigger / 1000 + 60)
                event = journal.recent(('liability_isolated',))[0]
                recover_xrp_on_startup(journal, trigger / 1000 + 120)
                self.assertEqual(journal.sequence, sequence + 1)
                self.assertEqual(D(event['reserved_cash']), D('6.64320'))
                self.assertIsNone(event['filled_qty'])
                self.assertEqual(journal.db.execute('SELECT hash FROM events WHERE seq=?', (sequence,)).fetchone()[0], head)
                self.assertEqual(journal.financials(), financials)
                self.assertEqual(journal.page(table='paper_history'), history)
                state = checkpoint.loads(journal.load())['delta_arms']
                report = delta_paper.reports(state, financials, [symbol])['D']
                self.assertEqual([item['id'] for item in report['isolated_liabilities']], [POSITION_ID, position_id])
                self.assertEqual(report['cash'], D(1000) - fee + funding)
                self.assertEqual(report['available_cash'], report['cash'] - previous_reserve - D('6.64320'))
                self.assertFalse(report['locked'])
                self.assertIsNone(report['equity'])
                journal.verify()
            finally:
                journal.close()

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


class ZecTargetTests(unittest.TestCase):
    stamp = 1794481789240
    symbol = 'ZECUSDT'

    def setUp(self):
        self.state = delta_paper.new_state()
        self.book = self.state['books']['C']
        self.events = []
        self.book.update(cash=D('999.994566990'), position=dict(
            id='C:ZECUSDT:delta:1794480600', symbol=self.symbol, entry=D('987.82'),
            opened=1794480693.465, opened_received_at=1794480693.5875785,
            original_qty=D('.01'), qty=D('.01'), stop=D('986.45'), target=D('994.45'),
            mark=D('994.45'), atr=D('4.57'), fees=D('.005433010'), funding=D(0),
            realized=D(0), exit_mode=None, deadline=1794487893.465, planned_risk=D('.03'),
            rules=dict(tick=D('.01'), step=D('.01'), multiplier=D(1), taker=D('.00055'))))
        self.rows = [
            dict(T=self.stamp, s=self.symbol, S='Buy', i=str(i), p=price, v=size,
                 received_at=1794481789.3630235)
            for i, (price, size) in enumerate(zip(
                '994.56 994.58 994.59 994.60 994.64 994.72 994.72 994.73 994.74 994.76 994.76 994.80 994.80 994.84 994.84 994.84 994.85 994.87 994.87'.split(),
                '.01 .02 .03 .62 .02 .10 .10 .50 .02 .41 .68 .31 .01 .01 .02 .05 .71 .01 2.26'.split(),
            ))
        ]

    def log(self, at, event, **fields):
        self.events.append(dict(at=at, event=event, **fields))

    def execute(self, rows):
        delta_paper.execute(self.book, 'C', self.symbol, rows, self.stamp // 1000 + 60, None, self.log)

    def test_target_tie_uses_fixed_limit_and_per_print_cap_in_either_order(self):
        self.execute(self.rows)
        fill, close = self.events
        self.assertEqual((fill['price'], fill['qty'], fill['reason']), (D('994.45'), D('.01'), 'target'))
        self.assertEqual(fill['target_tie_policy'], 'fixed_price_per_print_cap_v1')
        self.assertEqual(self.book['cash'], D(1000) + close['net'])
        self.assertEqual(self.book['wins'], 1)
        self.setUp()
        self.execute(reversed(self.rows))
        self.assertEqual(self.events, [fill, close])

    def test_stale_or_sub_lot_target_tie_cannot_fill(self):
        self.execute(self.rows[:-1])
        self.assertEqual(self.events, [])
        self.execute([dict(row, received_at=self.stamp / 1000 + 3) for row in self.rows])
        self.assertEqual([event['event'] for event in self.events], ['liability_quarantined'])

    def test_ambiguous_time_exit_defers_until_price_is_unambiguous(self):
        self.book['position']['deadline'] = self.stamp / 1000
        self.execute(self.rows)
        self.assertEqual(self.events[0]['event'], 'time_execution_deferred')
        self.assertEqual(self.book['position']['exit_mode'], 'time_exit')
        row = dict(self.rows[-1], T=self.stamp + 1, i='later')
        self.execute(self.rows + [row])
        self.assertEqual([event['event'] for event in self.events],
                         ['time_execution_deferred', 'exit_fill', 'position_closed'])

    def test_legacy_zec_recovery_retains_trade_and_verifies_backup(self):
        with tempfile.TemporaryDirectory() as path:
            journal = Journal(path)
            try:
                runtime = Runtime(journal, symbols=(self.symbol,), strategy_profile=PROFILE, delta_arms=True)
                runtime.state['delta_arms'] = self.state
                position = self.book['position']
                delta_paper.event(self.book, runtime.log, 'C', position['opened'], 'entry_fill',
                                  qty=position['qty'], price=position['entry'], fee=position['fees'])
                delta_paper.quarantine(self.book, runtime.log, 'C', self.stamp / 1000, REASON)
                minute = self.stamp // 60000 * 60
                runtime.commit([(f'trades:{self.symbol}:{minute}', 'public_trades', minute + 60, self.rows)])
                history = journal.page(table='paper_history')
                event = recover(journal, journal.head, minute + 120, incident='ZEC', apply=True, backup=True)
                self.assertEqual(event['reserved_cash'], D('9.8782'))
                self.assertEqual(event['available_cash_after'], D('990.116366990'))
                self.assertTrue((Path(path) / 'reconciliation-backups' / event['backup_name']).is_file())
                self.assertEqual(journal.page(table='paper_history'), history)
                self.assertEqual(checkpoint.loads(journal.load())['delta_arms']['books']['D'],
                                 self.state['books']['D'])
                journal.verify()
            finally:
                journal.close()
