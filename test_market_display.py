import copy,io,json,unittest,urllib.parse
from unittest.mock import patch
from phoenix import PublicSpot
from test_phoenix import T,S
import service_v2 as app

class MarketDisplayTest(unittest.TestCase):
 def sample(self):
  return dict(category='spot',list=[dict(symbol=S,lastPrice='98',prevPrice24h='100',price24hPcnt='-0.02'),
   dict(symbol='AVAXUSDT',lastPrice='25',prevPrice24h='20',price24hPcnt='0.25'),
   dict(symbol='NOTAPPROVEDUSDT',lastPrice='1',prevPrice24h='1',price24hPcnt='0')])
 def test_rolling_change_units_and_approved_spot_only(self):
  q=app.parse_tickers(self.sample(),dict(received_at=T,server_at=T-30))
  self.assertEqual(q[S]['last_price'],98);self.assertEqual(q[S]['change_24h_pct'],-2)
  self.assertEqual(q['AVAXUSDT']['change_24h_pct'],25);self.assertNotIn('NOTAPPROVEDUSDT',q)
  with self.assertRaises(ValueError):app.parse_tickers(dict(self.sample(),category='linear'),dict(received_at=T,server_at=T))
 def test_bad_or_missing_price_is_unavailable_not_zero(self):
  for value in ('',None,'NaN','Infinity','0','-1'):
   r=self.sample();r['list'][0]['lastPrice']=value
   q=app.parse_tickers(r,dict(received_at=T,server_at=T))
   self.assertEqual(q[S]['status'],'unavailable');self.assertNotIn('last_price',q[S])
 def test_timestamp_and_cached_snapshot_staleness(self):
  for ts in (None,T+1,T-30001):
   with self.assertRaises(ValueError):app.parse_tickers(self.sample(),dict(received_at=T,server_at=ts))
  q=app.parse_tickers(self.sample(),dict(received_at=T,server_at=T));snapshot=dict(symbols=q,status='ok')
  fresh=app.market_view(snapshot,[S,'XMRUSDT'],T+1000)
  self.assertFalse(fresh[S]['stale']);self.assertTrue(fresh['XMRUSDT']['stale'])
  stale=app.market_view(snapshot,[S],T+30001)[S]
  self.assertTrue(stale['stale']);self.assertEqual(stale['last_price'],98)
  self.assertTrue(app.market_view(dict(snapshot,status='unavailable'),[S],T+1000)[S]['stale'])
  self.assertNotIn('stale',q[S])
 def test_ticker_display_cannot_change_portfolio_or_marks(self):
  with patch('service_v2.now_ms',return_value=T),patch.dict(app.MARKET,dict(status='ok',symbols=app.parse_tickers(self.sample(),dict(received_at=T,server_at=T)))):
   before=app.summary()['state'];app.MARKET['symbols'][S]['last_price']=900000
   after=app.summary()['state']
   self.assertEqual(before,after);self.assertIsNone(after['positions'][S]['mark'])
 def test_public_ticker_transport_is_fixed_host_get_spot(self):
  body=dict(retCode=0,result=self.sample(),time=T)
  with patch('phoenix.urllib.request.build_opener') as opener,patch('phoenix.now_ms',return_value=T):
   opener.return_value.open.return_value.__enter__.return_value=io.StringIO(json.dumps(body))
   PublicSpot().get('tickers',None,category='linear')
   request=opener.return_value.open.call_args.args[0];url=urllib.parse.urlsplit(request.full_url)
   self.assertEqual(request.get_method(),'GET');self.assertEqual(url.netloc,'api.bybit.com')
   self.assertEqual(url.path,'/v5/market/tickers');self.assertEqual(urllib.parse.parse_qs(url.query)['category'],['spot'])
if __name__=='__main__':unittest.main()
