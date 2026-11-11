import tempfile
import unittest

from phoenix_v2.fast_strategy import FastStrategy, PROFILE
from phoenix_v2.forward import Runtime
from phoenix_v2.journal import Journal
from phoenix_v2.model import D
from phoenix_v2.universe import ORIGINAL_SYMBOLS, REMOVED, SYMBOLS


class MarketRotationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = Journal(self.directory.name)
        self.runtime = Runtime(self.journal, ORIGINAL_SYMBOLS, PROFILE, delta_arms=True)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_rotation_preserves_books_checkpoint_and_evidence(self):
        state = self.runtime.state
        state['previous'] = 4200
        state['arms']['A'].cash = D('997.125')
        state['arms']['B'].streak = 3
        state['arms']['A'].marks = {'BTCUSDT': D(100), 'BNBUSDT': D(500)}
        state['strategies']['BTCUSDT'].last_price = D(100)
        state['delta_arms']['books']['C']['cash'] = D('991.25')
        state['delta_arms']['books']['D']['streak'] = 4
        state['delta_arms']['markets'] = {'BTCUSDT': {'last': 4200}, 'BNBUSDT': {'last': 4200}}
        self.runtime.commit([('trades:BNBUSDT:4140', 'public_trades', 4200, [])])
        prior_head = self.journal.head
        migrated = Runtime(self.journal, SYMBOLS, PROFILE, delta_arms=True).state
        self.assertEqual(len(SYMBOLS), 12)
        self.assertEqual(set(ORIGINAL_SYMBOLS) - set(SYMBOLS), REMOVED)
        self.assertEqual(set(SYMBOLS) - set(ORIGINAL_SYMBOLS),
                         {'ZECUSDT', 'PUMPFUNUSDT', 'NEARUSDT', 'ENAUSDT'})
        self.assertEqual(migrated['previous'], 4200)
        self.assertEqual(migrated['arms']['A'].cash, D('997.125'))
        self.assertEqual(migrated['arms']['B'].streak, 3)
        self.assertEqual(migrated['arms']['A'].marks, {'BTCUSDT': D(100)})
        self.assertEqual(migrated['strategies']['BTCUSDT'].last_price, D(100))
        self.assertIsInstance(migrated['strategies']['ZECUSDT'], FastStrategy)
        self.assertEqual(migrated['delta_arms']['books']['C']['cash'], D('991.25'))
        self.assertEqual(migrated['delta_arms']['books']['D']['streak'], 4)
        self.assertEqual(migrated['delta_arms']['markets'], {'BTCUSDT': {'last': 4200}})
        event = self.journal.recent(('universe_rotated',))[0]
        self.assertEqual(event['journal_head_before'], prior_head)
        self.assertEqual(self.journal.evidence_counts['universe_rotation_checkpoint'], 1)
        self.assertEqual(len(self.journal.minute_evidence('BNBUSDT', 4140)['items']), 1)
        self.assertFalse(Runtime(self.journal, SYMBOLS, PROFILE, delta_arms=True).events)
        self.journal.verify()

    def test_refuses_unsettled_exposure_in_a_removed_market(self):
        self.runtime.state['delta_arms']['books']['D']['position'] = {'symbol': 'UNIUSDT'}
        self.runtime.commit()
        head = self.journal.head
        with self.assertRaisesRegex(ValueError, 'flat retired markets'):
            Runtime(self.journal, SYMBOLS, PROFILE, delta_arms=True)
        self.assertEqual(self.journal.head, head)
        self.assertNotIn('universe_rotation_checkpoint', self.journal.evidence_counts)

    def test_reverse_rotation_keeps_latest_ledger(self):
        self.runtime.commit()
        rotated = Runtime(self.journal, SYMBOLS, PROFILE, delta_arms=True)
        rotated.state['arms']['A'].cash = D('1001.50')
        rotated.commit()
        old = Runtime(self.journal, ORIGINAL_SYMBOLS, PROFILE, delta_arms=True)
        self.assertEqual(old.state['arms']['A'].cash, D('1001.50'))
        self.assertEqual(tuple(old.state['symbols']), ORIGINAL_SYMBOLS)
        self.assertEqual(self.journal.counts['universe_rotated'], 2)
