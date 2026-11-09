"""Explicit requested assets and native Bybit contract denominations."""
ASSETS = (
    ('BTC','BTCUSDT',1), ('ETH','ETHUSDT',1), ('SOL','SOLUSDT',1),
    ('BNB','BNBUSDT',1), ('AVAX','AVAXUSDT',1), ('XRP','XRPUSDT',1),
    ('LINK','LINKUSDT',1), ('ARB','ARBUSDT',1), ('UNI','UNIUSDT',1),
    ('DOGE','DOGEUSDT',1), ('SHIB','SHIB1000USDT',1000), ('PEPE','1000PEPEUSDT',1000),
)
SYMBOLS = tuple(s for _,s,_ in ASSETS)
META = {s:dict(asset=a,symbol=s,underlying_units_per_native_unit=n,
          denomination_note=(f'Native Bybit {n} {a} denomination; prices and sizes stay in native contract units.' if n!=1 else 'Native contract units.')) for a,s,n in ASSETS}
LIQUIDITY_POLICY = dict(min_turnover24h_usdt='5000000',max_spread_bps='15',
                       min_near_depth_each_side_usdt='10000',depth_band_bps='10',
                       refresh_seconds=900,expires_seconds=1800,
                       purpose='Disclosed experimental monitoring/risk eligibility; no research gate is closed.')

def assess(instrument, ticker, book, at):
    from .model import number, D
    symbol=instrument['symbol']; failures=[]
    if symbol not in META:raise ValueError('unrequested contract')
    expected_base=symbol[:-4]
    available=(instrument.get('baseCoin')==expected_base and instrument.get('quoteCoin')=='USDT'
        and instrument.get('settleCoin')=='USDT' and instrument.get('status')=='Trading'
        and instrument.get('contractType')=='LinearPerpetual')
    if not available:failures.append('contract_not_available_as_requested')
    if ticker['symbol']!=symbol or book['s']!=symbol:raise ValueError('liquidity instrument mismatch')
    if abs(at-int(book['ts'])/1000)>30:failures.append('stale_orderbook')
    bid=number(book['b'][0][0]);ask=number(book['a'][0][0]);mid=(bid+ask)/2
    if bid<=0 or ask<bid:raise ValueError('invalid bid/ask')
    spread=(ask-bid)/mid*10000;turnover=number(ticker['turnover24h'])
    band=D(LIQUIDITY_POLICY['depth_band_bps'])/10000
    bid_depth=sum((number(p)*number(q) for p,q in book['b'] if number(p)>=mid*(1-band)),D(0))
    ask_depth=sum((number(p)*number(q) for p,q in book['a'] if number(p)<=mid*(1+band)),D(0))
    if turnover<D(LIQUIDITY_POLICY['min_turnover24h_usdt']):failures.append('low_24h_turnover')
    if spread>D(LIQUIDITY_POLICY['max_spread_bps']):failures.append('wide_spread')
    if min(bid_depth,ask_depth)<D(LIQUIDITY_POLICY['min_near_depth_each_side_usdt']):failures.append('insufficient_visible_near_depth')
    return dict(META[symbol],available=available,liquidity_eligible=not failures,
        observed_at=at,expires_at=at+LIQUIDITY_POLICY['expires_seconds'],
        turnover24h_usdt=str(turnover),spread_bps=str(spread),
        bid_depth_usdt=str(bid_depth),ask_depth_usdt=str(ask_depth),
        failures=failures,policy=LIQUIDITY_POLICY)
