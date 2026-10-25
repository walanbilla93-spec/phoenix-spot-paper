import copy,gzip,json,os,tempfile,threading,unittest,urllib.request
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from phoenix import *
from test_phoenix import T,S,rule,book,bar,order,F
import service_v2 as app

class LedgerTest(unittest.TestCase):
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
    self.assertTrue(all(t['quote_age_ms']<=2000 and t['evidence']['book']['s']==S for t in fills))
    self.assertEqual(app.execution_ledger('weekly')['records'],[])
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
