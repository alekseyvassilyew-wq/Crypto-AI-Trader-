"""Conservative regime-aware strategy weighting for shadow research only."""

MIN_RESOLVED = 30
MIN_PROFIT_FACTOR = 1.10
MAX_DRAWDOWN_PCT = 12.0
MIN_WEIGHT = 0.50
MAX_WEIGHT = 1.50


def evaluate_adaptive_strategy_weights(regime, metrics):
    """Suggest bounded weights; insufficient evidence always receives neutral weight.

    This output is diagnostic only and must never authorize or size an order.
    """
    if not isinstance(regime, str) or not regime:
        regime = "UNKNOWN"
    regime_metrics = metrics.get(regime, {}) if isinstance(metrics, dict) else {}
    strategies = {}
    raw_weights = []
    for name, stats in sorted(regime_metrics.items()):
        if not isinstance(stats, dict):
            continue
        try:
            resolved = int(stats.get("resolved", 0))
            net_return = float(stats.get("net_return_pct", 0.0))
            profit_factor = stats.get("profit_factor", 0.0)
            profit_factor = float("inf") if profit_factor == "INF" else float(profit_factor)
            drawdown = float(stats.get("max_drawdown_pct", 100.0))
        except (TypeError, ValueError, OverflowError):
            resolved, net_return, profit_factor, drawdown = 0, 0.0, 0.0, 100.0
        eligible = (
            resolved >= MIN_RESOLVED
            and net_return > 0.0
            and profit_factor >= MIN_PROFIT_FACTOR
            and drawdown <= MAX_DRAWDOWN_PCT
        )
        # A small, bounded quality premium; never extrapolate raw returns.
        weight = 1.0
        if eligible:
            quality = min(1.0, max(0.0, (profit_factor - 1.0) / 1.0))
            quality *= min(1.0, max(0.0, net_return / 10.0))
            weight = min(MAX_WEIGHT, max(MIN_WEIGHT, 1.0 + 0.5 * quality))
        strategies[name] = {
            "weight": round(weight, 4),
            "eligible": eligible,
            "resolved": resolved,
            "reason": "qualified" if eligible else "insufficient_or_weak_evidence",
        }
        raw_weights.append(weight)
    # Normalize only the advisory weights; an absent strategy is not invented.
    total = sum(raw_weights)
    if total > 0:
        for item in strategies.values():
            item["weight"] = round(item["weight"] * len(strategies) / total, 4)
    return {
        "mode": "SHADOW_ONLY",
        "regime": regime,
        "minimum_resolved": MIN_RESOLVED,
        "strategies": strategies,
        "execution_effect": "NONE",
    }
