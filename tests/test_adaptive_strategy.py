from src.trader.adaptive_strategy import evaluate_adaptive_strategy_weights


def test_adaptive_weights_remain_neutral_until_minimum_sample():
    result = evaluate_adaptive_strategy_weights("TREND_UP", {
        "TREND_UP": {
            "trend_following": {
                "resolved": 29, "net_return_pct": 12.0,
                "profit_factor": 2.0, "max_drawdown_pct": 3.0,
            },
        },
    })
    item = result["strategies"]["trend_following"]
    assert item["weight"] == 1.0
    assert item["eligible"] is False
    assert result["execution_effect"] == "NONE"


def test_adaptive_weights_reward_only_qualified_regime_performance():
    result = evaluate_adaptive_strategy_weights("TREND_UP", {
        "TREND_UP": {
            "trend_following": {
                "resolved": 40, "net_return_pct": 8.0,
                "profit_factor": 1.8, "max_drawdown_pct": 7.0,
            },
            "breakout": {
                "resolved": 50, "net_return_pct": -2.0,
                "profit_factor": 0.8, "max_drawdown_pct": 15.0,
            },
        },
        "RANGE_OR_TRANSITION": {
            "mean_reversion": {
                "resolved": 80, "net_return_pct": 20.0,
                "profit_factor": 2.0, "max_drawdown_pct": 5.0,
            },
        },
    })
    assert result["strategies"]["trend_following"]["eligible"] is True
    assert result["strategies"]["trend_following"]["weight"] > 1.0
    assert result["strategies"]["breakout"]["eligible"] is False
    assert result["strategies"]["breakout"]["weight"] == 1.0
    assert result["execution_effect"] == "NONE"


def test_unknown_regime_returns_no_invented_strategy_weights():
    result = evaluate_adaptive_strategy_weights("UNKNOWN", {})
    assert result["strategies"] == {}
    assert result["mode"] == "SHADOW_ONLY"
