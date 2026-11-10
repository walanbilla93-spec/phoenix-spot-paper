"""Exact order-independence proofs; synthetic cases never create market fills."""
import copy,itertools,threading,unittest
from dataclasses import asdict
from phoenix_v2.forward import Runtime
from phoenix_v2.strategy import Strategy
from phoenix_v2.structure import Pivot
from phoenix_v2.objects import Zone,Pool
from phoenix_v2.model import D
from phoenix_v2.bybit_public import TradeStream,TRADE_BUFFER_LIMIT

class TieRegionTests(unittest.TestCase):
 def runtime(self,s):
  r=object.__new__(Runtime);r.state=dict(strategies={s.symbol:s},pending=[],arms={});return r
 def rows(self,prices):return [dict(T=360123,p=str(p)) for p in prices]
 def test_all_accepted_zone_regions_are_order_independent_and_idempotent(self):
  checked=0
  for side,kind,visits,inside,first,separation in itertools.product((1,-1),('OB','FVG'),(0,1,2),(False,True),(False,True),(0,2)):
   z=Zone('z',15,side,kind,D(100),D(110),0,300,300,visits=visits,inside=inside,first_eligible=first,separation=separation)
   s=Strategy('SOLUSDT',None);s.zones=[z];r=self.runtime(s)
   for a,b in itertools.combinations_with_replacement(map(D,('99','100','101','104','105','106','109','110','111')),2):
    if r.ambiguous_ties(s.symbol,self.rows((a,b))):continue
    x=copy.deepcopy(z);y=copy.deepcopy(z)
    for p in (a,b):x.update(p)
    for p in (b,a):y.update(p)
    self.assertEqual(asdict(x),asdict(y));before=asdict(x);x.update(b);self.assertEqual(asdict(x),before);checked+=1
  self.assertGreater(checked,1000)
 def test_first_visit_same_region_is_retained_but_threshold_crossing_is_not(self):
  s=Strategy('SOLUSDT',None);s.zones=[Zone('z',15,1,'OB',D(100),D(110),0,300,300)];r=self.runtime(s)
  self.assertFalse(r.ambiguous_ties(s.symbol,self.rows(('105.25','106.25'))))
  for pair in (('104','106'),('99','101'),('109','111'),('105','106')):
   self.assertTrue(r.ambiguous_ties(s.symbol,self.rows(pair)))
 def test_pool_hits_and_existing_sweeps_remain_ambiguous(self):
  s=Strategy('SOLUSDT',None);r=self.runtime(s);p=Pivot('p',-1,D(106),0,300)
  s.liquidity.pools=[Pool('p',15,-1,[p],300)]
  self.assertTrue(r.ambiguous_ties(s.symbol,self.rows(('105.25','105.75'))))
  s.liquidity.pools=[];s.sweeps={1:object()}
  self.assertTrue(r.ambiguous_ties(s.symbol,self.rows(('108','109'))))
 def test_unique_time_sweep_before_safe_tie_has_same_state_in_both_orders(self):
  s=Strategy('SOLUSDT',None)
  for f in s.frames.values():f.atr.count=300;f.atr.value=D(10)
  s.frames[240].regime=1;s.frames[240].leg=(Pivot('a',-1,D(90),0,0),Pivot('b',1,D(120),0,0))
  s.zones=[Zone('z',15,1,'OB',D(100),D(110),0,300,300)]
  p=Pivot('pool',-1,D(106),0,300);s.liquidity.pools=[Pool('pool',15,-1,[p],300)]
  rows=[dict(T=360122,p='105.5')]+self.rows(('108','109'));self.assertFalse(self.runtime(s).ambiguous_ties(s.symbol,rows));out=[]
  for group in itertools.permutations(rows[1:]):
   x=copy.deepcopy(s);logs=[];x.log=lambda *a,**k:logs.append((a,k))
   for row in [rows[0]]+list(group):x.price(D(row['p']),row['T']/1000)
   self.assertEqual(len(x.sweeps),1)
   out.append(dict(zones=[asdict(z) for z in x.zones],sweeps=[asdict(v) for v in x.sweeps.values()],logs=logs))
  self.assertEqual(out[0],out[1])
 def test_old_global_ceiling_no_longer_discards_but_new_ceiling_still_does(self):
  s=TradeStream(('SOLUSDT',),threading.Event());s.buffered=60000
  row=dict(T=300001,s='SOLUSDT',S='Buy',p='100',v='1',i='one',seq=1)
  s.add([row],received=301);self.assertEqual(s.total,1);self.assertEqual(s.overflow,0)
  s.buffered=TRADE_BUFFER_LIMIT;s.add([dict(row,i='two')],received=301)
  self.assertEqual(s.overflow,1);self.assertEqual(s.bad[('SOLUSDT',300)],'trade_buffer_overflow')
