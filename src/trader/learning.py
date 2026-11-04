"""Leakage-aware research models for PAPER strategy experiments.

This module never places orders or promotes a model. It trains a deterministic
small neural network using chronological walk-forward validation and returns
a candidate artifact for shadow observation only.
"""
import math

FEATURE_NAMES = ("return_3", "return_12", "return_24", "ema_gap", "rsi_centered", "atr_percent", "volume_ratio")


def _ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def extract_features(rows, end=None):
    end = len(rows) - 1 if end is None else int(end)
    history = rows[:end + 1]
    closes = [float(row[4]) for row in history]
    highs = [float(row[2]) for row in history]
    lows = [float(row[3]) for row in history]
    volumes = [float(row[5]) for row in history]
    close = closes[-1]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if close <= 0 or not e20 or not e50:
        return None
    returns = []
    for period in (3, 12, 24):
        previous = closes[-period - 1]
        returns.append(close / previous - 1.0 if previous > 0 else 0.0)
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
    gains = sum(max(value, 0.0) for value in changes) / 14
    losses = sum(max(-value, 0.0) for value in changes) / 14
    rsi = 100.0 if losses == 0 and gains else 50.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    true_ranges = []
    for i in range(len(closes) - 14, len(closes)):
        previous = closes[i - 1]
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - previous), abs(lows[i] - previous)))
    atr_pct = sum(true_ranges) / 14 / close
    avg_volume = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0.0
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    values = returns + [e20 / e50 - 1.0, (rsi - 50.0) / 50.0, atr_pct, min(volume_ratio, 10.0) / 10.0]
    return values if all(math.isfinite(value) for value in values) else None


def _sigmoid(value):
    if value >= 35:
        return 1.0
    if value <= -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _fit(samples, labels, epochs=450, learning_rate=0.05, l2=0.01, hidden_units=8):
    """Train a small deterministic 7-8-1 tanh/sigmoid neural network."""
    width = len(samples[0])
    means = [sum(row[j] for row in samples) / len(samples) for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in samples) / len(samples)
        scales.append(math.sqrt(variance) or 1.0)
    normalized = [[(row[j] - means[j]) / scales[j] for j in range(width)] for row in samples]
    # Deterministic low-amplitude initialization makes training reproducible.
    w1 = [[0.08 * math.sin((i + 1) * (j + 1)) for j in range(hidden_units)] for i in range(width)]
    b1 = [0.0] * hidden_units
    w2 = [0.08 * math.cos(j + 1) for j in range(hidden_units)]
    b2 = 0.0
    count = len(normalized)
    positive_count = sum(1 for label in labels if label == 1)
    negative_count = count - positive_count
    # Balance the loss so a rare profitable class is not overwhelmed by the
    # majority class. These weights are fitted on the training partition only.
    if positive_count and negative_count:
        class_weights = {
            "0": count / (2.0 * negative_count),
            "1": count / (2.0 * positive_count),
        }
    else:
        class_weights = {"0": 1.0, "1": 1.0}
    weight_sum = sum(class_weights[str(label)] for label in labels)
    for _ in range(epochs):
        gw1 = [[0.0] * hidden_units for _ in range(width)]
        gb1 = [0.0] * hidden_units
        gw2 = [0.0] * hidden_units
        gb2 = 0.0
        for row, label in zip(normalized, labels):
            sample_weight = class_weights[str(label)]
            hidden = [
                math.tanh(b1[j] + sum(w1[i][j] * row[i] for i in range(width)))
                for j in range(hidden_units)
            ]
            probability = _sigmoid(b2 + sum(w2[j] * hidden[j] for j in range(hidden_units)))
            output_delta = (probability - label) * sample_weight
            gb2 += output_delta
            hidden_deltas = [
                output_delta * w2[j] * (1.0 - hidden[j] * hidden[j])
                for j in range(hidden_units)
            ]
            for j in range(hidden_units):
                gw2[j] += output_delta * hidden[j]
                gb1[j] += hidden_deltas[j]
                for i in range(width):
                    gw1[i][j] += hidden_deltas[j] * row[i]
        b2 -= learning_rate * gb2 / weight_sum
        for j in range(hidden_units):
            w2[j] -= learning_rate * (gw2[j] / weight_sum + l2 * w2[j])
            b1[j] -= learning_rate * gb1[j] / weight_sum
        for i in range(width):
            for j in range(hidden_units):
                w1[i][j] -= learning_rate * (gw1[i][j] / weight_sum + l2 * w1[i][j])
    return {
        "architecture": f"{width}-{hidden_units}-1",
        "means": means, "scales": scales,
        "weights_input_hidden": w1, "bias_hidden": b1,
        "weights_hidden_output": w2, "bias_output": b2,
        "training_class_counts": {"negative": negative_count, "positive": positive_count},
        "training_class_weights": class_weights,
        "score_type": "class_weighted_probability_score_not_calibrated_probability",
    }


def predict_probability(model, features):
    normalized = [
        (float(value) - model["means"][index]) / model["scales"][index]
        for index, value in enumerate(features)
    ]
    hidden = [
        math.tanh(
            model["bias_hidden"][j]
            + sum(model["weights_input_hidden"][i][j] * normalized[i]
                  for i in range(len(normalized)))
        )
        for j in range(len(model["bias_hidden"]))
    ]
    return _sigmoid(
        model["bias_output"]
        + sum(model["weights_hidden_output"][j] * hidden[j] for j in range(len(hidden)))
    )


def _net_return_after_costs(entry_price, exit_price, fee_rate, slippage_bps):
    """Long spot return after adverse entry/exit slippage and fees on both sides."""
    entry = float(entry_price)
    exit_value = float(exit_price)
    fee = float(fee_rate)
    slip = float(slippage_bps) / 10000.0
    if not all(math.isfinite(value) for value in (entry, exit_value, fee, slip)):
        raise ValueError("Execution prices and costs must be finite")
    if entry <= 0 or exit_value <= 0 or fee < 0 or fee >= 1 or slip < 0 or slip >= 1:
        raise ValueError("Invalid execution prices or costs")
    entry_fill = entry * (1.0 + slip)
    exit_fill = exit_value * (1.0 - slip)
    return (exit_fill * (1.0 - fee)) / (entry_fill * (1.0 + fee)) - 1.0


def _performance(trades, threshold, horizon=1):
    equity, peak, drawdown = 1.0, 1.0, 0.0
    wins, losses = [], []
    next_entry = -1
    for sample_index, probability, future_return in trades:
        if sample_index < next_entry or probability < threshold:
            continue
        next_entry = sample_index + int(horizon)
        equity *= 1.0 + future_return
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak else 0.0)
        (wins if future_return > 0 else losses).append(future_return)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    count = len(wins) + len(losses)
    return {
        "trades": count, "wins": len(wins),
        "win_rate_pct": len(wins) / count * 100 if count else 0.0,
        "net_return_pct": (equity - 1.0) * 100,
        "profit_factor": gross_win / gross_loss if gross_loss else ("INF" if gross_win else 0.0),
        "max_drawdown_pct": drawdown * 100,
    }


def _ranking_auc(predictions, labels_by_index):
    """ROC AUC for ranking scores; None when OOS labels contain one class."""
    positives = [float(p) for index, p, _ in predictions if labels_by_index[index] == 1]
    negatives = [float(p) for index, p, _ in predictions if labels_by_index[index] == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    return wins / (len(positives) * len(negatives))


def _benchmark_predictions(samples, returns, indices, kind):
    """Create deterministic, non-fitted benchmark scores on the same OOS rows."""
    output = []
    for index in indices:
        if kind == "always_long":
            score = 1.0
        elif kind == "positive_24h_momentum":
            score = 1.0 if samples[index][2] > 0.0 else 0.0
        else:
            raise ValueError("Unknown research benchmark")
        output.append((index, score, returns[index]))
    return output


def _probability_diagnostics(predictions, horizon=3, thresholds=(0.50, 0.52, 0.55, 0.58, 0.60)):
    """Describe model confidence and threshold sensitivity without selecting a threshold."""
    probabilities = [float(item[1]) for item in predictions]
    if not probabilities:
        return {
            "prediction_count": 0, "min": None, "mean": None, "median": None,
            "p90": None, "max": None, "thresholds": {},
            "interpretation": "NO_OUT_OF_SAMPLE_PREDICTIONS",
        }
    ordered = sorted(probabilities)

    def percentile(fraction):
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    sweep = {}
    for threshold in thresholds:
        sweep[f"{threshold:.2f}"] = _performance(predictions, float(threshold), int(horizon))
    return {
        "prediction_count": len(probabilities), "min": ordered[0],
        "mean": sum(probabilities) / len(probabilities),
        "median": percentile(0.50), "p90": percentile(0.90), "max": ordered[-1],
        "thresholds": sweep,
        "interpretation": "DIAGNOSTIC_ONLY_NOT_FOR_THRESHOLD_SELECTION",
    }


def research_candidate(rows, *, horizon=3, fee_rate=0.0026, slippage_bps=5.0,
                       probability_threshold=0.58, min_train=120, min_validation=20):
    """Run expanding-window walk-forward evaluation on closed OHLCV candles."""
    if not isinstance(rows, (list, tuple)) or len(rows) < 260:
        raise ValueError("At least 260 closed candles are required for model research")
    if isinstance(horizon, bool) or int(horizon) < 1 or int(horizon) > 24:
        raise ValueError("horizon must be between 1 and 24 candles")
    if isinstance(probability_threshold, bool):
        raise ValueError("probability_threshold must be between 0 and 1")
    threshold = float(probability_threshold)
    if not math.isfinite(threshold) or not 0.0 < threshold < 1.0:
        raise ValueError("probability_threshold must be between 0 and 1")
    for name, value in (("min_train", min_train), ("min_validation", min_validation)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    fee, slip = float(fee_rate), float(slippage_bps) / 10000.0
    if not math.isfinite(fee) or fee < 0 or fee >= 0.1 or not math.isfinite(slip) or slip < 0 or slip >= 0.1:
        raise ValueError("Invalid fee or slippage assumptions")
    parsed, previous = [], None
    for index, row in enumerate(rows):
        if len(row) < 6:
            raise ValueError(f"Invalid candle at index {index}")
        values = [float(row[j]) for j in range(6)]
        if not all(math.isfinite(x) for x in values) or values[0] < 0 or min(values[1:5]) <= 0 or values[5] < 0:
            raise ValueError(f"Invalid OHLCV values at index {index}")
        if values[3] > min(values[1], values[4]) or values[2] < max(values[1], values[4]) or values[3] > values[2]:
            raise ValueError(f"Inconsistent OHLC values at index {index}")
        if previous is not None and values[0] <= previous:
            raise ValueError("Candles must be strictly chronological")
        previous = values[0]
        parsed.append(values)
    samples, labels, future_returns = [], [], []
    for index in range(60, len(parsed) - int(horizon)):
        features = extract_features(parsed, index)
        # Signal is known only after candle[index] closes. The earliest
        # executable entry is the following candle's open, not that close.
        entry, exit_price = parsed[index + 1][1], parsed[index + int(horizon)][4]
        if features is None or entry <= 0:
            continue
        net_return = _net_return_after_costs(entry, exit_price, fee, slippage_bps)
        samples.append(features)
        labels.append(1 if net_return > 0 else 0)
        future_returns.append(net_return)
    if len(samples) < min_train + 3 * min_validation + int(horizon):
        raise ValueError("Not enough usable samples for three walk-forward validation folds")
    # Keep the final 15% completely untouched until the independent holdout.
    holdout_start = max(
        min_train + 3 * min_validation + 2 * int(horizon),
        int(len(samples) * 0.85),
    )
    validation_end = holdout_start
    first_test = max(min_train + int(horizon), int(validation_end * 0.55))
    fold_size = (validation_end - first_test) // 3
    folds, all_oos = [], []
    for fold in range(3):
        start = first_test + fold * fold_size
        end = validation_end if fold == 2 else start + fold_size
        train_end = start - int(horizon)
        if train_end < min_train or end - start < min_validation:
            continue
        model = _fit(samples[:train_end], labels[:train_end])
        predictions = []
        for i in range(start, end):
            probability = predict_probability(model, samples[i])
            predictions.append((i, probability, future_returns[i]))
        metrics = _performance(predictions, threshold, int(horizon))
        metrics.update({"fold": fold + 1, "train_samples": train_end, "validation_samples": end - start})
        folds.append(metrics)
        all_oos.extend(predictions)
    if len(folds) != 3:
        raise ValueError("Could not construct three valid walk-forward validation folds")

    # A separate final holdout is never used to fit the model evaluated on it.
    holdout_train_end = holdout_start - int(horizon)
    if holdout_train_end < min_train or len(samples) - holdout_start < min_validation:
        raise ValueError("Not enough untouched samples for the independent holdout")
    holdout_model = _fit(samples[:holdout_train_end], labels[:holdout_train_end])
    holdout_predictions = [
        (i, predict_probability(holdout_model, samples[i]), future_returns[i])
        for i in range(holdout_start, len(samples))
    ]
    aggregate = _performance(all_oos, threshold, int(horizon))
    independent_holdout = _performance(holdout_predictions, threshold, int(horizon))
    oos_indices = [item[0] for item in all_oos]
    holdout_indices = [item[0] for item in holdout_predictions]
    benchmarks = {
        "walk_forward_oos": {
            "always_long": _performance(
                _benchmark_predictions(samples, future_returns, oos_indices, "always_long"),
                0.5, int(horizon),
            ),
            "positive_24h_momentum": _performance(
                _benchmark_predictions(samples, future_returns, oos_indices, "positive_24h_momentum"),
                0.5, int(horizon),
            ),
        },
        "independent_holdout": {
            "always_long": _performance(
                _benchmark_predictions(samples, future_returns, holdout_indices, "always_long"),
                0.5, int(horizon),
            ),
            "positive_24h_momentum": _performance(
                _benchmark_predictions(samples, future_returns, holdout_indices, "positive_24h_momentum"),
                0.5, int(horizon),
            ),
        },
    }
    score_ranking = {
        "walk_forward_oos_roc_auc": _ranking_auc(all_oos, labels),
        "independent_holdout_roc_auc": _ranking_auc(holdout_predictions, labels),
        "interpretation": "RANKING_METRIC_ONLY_NOT_PROBABILITY_CALIBRATION",
    }
    probability_diagnostics = {
        "walk_forward_oos": _probability_diagnostics(all_oos, horizon),
        "independent_holdout": _probability_diagnostics(holdout_predictions, horizon),
        "configured_threshold": threshold,
        "warning": "Threshold sweep is descriptive only; do not choose a production threshold from the same OOS/holdout data.",
    }

    def regime_for(features):
        momentum_24 = float(features[2])
        if momentum_24 >= 0.03:
            return "BULL"
        if momentum_24 <= -0.03:
            return "BEAR"
        return "SIDEWAYS"

    def by_regime(predictions):
        grouped = {"BULL": [], "BEAR": [], "SIDEWAYS": []}
        for prediction in predictions:
            grouped[regime_for(samples[prediction[0]])].append(prediction)
        return {
            regime: {
                "observations": len(grouped[regime]),
                **_performance(grouped[regime], threshold, int(horizon)),
            }
            for regime in ("BULL", "BEAR", "SIDEWAYS")
        }

    regime_metrics = by_regime(all_oos)
    holdout_regime_metrics = by_regime(holdout_predictions)
    final_model = _fit(samples, labels)  # Shadow artifact only; never controls entries.
    positive_folds = sum(1 for item in folds if item["net_return_pct"] > 0)
    rejection_reasons = []
    reason_codes = []
    if aggregate["trades"] < 20:
        reason_codes.append("INSUFFICIENT_OOS_TRADES")
        rejection_reasons.append(f"Недостаточно вневыборочных сделок: {aggregate['trades']} из 20")
    if aggregate["net_return_pct"] <= 0:
        reason_codes.append("NON_POSITIVE_OOS_RETURN")
        rejection_reasons.append("Отрицательная или нулевая доходность walk-forward после издержек")
    pf = aggregate["profit_factor"]
    if pf != "INF" and float(pf) < 1.15:
        reason_codes.append("LOW_OOS_PROFIT_FACTOR")
        rejection_reasons.append(f"Profit Factor ниже 1.15: {pf}")
    if aggregate["max_drawdown_pct"] > 15.0:
        reason_codes.append("OOS_DRAWDOWN_TOO_HIGH")
        rejection_reasons.append(f"Просадка выше 15%: {aggregate['max_drawdown_pct']:.2f}%")
    always_long_oos = benchmarks["walk_forward_oos"]["always_long"]
    if aggregate["net_return_pct"] <= always_long_oos["net_return_pct"]:
        reason_codes.append("DOES_NOT_BEAT_ALWAYS_LONG")
        rejection_reasons.append("Модель не превзошла базовый benchmark BUY-AND-HOLD на walk-forward OOS")
    momentum_oos = benchmarks["walk_forward_oos"]["positive_24h_momentum"]
    if aggregate["net_return_pct"] <= momentum_oos["net_return_pct"]:
        reason_codes.append("DOES_NOT_BEAT_MOMENTUM_OOS")
        rejection_reasons.append("Модель не превзошла 24-часовой momentum benchmark на walk-forward OOS")
    always_long_holdout = benchmarks["independent_holdout"]["always_long"]
    if independent_holdout["net_return_pct"] <= always_long_holdout["net_return_pct"]:
        reason_codes.append("DOES_NOT_BEAT_ALWAYS_LONG_HOLDOUT")
        rejection_reasons.append("Модель не превзошла BUY-AND-HOLD на независимом holdout")
    momentum_holdout = benchmarks["independent_holdout"]["positive_24h_momentum"]
    if independent_holdout["net_return_pct"] <= momentum_holdout["net_return_pct"]:
        reason_codes.append("DOES_NOT_BEAT_MOMENTUM_HOLDOUT")
        rejection_reasons.append("Модель не превзошла 24-часовой momentum benchmark на независимом holdout")
    if positive_folds < 2:
        reason_codes.append("UNSTABLE_WALK_FORWARD")
        rejection_reasons.append(f"Нестабильные фолды: положительных {positive_folds} из 3")

    if independent_holdout["trades"] < 5:
        reason_codes.append("INSUFFICIENT_HOLDOUT_TRADES")
        rejection_reasons.append(
            f"Недостаточно сделок на независимом отложенном периоде: {independent_holdout['trades']} из 5"
        )
    if independent_holdout["net_return_pct"] <= 0:
        reason_codes.append("HOLDOUT_NON_POSITIVE_RETURN")
        rejection_reasons.append("Независимый holdout не подтвердил положительную доходность")
    holdout_pf = independent_holdout["profit_factor"]
    if holdout_pf != "INF" and float(holdout_pf) < 1.0:
        reason_codes.append("HOLDOUT_PROFIT_FACTOR_TOO_LOW")
        rejection_reasons.append(f"Profit Factor независимого holdout ниже 1.00: {holdout_pf}")
    if independent_holdout["max_drawdown_pct"] > 15.0:
        reason_codes.append("HOLDOUT_DRAWDOWN_TOO_HIGH")
        rejection_reasons.append(
            f"Просадка независимого holdout выше 15%: {independent_holdout['max_drawdown_pct']:.2f}%"
        )

    for regime in ("BULL", "BEAR", "SIDEWAYS"):
        metrics = regime_metrics[regime]
        if metrics["observations"] < 10:
            reason_codes.append(f"INSUFFICIENT_{regime}_DATA")
            rejection_reasons.append(
                f"Недостаточно независимых наблюдений для режима {regime}: {metrics['observations']} из 10"
            )
        elif metrics["trades"] < 2:
            reason_codes.append(f"INSUFFICIENT_{regime}_TRADES")
            rejection_reasons.append(f"Недостаточно сделок для оценки режима {regime}")
        elif metrics["net_return_pct"] <= 0:
            reason_codes.append(f"WEAK_{regime}_PERFORMANCE")
            rejection_reasons.append(
                f"Стратегия не показала положительный результат в режиме {regime}"
            )

    research_pass = not rejection_reasons
    return {
        "status": "SHADOW_ONLY", "research_pass": research_pass, "samples": len(samples),
        "positive_labels_pct": sum(labels) / len(labels) * 100,
        "horizon_candles": int(horizon), "probability_threshold": threshold,
        "assumptions": {
            "fee_rate_per_side": fee,
            "slippage_bps_per_side": float(slippage_bps),
            "signal": "after signal candle close",
            "entry": "next candle open with adverse slippage",
            "exit": "close of horizon candle with adverse slippage",
            "validation": "3 expanding-window folds with purged label horizon",
            "benchmarks": "must beat both buy-and-hold and positive 24-candle momentum after costs",
            "holdout_risk_limit": "independent holdout max drawdown must not exceed 15%",
            "independent_holdout": "final 15% chronological block; not used for fitting its evaluator",
            "regime_definition": "24-candle return >= +3% bull; <= -3% bear; otherwise sideways",
        },
        "walk_forward": folds,
        "out_of_sample": aggregate,
        "independent_holdout": independent_holdout,
        "regime_metrics": regime_metrics,
        "independent_holdout_by_regime": holdout_regime_metrics,
        "probability_diagnostics": probability_diagnostics,
        "benchmark_metrics": benchmarks,
        "score_ranking": score_ranking,
        "diagnostics": {
            "reason_codes": reason_codes,
            "rejection_reasons": rejection_reasons,
            "positive_walk_forward_folds": positive_folds,
            "walk_forward_folds": len(folds),
            "independent_holdout_samples": len(holdout_predictions),
        },
        "rejection_reasons": rejection_reasons,
        "model": {"feature_names": list(FEATURE_NAMES), **final_model},
        "warning": "Research only. Candidate is not connected to PAPER order decisions and is never auto-promoted.",
    }


def rotate_research_batch(eligible, cursor, batch_size):
    """Return a bounded rotating slice so lower-ranked markets are researched too.

    The caller supplies a deterministically ordered market list (for example,
    liquidity-ranked Kraken markets). The cursor is persisted between runs.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    markets = list(eligible)
    if not markets:
        return [], 0
    try:
        start = int(cursor or 0) % len(markets)
    except (TypeError, ValueError, OverflowError):
        start = 0
    rotated = markets[start:] + markets[:start]
    selected = rotated[:min(batch_size, len(markets))]
    return selected, (start + len(selected)) % len(markets)
