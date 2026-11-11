"""Synthetic evidence for the two additive, strictly paper-only arms."""
import tempfile
import unittest
from unittest.mock import patch

from phoenix_v2 import checkpoint, delta, delta_paper
from phoenix_v2.fast_strategy import PROFILE
from phoenix_v2.forward import PaperPolicy, Runtime
from phoenix_v2.journal import Journal
from phoenix_v2.model import D, canonical
from tests.test_forward import SYMBOL, bar, rule, trade


def public(at, price, size='1000', side='Sell', id_suffix='', lag=.1):
    row = trade(int(at * 1000), str(price), str(size), f'trade-{at}{id_suffix}', side)
    return dict(row, received_at=at + lag)


def intent(name='C'):
    return dict(id=f'{name}:{SYMBOL}:delta:3900', symbol=SYMBOL, tick=D('.01'),
                decision=3905, expires=3960 if name == 'C' else 4200, stop=D(97),
                target=D(110) if name == 'C' else None, trigger=None if name == 'C' else D(100),
                max_entry=D(101), atr=D(1))


class ProfileTests(unittest.TestCase):
    def test_profile_is_contiguous_and_delta_preserves_taker_side(self):
        bucket = delta.new_bucket(0, D(1))
        for price, size, side in [(100, 40, 0), (99, 20, 0), (101, 20, 1), (97, 5, 1)] * 3:
            self.assertTrue(delta.add_trades(bucket, [(D(price), D(size), side)]))
        reference = delta.profile(bucket)
        self.assertEqual((reference['low'], reference['poc'], reference['high']), (99, 100, 100))
        self.assertEqual(delta.seller_levels(reference, reference['volume']), [(101, D(60)), (97, D(15))])

    def test_each_setup_uses_its_own_flow_pattern(self):
        reference = dict(width=D(1), poc=105, low=100, volume=D(100),
                         levels={98: [D(0), D(10), 3], 97: [D(0), D(2), 3]})
        prior = [dict(high=D(110), close=D(100), delta_fraction=D('.3'))] * 3
        reversal = dict(low=D('98.5'), high=D('101'), close=D('99.5'), levels={})
        breakout = dict(low=D(108), high=D('110.8'), close=D('110.5'),
                        levels={110: [D(1), D(12), 10]})
        c, d = delta.evaluate(reference, reversal, prior, D('.01')).values()
        self.assertEqual(c[1]['target'], D('105.5'))
        self.assertIsNone(d[1])
        c, d = delta.evaluate(reference, breakout, prior, D('.01')).values()
        self.assertIsNone(c[1])
        self.assertEqual(d[1]['trigger'], D('111.01'))
        prior[-1] = dict(prior[-1], delta_fraction=D('-.3'))
        self.assertIsNone(delta.evaluate(reference, breakout, prior, D('.01'))['D'][1])

    def test_profiles_require_complete_trade_coverage(self):
        state = delta.market()
        for minute in range(60):
            start = minute * 60
            rows = [public(start + t, 100) for t in (1, 2, 3)]
            _, evaluation = delta.ingest(state, SYMBOL, start + 60, rows, None, D('.01'), start + 61)
            self.assertIsNone(evaluation)
        self.assertEqual(state['reference']['count'], 180)
        for minute in range(60, 65):
            start = minute * 60
            rows = [public(start + t, 100) for t in range(1, 9)]
            _, evaluation = delta.ingest(state, SYMBOL, start + 60, rows, None, D('.01'), start + 61)
        self.assertIsNotNone(evaluation)
        reason, _ = delta.ingest(state, SYMBOL, 3960, [], 'stream_disconnected', D('.01'), 3961)
        self.assertEqual(reason, 'stream_disconnected')
        self.assertIsNone(state['reference'])

    def test_invalid_future_duplicate_and_unsigned_data_fails_closed(self):
        good = public(3601, 100)
        self.assertIsNone(delta.validate_rows(SYMBOL, 3600, [good], 3660))
        for rows in ([good, good], [dict(good, S='')], [dict(good, received_at=3661)],
                     [dict(good, received_at=None)], [dict(good, T=3660000)]):
            self.assertEqual(delta.validate_rows(SYMBOL, 3600, rows, 3660), 'invalid_trade_provenance')


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.state = delta_paper.new_state()
        self.events = []
        self.policy = PaperPolicy({SYMBOL: [rule()]})

    def log(self, at, event, **fields):
        self.events.append(dict(at=at, event=event, **fields))

    def execute(self, name, rows, end=3960):
        book = self.state['books'][name]
        delta_paper.execute(book, name, SYMBOL, rows, end, self.policy, self.log)
        return book

    def test_d_requires_post_decision_stop_cross_and_each_arm_is_independent(self):
        c, d = self.state['books'].values()
        c['pending'], d['pending'] = intent(), intent('D')
        self.execute('C', [public(3904, 100), public(3906, 100)])
        self.assertEqual(c['position']['opened'], 3906)
        self.assertLess(c['cash'], 1000)
        self.assertEqual(d['cash'], 1000)
        self.execute('D', [public(3904, 101), public(3907, '99.99')])
        self.assertIsNone(d['position'])
        self.execute('D', [public(3961, 100)], 4020)
        self.assertEqual(d['position']['opened'], 3961)
        self.assertLessEqual(d['position']['planned_risk'], D(5))
        self.assertGreater(d['position']['entry'], D(100))

    def test_stale_and_ambiguous_entries_never_fill(self):
        for rows, reason in (([public(3906, 100, lag=3)], 'stale_entry_print'),
                             ([public(3906, 100), public(3906, 101, id_suffix='-2')], 'ambiguous_entry_print')):
            with self.subTest(reason=reason):
                self.state['books']['C']['pending'] = intent()
                book = self.execute('C', rows)
                self.assertIsNone(book['position'])
                self.assertEqual(book['cash'], D(1000))
                self.assertEqual(self.events[-1]['reason'], reason)

    def test_fee_bearing_stop_and_five_loss_lock_survive_restart(self):
        book = self.state['books']['C']
        book['streak'] = 4
        book['pending'] = intent()
        self.execute('C', [public(3906, 100), public(3907, 97)])
        self.assertIsNone(book['position'])
        self.assertEqual(book['streak'], 5)
        self.assertGreater(self.events[-2]['fee'], 0)
        restored = checkpoint.loads(checkpoint.dumps(self.state))['books']['C']
        delta_paper.risk(restored, 86400)
        self.assertTrue(delta_paper.locked(restored))

    def test_daily_loss_lock_rejects_entry_and_resets_at_utc_boundary(self):
        book = self.state['books']['C']
        book.update(day=0, day_start=D(1000), cash=D(980), pending=intent())
        self.execute('C', [public(3906, 100)])
        self.assertEqual(self.events[-1]['reason'], 'risk_locked')
        self.assertTrue(book['day_locked'])
        delta_paper.risk(book, 86400)
        self.assertFalse(book['day_locked'])
        self.assertEqual(book['day_start'], D(980))

    def test_gap_retains_held_liability_and_cancels_other_intent(self):
        self.state['books']['C']['pending'] = intent()
        book = self.execute('C', [public(3906, 100)])
        self.state['books']['D']['pending'] = intent('D')
        delta_paper.invalidate(self.state, [SYMBOL], 3960, 'stream_disconnected', self.log)
        self.execute('C', [public(3961, 110)], 4020)
        self.assertEqual(book['unresolved'], 'stream_disconnected')
        self.assertIsNotNone(book['position'])
        self.assertIsNone(self.state['books']['D']['pending'])
        self.assertIsNone(delta_paper.reports(self.state, {}, [SYMBOL])['C']['equity'])


class RuntimeTests(unittest.TestCase):
    def test_reclaim_uses_completed_footprint_then_a_later_print(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                runtime = Runtime(journal, strategy_profile=PROFILE, delta_arms=True)
                runtime.state['contracts'] = {SYMBOL: [rule()]}
                runtime.bind()
                runtime.state['previous'] = 0
                runtime.state['strategies'][SYMBOL].frames[5].atr.value = D(1)
                for start in range(0, 4260, 60):
                    if start < 3600:
                        prices = [D('105.5')] * 10 + [D(99)] * 4
                    elif start < 3900:
                        prices = [D('99.5')] * 6
                    elif start < 4200:
                        prices = [D('99.5'), D(99)] + [D('99.5')] * 4
                    else:
                        prices = [D('99.5')] * 6
                    rows = [public(start + i + 2, p, side='Sell' if p == 99 else 'Buy')
                            for i, p in enumerate(prices)]
                    candle = bar(start, o=str(prices[0]), h=str(max(prices)), l=str(min(prices)),
                                 c=str(prices[-1]), v=str(1000 * len(prices)))
                    with patch('phoenix_v2.forward.time.time', return_value=start + 61):
                        runtime.minute(start + 60, {SYMBOL: [candle]}, {SYMBOL: candle}, {SYMBOL: (rows, None)}, {})
                    if start == 4140:
                        c = runtime.state['delta_arms']['books']['C']
                        self.assertEqual(c['pending']['decision'], 4201)
                        self.assertIsNone(c['position'])
                self.assertEqual(c['position']['opened'], 4202)
                self.assertEqual(runtime.state['arms']['A'].cash, D(1000))
                self.assertEqual(runtime.state['arms']['B'].cash, D(1000))
            finally:
                journal.close()

    def test_upgrade_preserves_ab_and_projects_new_fills_and_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                base = Runtime(journal, strategy_profile=PROFILE)
                base.state['arms']['A'].cash = D('987.65')
                base.state['arms']['B'].reset(3900)
                base.state['arms']['B'].streak = 3
                base.state['contracts'] = {SYMBOL: [rule()]}
                base.state['previous'] = 3900
                base.bind()
                base.commit()
                previous_head = journal.head
                runtime = Runtime(journal, strategy_profile=PROFILE, delta_arms=True)
                self.assertEqual(journal.head, previous_head)
                def books(state):
                    return canonical({name: {k: v for k, v in vars(arm).items() if k not in ('log', 'manifest')}
                                      for name, arm in state['arms'].items()})
                self.assertEqual(books(runtime.state), books(base.state))
                c = runtime.state['delta_arms']['books']['C']
                c['pending'] = intent()
                rows = [public(3906, 100), public(3907, 97)]
                with patch('phoenix_v2.forward.time.time', return_value=3961):
                    runtime.minute(3960, {SYMBOL: [bar(3900, h='100', l='97', c='97', v='2000')]},
                        {SYMBOL: bar(3900)}, {SYMBOL: (rows, None)}, {})
                page = journal.page(table='paper_history', arm='C')['items']
                self.assertEqual(len(page), 1)
                self.assertEqual(page[0]['state'], 'CLOSED')
                self.assertEqual(page[0]['exit_reason'], 'stop')
                self.assertLess(D(page[0]['net_pnl']), 0)
                self.assertEqual(journal.page(table='paper_history', arm='D')['items'], [])
                self.assertTrue(all(row['arm'] == 'C' for row in journal.page(arm='C')['items']))
                after = Runtime(journal, strategy_profile=PROFILE, delta_arms=True)
                self.assertEqual(after.state['arms']['A'].cash, D('987.65'))
                self.assertEqual(after.state['arms']['B'].streak, 3)
                self.assertEqual(after.state['delta_arms']['books']['C']['losses'], 1)
                self.assertEqual(after.state['delta_arms']['books']['D']['cash'], D(1000))
                self.assertEqual(journal.counts['delta_arms_initialized'], 1)
            finally:
                journal.close()
