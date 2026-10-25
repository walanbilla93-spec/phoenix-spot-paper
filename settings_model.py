"""Validated prospective paper strategy settings; execution/account limits remain fixed."""
import copy,hashlib,json,math

DEFAULT_SETTINGS=dict(tp_arm_pct=1.6,tp_callback_pct=.2,dca_atr_multiplier=.75,
    dca_min_drop_pct=1.,dca_rebound_pct=1.5,entries_enabled=True)
SETTING_FIELDS=[
 dict(key='tp_arm_pct',label='Profit arm (%)',minimum=.3,maximum=10,step=.1),
 dict(key='tp_callback_pct',label='Profit trailing callback (%)',minimum=.05,maximum=3,step=.05),
 dict(key='dca_atr_multiplier',label='DCA ATR14 multiplier',minimum=.1,maximum=5,step=.05),
 dict(key='dca_min_drop_pct',label='DCA minimum drop (%)',minimum=.1,maximum=10,step=.1),
 dict(key='dca_rebound_pct',label='DCA rebound confirmation (%)',minimum=.1,maximum=10,step=.1)]

def validate_settings(values):
 if not isinstance(values,dict) or set(values)!=set(DEFAULT_SETTINGS):raise ValueError('Unknown or missing settings')
 out={}
 for f in SETTING_FIELDS:
  v=values[f['key']]
  if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not f['minimum']<=v<=f['maximum']:
   raise ValueError(f["label"]+' is outside its allowed range')
  out[f['key']]=float(v)
 if type(values['entries_enabled']) is not bool:raise ValueError('New entries must be enabled or paused')
 if out['tp_callback_pct']>=out['tp_arm_pct']:raise ValueError('Profit callback must be below profit arm')
 out['entries_enabled']=values['entries_enabled'];return out

def settings_hash(values):return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def account_settings(state):return copy.deepcopy(state.get('paper_settings',DEFAULT_SETTINGS))
def cycle_settings(position,state=None):
 return copy.deepcopy(position.get('strategy_settings',DEFAULT_SETTINGS) if position['active'] else account_settings(state or {}))
def change_settings(state,values,at,expected_revision,request_id):
 revision=state.get('settings_revision',0)
 if revision!=expected_revision:raise ValueError('Settings changed in another window. Reload before saving.')
 values=validate_settings(values);previous=account_settings(state);cancelled=[]
 for symbol,p in state['positions'].items():
  if p['active']:p.setdefault('strategy_settings',copy.deepcopy(DEFAULT_SETTINGS))
  elif p['pending']:p['pending']=None;cancelled.append(symbol)
 state.update(paper_settings=values,settings_revision=revision+1,settings_hash=settings_hash(values))
 event=dict(type='paper_settings_change',at=at,revision=revision+1,request_id=request_id,
    previous=previous,values=values,settings_hash=state['settings_hash'],cancelled_entries=cancelled,
    scope='New cycles only. Open cycles retain their original settings.')
 state['settings_history']=(state.get('settings_history',[])+[copy.deepcopy(event)])[-20:]
 return [event]
