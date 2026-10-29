"""Bounded capture quality metadata. This module makes no trading decisions."""
import json,math,time,uuid
from collections import deque

def compact(x):return json.dumps(x,separators=(',',':')).encode()+b'\n'
def now():return time.time_ns()//1000000

class CoinBuffer:
 def __init__(self,cap=2_000_000,max_records=6000):
  self.rows=deque();self.bytes=0;self.cap=cap;self.max_records=max_records;self.started=None;self.capacity_evicted_at=None;self.dropped=0
 def trim(self,ts):
  while self.rows and self.rows[0][1]<ts-120000:
   _,_,data=self.rows.popleft();self.bytes-=len(data)
 def add(self,ordinal,ts,data):
  if self.started is None:self.started=ts
  self.trim(ts)
  while self.rows and (self.bytes+len(data)>self.cap or len(self.rows)>=self.max_records):
   _,t,d=self.rows.popleft();self.bytes-=len(d);self.capacity_evicted_at=t;self.dropped+=1
  if len(data)>self.cap:self.capacity_evicted_at=ts;self.dropped+=1;return
  self.rows.append((ordinal,ts,data));self.bytes+=len(data)
 def coverage(self,ts):
  self.trim(ts);truncated=self.capacity_evicted_at is not None and self.capacity_evicted_at>=ts-120000;warm=self.started is None or ts-self.started<120000
  return {'requested_ms':120000,'monitoring_since_ms':self.started,'first_retained_ms':self.rows[0][1] if self.rows else None,'last_retained_ms':self.rows[-1][1] if self.rows else None,'capacity_truncated':truncated,'warming_up':warm,'payload_bytes':self.bytes,'payload_limit_bytes':self.cap,'record_count':len(self.rows),'record_limit':self.max_records,'capacity_dropped_records_total':self.dropped,'coverage':'TRUNCATED' if truncated else 'WARMUP' if warm else 'MONITORED_NOT_PROOF_OF_COMPLETENESS'}

class QualityMixin:
 def __init__(self,symbols):
  super().__init__(symbols)
  self.buffers={s:CoinBuffer() for s in symbols};self.ordinal={s:0 for s in symbols};self.saved_ordinal={s:0 for s in symbols};self.window_meta={}
  self.recent={s:deque(maxlen=4000) for s in symbols};self.flow_evicted={s:None for s in symbols};self.flow_started={s:None for s in symbols};self.late_trades={s:0 for s in symbols};self.latest_quotes={s:None for s in symbols}
 def candle(self,s,c):
  zones=super().candle(s,c)
  for z in zones:z['available_ms']=c.get('available_ms',z['available_ms'])
  return zones
 def flow_metrics(self,s,ts):
  rows=self.recent[s]
  while rows and rows[0][0]<ts-60000:rows.popleft()
  buy=sum(v for _,v,side in rows if side=='Buy');sell=sum(v for _,v,side in rows if side=='Sell')
  return {'interval_ms':60000,'as_of_ms':ts,'trade_count':len(rows),'buy_trade_count':sum(side=='Buy' for _,_,side in rows),'sell_trade_count':sum(side=='Sell' for _,_,side in rows),'buy_base_volume':buy,'sell_base_volume':sell,'delta_base_volume':buy-sell,'delta_pct':100*(buy-sell)/(buy+sell) if buy+sell else None,'capacity_truncated':self.flow_evicted[s] is not None and self.flow_evicted[s]>=ts-60000,'warming_up':self.flow_started[s] is None or ts-self.flow_started[s]<60000,'max_trades':4000,'deduplicated':False,'note':'Observed flow, not entry permission.'}
 def end_window(self,s,ts,reason,complete=False):
  w=self.window_meta.pop(s);self.until[s]=0
  return compact({'kind':'capture_window_end','schema_version':2,'symbol':s,'receive_ms':ts,'end_ms':w['requested_end_ms'],'complete_requested_interval':complete,'reason':reason,**w,'flow_at_end':self.flow_metrics(s,ts),'quote_at_end':self.latest_quotes[s],'outcome':'NOT_A_TRADE_OUTCOME'})
 def process(self,s,payload,data,ts):
  out=[];topic=payload.get('topic','');self.received+=1;self.ordinal[s]+=1;ordinal=self.ordinal[s];buf=self.buffers[s];buf.trim(ts)
  if self.until[s] and ts>=self.until[s]:out.append(self.end_window(s,ts,'FIXED_10_MINUTE_LIMIT',True))
  price=None
  if topic.startswith('orderbook.1.'):
   q=payload.get('data',{});b=q.get('b',[]);a=q.get('a',[])
   if b and a:
    bid=float(b[0][0]);ask=float(a[0][0]);self.latest_quotes[s]={'bid':bid,'ask':ask,'bid_size':float(b[0][1]),'ask_size':float(a[0][1]),'spread_bps':(ask-bid)/((ask+bid)/2)*10000,'receive_ms':ts,'exchange_ms':payload.get('cts',payload.get('ts'))}
  if topic.startswith('publicTrade.') and payload.get('data'):
   price=float(payload['data'][-1]['p'])
   for trade in payload['data']:
    rows=self.recent[s]
    while rows and rows[0][0]<ts-60000:rows.popleft()
    if len(rows)==rows.maxlen:self.flow_evicted[s]=rows[0][0]
    if self.flow_started[s] is None:self.flow_started[s]=ts
    rows.append((ts,float(trade['v']),trade['S']))
    minute=int(trade['T'])//60000*60000;f=self.flow[s]
    if f and minute<f['minute_ms']:self.late_trades[s]+=1;continue
    if f and minute>f['minute_ms']:out.append(compact({'kind':'flow_summary_1m','symbol':s,'receive_ms':ts,**f}));f=None
    if f is None:
     f={'minute_ms':minute,'log_price_step':.0005,'bins':{},'overflow_buy':0.,'overflow_sell':0.,'trade_count':0,'buy_trade_count':0,'sell_trade_count':0,'buy_base_volume':0.,'sell_base_volume':0.,'late_trades_omitted_total':self.late_trades[s],'approximate':True};self.flow[s]=f
    f['trade_count']+=1;f['buy_trade_count' if trade['S']=='Buy' else 'sell_trade_count']+=1;f['buy_base_volume' if trade['S']=='Buy' else 'sell_base_volume']+=float(trade['v'])
    bucket=str(math.floor(math.log(float(trade['p']))/.0005));side=0 if trade['S']=='Buy' else 1;volume=float(trade['v'])
    if bucket not in f['bins'] and len(f['bins'])>=256:f['overflow_buy' if side==0 else 'overflow_sell']+=volume
    else:
     if bucket not in f['bins']:f['bins'][bucket]=[0.,0.]
     f['bins'][bucket][side]+=volume
  if topic.startswith('kline.'):
   for k in payload.get('data',[]):
    if k.get('confirm'):
     c={'t':int(k['start']),'o':float(k['open']),'h':float(k['high']),'l':float(k['low']),'c':float(k['close']),'v':k['volume'],'turnover':k['turnover'],'available_ms':ts}
     if self.bars[s] and c['t']<=self.bars[s][-1]['t']:continue
     for z in self.candle(s,c):out.append(compact({'kind':'candidate_zone','symbol':s,'receive_ms':ts,'zone':z}))
     out.append(compact({'kind':'candle_1m','symbol':s,'receive_ms':ts,'candle':c}));price=c['c']
  if price:
   matches=[z for z in self.zones[s] if z['available_ms']<=ts and ts-z['formed_ms']<86400000 and z['low']*.999<=price<=z['high']*1.001]
   approaching=bool(matches) and not self.near[s];self.near[s]=bool(matches)
   if approaching and ts>=self.cooldown[s] and not self.until[s]:
    self.windows+=1;self.until[s]=ts+600000;self.cooldown[s]=ts+1800000;coverage=buf.coverage(ts);window_id=uuid.uuid4().hex
    self.window_meta[s]={'window_id':window_id,'start_ms':ts,'requested_end_ms':self.until[s],'prebuffer':coverage,'last_detail_ms':None,'last_trade_ms':None,'last_quote_ms':None,'trade_count':0,'quote_messages':0}
    out.append(compact({'kind':'capture_window_start','schema_version':2,'window_id':window_id,'market_time_bucket_15m':ts//900000*900000,'symbol':s,'receive_ms':ts,'end_ms':self.until[s],'zones':matches,'policy':'PROVISIONAL_POI_NOT_ENTRY','prebuffer_requested_ms':120000,'prebuffer':coverage,'prebuffer_globally_truncated':coverage['capacity_truncated'],'prebuffer_warming_up':coverage['warming_up'],'flow_at_trigger':self.flow_metrics(s,ts),'quote_at_trigger':self.latest_quotes[s]}))
    for number,t,d in buf.rows:
     if number>self.saved_ordinal[s]:out.append(d);self.last_saved[s]=t;self.saved_ordinal[s]=number
  if self.until[s] and topic.startswith(('publicTrade.','orderbook.1.')):
   out.append(data);self.last_saved[s]=ts;self.saved_ordinal[s]=ordinal;self.saved+=1;w=self.window_meta[s];w['last_detail_ms']=ts
   if topic.startswith('publicTrade.'):w['trade_count']+=len(payload.get('data',[]));w['last_trade_ms']=ts
   else:w['quote_messages']+=1;w['last_quote_ms']=ts
  if topic.startswith(('publicTrade.','orderbook.1.')):buf.add(ordinal,ts,data)
  self.buffer_bytes=sum(b.bytes for b in self.buffers.values())
  return out
 def interrupt(self,ts,reason):
  out=[self.end_window(s,ts,reason,False) for s in self.until if self.until[s]];self.reset_gap();return out
 def reset_gap(self):
  super().reset_gap();self.buffers={s:CoinBuffer() for s in self.bars};self.window_meta.clear()
  for s in self.bars:self.recent[s].clear();self.flow_evicted[s]=None;self.flow_started[s]=None;self.latest_quotes[s]=None
 def status(self):
  ts=now();coverage={s:b.coverage(ts) for s,b in self.buffers.items()};self.buffer_bytes=sum(b.bytes for b in self.buffers.values())
  return {'mode':'SELECTIVE_PROVISIONAL_POI','timeframe':'1m','zone_limit_per_coin':8,'zone_expiry_hours':24,'proximity_bps':10,'prebuffer_seconds':120,'prebuffer_policy':'PER_COIN_RESERVED_MEMORY_V2','prebuffer_global_cap_bytes':len(self.buffers)*2_000_000,'prebuffer_bytes':self.buffer_bytes,'prebuffer_per_coin':coverage,'detail_window_minutes':10,'minimum_trigger_spacing_minutes':30,'windows_started':self.windows,'active_windows':{s:{'end_ms':t,'window_id':self.window_meta[s]['window_id']} for s,t in self.until.items() if t>ts},'raw_messages_considered':self.received,'raw_live_messages_saved':self.saved,'recent_flow_60s':{s:self.flow_metrics(s,ts) for s in self.bars},'latest_quotes':self.latest_quotes,'late_summary_trades_omitted':self.late_trades,'zones':{s:list(z) for s,z in self.zones.items()},'note':'Capture only; not strategy entries, exact profiles, deduplicated flow, verified depth/fills, or independent trades.'}

class RuntimeQualityMixin:
 async def mark_gap(self,connection,started,exc,ts=None):
  ts=ts or now();error=type(exc).__name__+': '+str(exc);frames=self.filter.interrupt(ts,'CONNECTION_GAP')
  self.research.boundary(ts,'CONNECTION_GAP');frames.extend(self.research_outbox);self.research_outbox.clear()
  if self.stop.is_set():return
  await self.enqueue_selected(frames);await self.queue.join();ordinary=isinstance(exc,(ConnectionError,TimeoutError));observed={s:{k:v[k] for k in ('last_quote_at','last_trade_at','last_candle_at')} for s,v in self.per_symbol.items()};quotes=[v['last_quote_at'] for v in observed.values() if v['last_quote_at'] is not None];since=max(started,min(quotes)-2000) if ordinary and quotes else started
  if self.pending_gap is None:
   self.pending_gap={'gap_id':uuid.uuid4().hex,'since_ms':since,'connection_started_ms':started,'original_connection':connection,'policy':'LOCALIZED_OBSERVATION_BOUNDARY_V2' if ordinary else 'CONSERVATIVE_PIPELINE_FAILURE_V2'}
   await self.event('gap',schema_version=2,connection=connection,until_ms=ts,detected_ms=ts,error=error,recovery_required=True,last_observed=observed,replay_policy=self.pending_gap['policy'],interpretation='Uncertain observation boundary; recovery and offline volume/quote checks required. No feed-completeness claim.',**self.pending_gap)
  else:
   if not ordinary:self.pending_gap['since_ms']=min(self.pending_gap['since_ms'],started);self.pending_gap['policy']='CONSERVATIVE_PIPELINE_FAILURE_V2'
   await self.event('gap_attempt',gap_id=self.pending_gap['gap_id'],connection=connection,detected_ms=ts,error=error,since_ms=self.pending_gap['since_ms'])
  self.state.update(connected=False,capture='GAP',last_error=error,last_disconnect_error=error,current_gap=dict(self.pending_gap));self.state['gap_count']+=1;self.state['reconnects']+=1
 async def feed_ready(self,connection,ts=None):
  ts=ts or now()
  if self.pending_gap:
   await self.event('gap_recovered',schema_version=2,gap_id=self.pending_gap['gap_id'],since_ms=self.pending_gap['since_ms'],until_ms=ts,new_connection=connection,replay_policy=self.pending_gap['policy'],recovery_evidence='Subscription acknowledged and new L1 snapshot received for every coin; no missing-trade backfill claimed');self.pending_gap=None
  self.state.update(capture='RECORDING',connected=True,last_error=None,current_gap=None)

PAGE='''<!doctype html><meta charset="utf-8"><title>Market Data Recorder</title><style>body{font:15px system-ui;max-width:1100px;margin:30px auto;padding:18px;color:#183047}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #dce5eb;padding:7px;text-align:right}td:first-child,th:first-child{text-align:left}pre{background:#eef3f6;padding:18px;white-space:pre-wrap}input,select,button{padding:10px;margin:5px}#health{padding:15px;background:#edf8f2}progress{width:100%}</style><h1>Market Data Recorder</h1><p>Selective public Bybit capture. Candidate OB/FVG windows are not trading signals. No trading or AI calls.</p><div id="health">Loading capture health…</div><p id="capacity"></p><progress id="storage" max="100" value="0"></progress><p id="windows"></p><h2>Confluence paper research</h2><p>Frozen research interpretation; no live exchange orders. All seven confluence gates are required.</p><p id="research">Loading measured decisions…</p><details><summary>Eligibility and evidence coverage</summary><pre id="eligibility"></pre></details><table><thead><tr><th>Coin</th><th>60s trades</th><th>60s base volume</th><th>Delta %</th><th>Flow coverage</th><th>Quoted spread bps</th><th>Prebuffer</th></tr></thead><tbody id="coins"></tbody></table><p>A large delta percentage from one trade is not sustained-flow confirmation. Quotes do not establish depth or achievable fills.</p><form method="post" action="/export.tar"><label>Export token <input type="password" name="token" required autocomplete="off"></label><label>Data <select name="scope"><option value="research" selected>Research ledger and decisions (small archive)</option><option value="current_boot_part">Current run, small archive part</option><option value="all">All retained recordings</option><option value="current_boot">Current run only (smaller)</option></select></label><label>Part <input type="number" name="part" min="0" max="1000" value="0" style="width:65px"></label><button>Download research archive</button></form><p>Archive parts contain up to ten sealed files; the manifest identifies the next part. Exact-price minute footprints continue outside raw clips. Review candidates require all confluences and a frozen execution plan. Exports stream from disk. No automatic offload or deletion. Capacity warnings at 80%, 90% and 95%; preserve copies before storage fills. Startup, reconnects and buffer truncation remain quality boundaries.</p><details><summary>Detailed status and quality metadata</summary><pre id="s"></pre></details><script>
function cell(row,v){const e=document.createElement('td');e.textContent=v;row.appendChild(e)}
function renderResearch(v){const r=v.research;if(!r){document.getElementById('research').textContent='Research engine unavailable';return}document.getElementById('research').textContent=r.state+' · '+r.evaluated_setups+' evaluated · '+r.qualified_setups+' qualified · '+r.entered+' entered · '+r.closed+' closed · '+r.censored+' censored · '+r.missed+' missed · WR '+(r.win_rate_pct==null?'unavailable (no closed eligible trades)':r.win_rate_pct.toFixed(1)+'%')+' · net '+r.net_R.toFixed(3)+'R'+(r.error?' · ERROR: '+r.error:'');document.getElementById('eligibility').textContent=JSON.stringify({policy:r.policy_id,blockers:r.blockers,coverage:r.per_symbol,open_position:r.open_position,cost_and_fill_model:r.outcome_model},null,2)}
async function update(){try{const response=await fetch('/status');if(!response.ok)throw Error('HTTP '+response.status);const v=await response.json(),q=v.selection,st=v.storage;document.getElementById('s').textContent=JSON.stringify(v,null,2);document.getElementById('health').textContent=v.capture+' · '+v.version+' · '+v.gap_count+' disconnect attempts · '+v.overflow_messages+' queue overflows'+(v.last_error?' · CURRENT ERROR: '+v.last_error:'');document.getElementById('health').style.background=v.capture==='RECORDING'?'#edf8f2':'#fff0da';document.getElementById('capacity').textContent='Storage '+(st.total_bytes/1e6).toFixed(1)+' / '+(st.limit_bytes/1e6).toFixed(0)+' MB · '+st.warning+' · includes active segment';document.getElementById('storage').value=st.used_pct;document.getElementById('windows').textContent=q.windows_started+' candidate windows · '+Object.keys(q.active_windows).length+' active · '+(q.prebuffer_bytes/1e6).toFixed(1)+' MB of reserved buffers used';if(q.setup_review){document.getElementById('windows').textContent+=' · '+q.setup_review.sweep_break_candidates+' sweep/break review candidates · '+q.setup_evidence.minutes+' exact-price minute footprints · full setup qualification still requires causal confluence review';}renderResearch(v);const body=document.getElementById('coins');body.replaceChildren();for(const sym of v.symbols){const f=q.recent_flow_60s[sym],b=q.prebuffer_per_coin[sym],quote=q.latest_quotes[sym],row=document.createElement('tr');cell(row,sym);cell(row,f.trade_count);cell(row,(f.buy_base_volume+f.sell_base_volume).toPrecision(5));cell(row,f.delta_pct==null?'—':f.delta_pct.toFixed(1));cell(row,f.capacity_truncated?'TRUNCATED':f.warming_up?'WARMUP':'Observed / not deduplicated');cell(row,quote?quote.spread_bps.toFixed(2):'—');cell(row,b.capacity_truncated?'TRUNCATED':b.warming_up?'WARMUP':'Monitored 120s');body.appendChild(row)}}catch(e){document.getElementById('health').textContent='Capture status unavailable: '+e.message}}update();setInterval(update,10000)</script>'''


