import unittest,pickle
from phoenix_v2.strategy import Strategy
from phoenix_v2.diagnostics import entry_context
from phoenix_v2.objects import Zone
from phoenix_v2.model import D

class EntryContextTests(unittest.TestCase):
 def test_read_only_summary_does_not_create_a_signal_or_mutate_context(self):
  s=Strategy('SOLUSDT',None);s.zones=[Zone('poi',15,1,'OB',D(10),D(11),0,100,90,visits=1,inside=True)]
  before=pickle.dumps(s);d=entry_context(s,300,'trade_ohlcv_mismatch')
  self.assertEqual(pickle.dumps(s),before);self.assertEqual(d['live_pois'],1)
  self.assertEqual(d['active_reclaims'],0);self.assertIn('higher_timeframe_bias_neutral',d['waiting_for'])
  self.assertIn('feed_quarantined',d['waiting_for']);self.assertEqual(d['visited_pois_in_bias'],0)
 def test_unavailable_invalid_and_wrong_timeframe_pois_not_presented_as_live(self):
  s=Strategy('1000PEPEUSDT',None)
  s.zones=[Zone('future',15,1,'OB',D(1),D(2),0,400,90),Zone('five',5,1,'OB',D(1),D(2),0,100,90),Zone('invalid',60,1,'OB',D(1),D(2),0,100,90,valid=False)]
  d=entry_context(s,300);self.assertEqual(d['live_pois'],0);self.assertIn('no_live_15m_1h_poi',d['waiting_for'])
