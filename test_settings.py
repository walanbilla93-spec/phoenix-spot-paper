import base64,copy,hashlib,json,os,tempfile,threading,unittest,urllib.request,urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from phoenix import *
from settings_model import *
from test_phoenix import T,S,rule,book,bar,order,F
import service_v2 as app

def verifier(password):
 salt=b'disposable-test-salt';return 'pbkdf2_sha256$100000$'+salt.hex()+'$'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt,100000).hex()

class SettingsTest(unittest.TestCase):
 def test_saved_profit_arm_callback_and_dca_rebound_drive_signals(self):
  cfg=dict(DEFAULT_SETTINGS,tp_arm_pct=3.,tp_callback_pct=.5,dca_rebound_pct=2.)
  p=position();p.update(active=True,qty=1,basis=100,purchases=1,threshold=98,strategy_settings=cfg)
  self.assertIsNone(signal(p,bar(T,p=102,h=102.9,l=102),F,T+1000))
  self.assertIsNone(p['peak'])
  self.assertIsNone(signal(p,bar(T+MINUTE,p=103,h=104,l=102),F,T+MINUTE+1000))
  self.assertEqual(p['peak'],104)
  # 0.3% pullback does not pass the configured 0.5% callback.
  self.assertIsNone(signal(p,bar(T+2*MINUTE,p=103.8,h=104,l=103.7),F,T+2*MINUTE+1000))
  self.assertEqual(signal(p,bar(T+3*MINUTE,p=103.5,h=103.8,l=103.4),F,T+3*MINUTE+1000)['side'],'sell')
  p=position();p.update(active=True,qty=1,basis=100,purchases=1,threshold=98,strategy_settings=cfg)
  self.assertIsNone(signal(p,bar(T,p=97,h=97.5,l=96),F,T+1000))
  self.assertIsNone(signal(p,bar(T+MINUTE,p=97,h=97.5,l=96.5),F,T+MINUTE+1000))
  self.assertEqual(signal(p,bar(T+2*MINUTE,p=97.8,h=98,l=97),F,T+2*MINUTE+1000)['side'],'buy')
 def test_validation_and_immutable_safety_fields(self):
  for values in (dict(DEFAULT_SETTINGS,reserve=0),dict(DEFAULT_SETTINGS,tp_arm_pct=float('nan')),
      dict(DEFAULT_SETTINGS,entries_enabled=1),dict(DEFAULT_SETTINGS,tp_callback_pct=2)):
   with self.assertRaises(ValueError):validate_settings(values)
 def test_new_cycle_uses_new_settings_existing_cycle_retains_original(self):
  st=initial();p=st['positions'][S];p['pending']=order(at=T-5000)
  advance(st,S,bar(T),F,book(T),rule(T),T)
  before=copy.deepcopy(st);new=dict(DEFAULT_SETTINGS,tp_arm_pct=2.5,dca_atr_multiplier=1.2)
  change_settings(st,new,T+1000,0,'synthetic-settings-0001')
  self.assertEqual(cycle_settings(p,st),DEFAULT_SETTINGS)
  for key in ('cash','realized','fees'):self.assertEqual(st[key],before[key])
  self.assertEqual(p['qty'],before['positions'][S]['qty'])
  self.assertEqual(p['basis'],before['positions'][S]['basis'])
  empty=st['positions']['AVAXUSDT'];self.assertEqual(cycle_settings(empty,st),new)
  # A later cycle captures the new settings at its actual first fill.
  p.update(active=False,qty=0,basis=0,purchases=0,pending=order(at=T+MINUTE-5000))
  # Use a fresh independent account to preserve accounting identity.
  st2=initial();change_settings(st2,new,T,0,'synthetic-settings-0002');st2['positions'][S]['pending']=order(at=T+MINUTE-5000)
  advance(st2,S,bar(T+MINUTE),F,book(T+MINUTE),rule(T+MINUTE),T+MINUTE)
  self.assertEqual(st2['positions'][S]['strategy_settings'],new)
  expected=st2['positions'][S]['last_fill']-max(1.2*F['atr'],.01*st2['positions'][S]['last_fill'])
  self.assertAlmostEqual(st2['positions'][S]['threshold'],expected)
 def test_paused_new_cycles_do_not_fill_queued_initial_buy(self):
  st=initial();change_settings(st,dict(DEFAULT_SETTINGS,entries_enabled=False),T,0,'synthetic-pause-0001')
  st['positions'][S]['pending']=order(at=T-5000)
  events=advance(st,S,bar(T),F,book(T),rule(T),T)
  self.assertEqual(st['cash'],1000);self.assertFalse(st['positions'][S]['active'])
  self.assertFalse(any(e['type']=='attempt' and e['fill']['qty']>0 for e in events))
 def test_authenticated_save_revision_dedup_and_restart(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('PHOENIX_TEST_TMP')) as d:
   path=os.path.join(d,'settings.db');db=Store(path)
   server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   base='http://127.0.0.1:'+str(server.server_port);password='disposable-test-password'
   body=dict(values=dict(DEFAULT_SETTINGS,tp_arm_pct=2.5),revision=0,request_id='synthetic-request-0001')
   def post(payload,pw=password):
    req=urllib.request.Request(base+'/api/settings?mode=fixed',data=json.dumps(payload).encode(),headers={
      'Content-Type':'application/json','Origin':base,'Authorization':'Basic '+base64.b64encode(('admin:'+pw).encode()).decode()})
    with urllib.request.urlopen(req) as f:return json.load(f)
   try:
    with patch.dict(app.DBS,{'fixed':db}),patch.dict(os.environ,{'PHOENIX_ADMIN_PASSWORD_HASH':verifier(password)}):
     with self.assertRaises(urllib.error.HTTPError) as err:post(body,'incorrect')
     self.assertEqual(err.exception.code,401);self.assertEqual(db.state['cash'],1000)
     result=post(body);self.assertEqual(result['revision'],1);head=db.verify()
     self.assertNotIn(password,json.dumps(result));self.assertNotIn('pbkdf2',json.dumps(result))
     self.assertEqual(post(body)['revision'],1);self.assertEqual(db.verify(),head)
     stale=dict(body,request_id='synthetic-request-0002')
     with self.assertRaises(urllib.error.HTTPError) as err:post(stale)
     self.assertEqual(err.exception.code,409)
     state=copy.deepcopy(db.state);db.db.close();db=Store(path);self.assertEqual(db.state,state);self.assertEqual(db.verify(),head)
   finally:server.shutdown();server.server_close();db.db.close()

if __name__=='__main__':unittest.main()
