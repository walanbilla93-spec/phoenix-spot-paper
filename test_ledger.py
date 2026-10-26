import copy,gzip,json,os,tempfile,threading,unittest,urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from phoenix import *
from test_phoenix import T,S,rule,book,bar,order,F
import service_v2 as app

class LedgerTest(unittest.TestCase):
 def test_position_and_record_details_share_incremental_journal(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   db=Store(os.path.join(d,'fast.db'));self.populate(db)
   with patch.dict(app.DBS,{'fixed':db}):
    ledger=app.execution_ledger('fixed',include_evidence=False)
    queries=[];db.db.set_trace_callback(queries.append)
    position=app.position_details('fixed',S)
    record=app.execution_ledger('fixed',record_id=ledger['records'][0]['record_id'])
    again=app.position_details('fixed',S)
    self.assertEqual(position['trades'],again['trades'])
    self.assertEqual(len(position['trades']),3)
    self.assertEqual(position['trades'][0]['paper_fill_id'],ledger['records'][0]['paper_fill_id'])
    self.assertEqual(len(record['records']),1);self.assertIn('evidence',record['records'][0])
    self.assertNotIn('coins',record) # A single detail avoids rebuilding unrelated summaries.
    payload_reads=[q for q in queries if q.startswith('SELECT id,observation,payload')]
    self.assertEqual(payload_reads,[]) # unchanged head never scans history again
    self.assertEqual(sum(q.startswith('SELECT payload FROM events WHERE id=') for q in queries),1)
    self.assertTrue(all('evidence' not in r for r in db._trade_journal['records']))
   db.db.close()
 def test_hourly_storage_batches_without_changing_frozen_rows(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   db=Store(os.path.join(d,'hours.db'));queries=[];db.db.set_trace_callback(queries.append)
   candles=[[T+i*HOUR,100,102,99,101,1000,100000] for i in range(205)]
   self.assertEqual(db.hourly(S,candles),candles)
   self.assertEqual(sum(q.startswith('INSERT INTO hours') for q in queries),3)
   changed=[row[:1]+[999]*6 for row in candles]
   self.assertEqual(db.hourly(S,changed),candles)
   db.db.close()
 def test_repeated_coin_cycles_stay_separate_after_restart(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   path=os.path.join(d,'cycles.db');db=Store(path)
   def execute(st,at,side,price):
    st['positions'][S]['pending']=order(side,at-5000)
    return advance(st,S,bar(at,p=price),F,book(at,price),rule(at),at)
   for n in range(2):
    at=T+n*10*MINUTE
    db.apply('buy'+str(n),lambda st:execute(st,at,'buy',100))
    db.apply('sell'+str(n),lambda st:execute(st,at+MINUTE,'sell',103))
   with patch.dict(app.DBS,{'fixed':db}):
    r=app.execution_ledger('fixed',include_evidence=False,filled_only=True)
   self.assertEqual(len(r['cycles']),2)
   self.assertEqual(len({c['cycle_id'] for c in r['cycles']}),2)
   self.assertTrue(all(c['status']=='Completed' and c['buy_fills']==1 and c['sell_fills']==1 for c in r['cycles']))
   self.assertAlmostEqual(sum(c['realized_pnl'] for c in r['cycles']),db.state['realized'])
   for c in r['cycles']:
    self.assertEqual([t['side'] for t in r['records'] if t['paper_cycle_id']==c['cycle_id']],['buy','sell'])
   db.db.close();db=Store(path)
   with patch.dict(app.DBS,{'fixed':db}):self.assertEqual(app.execution_ledger('fixed')['cycles'],r['cycles'])
   db.db.close()
 def populate(self,db):
  def buy(st,at,depth=100):
   st['positions'][S]['pending']=order(at=at-5000)
   return advance(st,S,bar(at),F,book(at,depth=depth),rule(at),at)
  db.apply('buy1',lambda st:buy(st,T));db.apply('buy2',lambda st:buy(st,T+MINUTE))
  def sell(st):
   st['positions'][S]['pending']=order('sell',T+2*MINUTE-5000)
   return advance(st,S,bar(T+2*MINUTE,p=102),F,book(T+2*MINUTE,102,.05),rule(T+2*MINUTE),T+2*MINUTE)
  db.apply('partial-sell',sell)
  db.apply('rejected',lambda st:buy(st,T+3*MINUTE,.00000001))
 def test_full_ledger_ids_quantities_and_no_read_mutation(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   db=Store(os.path.join(d,'ledger.db'));self.populate(db);before=copy.deepcopy(db.state);head=db.verify()
   with patch.dict(app.DBS,{'fixed':db,'weekly':None}):
    r=app.execution_ledger('fixed');again=app.execution_ledger('fixed')
    self.assertEqual(r['records'],again['records']);self.assertEqual(len(r['records']),4)
    fills=[t for t in r['records'] if t['filled']];self.assertEqual(len(fills),3)
    self.assertEqual(len({t['paper_fill_id'] for t in fills}),3)
    self.assertEqual(fills[0]['paper_cycle_id'],fills[1]['paper_cycle_id'])
    self.assertEqual(fills[0]['paper_cycle_id'],fills[2]['paper_cycle_id'])
    self.assertEqual(fills[2]['status'],'partial');self.assertAlmostEqual(fills[2]['gross_quantity'],.05)
    self.assertIsNone(r['records'][-1]['paper_fill_id']);self.assertEqual(r['totals']['rejections'],1)
    self.assertAlmostEqual(sum(t['realized_pnl'] for t in fills),db.state['realized'])
    self.assertTrue(all(t['exchange_order_id'] is None for t in fills))
    coin=next(c for c in r['coins'] if c['symbol']==S)
    self.assertAlmostEqual(coin['quantity'],db.state['positions'][S]['qty'])
    self.assertAlmostEqual(coin['realized_pnl'],db.state['realized'])
    self.assertEqual(coin['buy_fills'],2);self.assertEqual(coin['sell_fills'],1)
    self.assertTrue(all(t['quote_age_ms']<=2000 and t['evidence']['book']['s']==S for t in fills))
    self.assertEqual(app.execution_ledger('weekly')['records'],[])
    self.assertEqual(len(app.execution_ledger('fixed',include_evidence=False,filled_only=True)['records']),3)
   self.assertEqual(db.state,before);self.assertEqual(db.verify(),head);db.db.close()
 def test_ledger_page_and_api_are_read_only(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   db=Store(os.path.join(d,'ledger.db'));self.populate(db)
   server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   base='http://127.0.0.1:'+str(server.server_port)
   try:
    with patch.dict(app.DBS,{'fixed':db}):
     with urllib.request.urlopen(base+'/ledger?mode=fixed') as f:html=f.read().decode()
     self.assertIn('Live Ledger',html);self.assertIn('Download CSV',html);self.assertIn('Asia/Colombo',html)
     with urllib.request.urlopen(base+'/api/trades?mode=fixed') as f:r=json.load(f)
     self.assertEqual(r['portfolio'],'fixed');self.assertEqual(r['totals']['buy_fills'],2)
     self.assertNotIn('evidence',r['records'][0])
     rid=r['records'][0]['record_id']
     with urllib.request.urlopen(base+'/api/trades?mode=fixed&evidence=1&record='+rid) as f:proof=json.load(f)
     self.assertEqual(len(proof['records']),1);self.assertIn('book',proof['records'][0]['evidence'])
     with urllib.request.urlopen(urllib.request.Request(base+'/api/trades?mode=fixed&evidence=1',headers={'Accept-Encoding':'gzip'})) as f:
      self.assertEqual(f.headers.get('Content-Encoding'),'gzip');compressed=json.loads(gzip.decompress(f.read()))
     self.assertEqual([{k:v for k,v in row.items() if k!='evidence'} for row in compressed['records']],r['records'])
     with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(base+'/api/trades?mode=fixed',method='POST',data=b'{}'))
     self.assertEqual(error.exception.code,501)
   finally:server.shutdown();server.server_close();db.db.close()
if __name__=='__main__':unittest.main()
