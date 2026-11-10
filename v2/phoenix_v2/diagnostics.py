"""Read-only context summaries, never an alternative signal evaluator."""

def entry_context(strategy, at, feed_reason=None):
    bias = strategy.bias()
    zones = [z for z in strategy.zones if z.available <= at and z.valid and z.tf in (15, 60)]
    pools = [p for p in strategy.liquidity.pools if p.available <= at and not p.consumed]
    candidates = [z for z in zones if z.side == bias and z.inside and 1 <= z.visits <= 2]
    waiting = []
    if feed_reason: waiting.append('feed_quarantined')
    if not strategy.warm(): waiting.append('structure_warmup')
    if not bias: waiting.append('higher_timeframe_bias_neutral')
    if not zones: waiting.append('no_live_15m_1h_poi')
    if not candidates: waiting.append('no_visited_poi_in_bias')
    if not pools: waiting.append('no_unconsumed_liquidity_pool')
    if not strategy.sweeps and not strategy.setups: waiting.append('no_active_sweep_or_reclaim')
    return dict(at=at, warm=strategy.warm(), bias=bias,
                h4_regime=strategy.frames[240].regime, daily_regime=strategy.frames[1440].regime,
                h1_regime=strategy.frames[60].regime,strategy_version=getattr(strategy,'profile','RC2'),
                live_pois=len(zones), visited_pois_in_bias=len(candidates),
                unconsumed_pools=len(pools), active_sweeps=len(strategy.sweeps),
                active_reclaims=len(strategy.setups), waiting_for=waiting,
                note='Current prerequisites only; not a full signal verdict or a historical missed-trade claim.')
