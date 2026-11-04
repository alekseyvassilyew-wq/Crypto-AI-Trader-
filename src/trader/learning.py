"""Leakage-aware research models for PAPER strategy experiments.

This module never places orders or promotes a model. It trains a deterministic
logistic classifier using chronological walk-forward validation and returns a
candidate artifact for shadow observation only.
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


def _features(rows, end):
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


def _fit(samples, labels, epochs=350, learning_rate=0.12, l2=0.02):
    width = len(samples[0])
    means = [sum(row[j] for row in samples) / len(samples) for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in samples) / len(samples)
        scales.append(math.sqrt(variance) or 1.0)
    normalized = [[(row[j] - means[j]) / scales[j] for j in range(width)] for row in samples]
    weights, bias = [0.0] * width, 0.0
    for _ in range(epochs):
        grad, bias_grad = [0.0] * width, 0.0
        for row, label in zip(normalized, labels):
            error = _sigmoid(sum(w * x for w, x in zip(weights, row)) + bias) - label
            bias_grad += error
            for j, value in enumerate(row):
                grad[j] += error * value
        count = len(normalized)
        bias -= learning_rate * bias_grad / count
        for j in range(width):
            weights[j] -= learning_rate * (grad[j] / count + l2 * weights[j])
    return {"means": means, "scales": scales, "weights": weights, "bias": bias}


def predict_probability(model, features):
    normalized = [(float(value) - model["means"][i]) / model["scales"][i] for i, value in enumerate(features)]
    return _sigmoid(sum(w * x for w, x in zip(model["weights"], normalized)) + model["bias"])


def _performance(trades, threshold):
    equity, peak, drawdown = 1.0, 1.0, 0.0
    wins, losses = [], []
    for probability, future_return in trades:
        if probability < threshold:
            continue
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
        "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
        "max_drawdown_pct": drawdown * 100,
    }


def research_candidate(rows, *, horizon=3, fee_rate=0.0026, slippage_bps=5.0,
                       probability_threshold=0.58, min_train=120, min_validation=20):
    """Run expanding-window walk-forward evaluation on closed OHLCV candles."""
    if not isinstance(rows, (list, tuple)) or len(rows) < 260:
        raise ValueError("At least 260 closed candles are required for model research")
    if isinstance(horizon, bool) or int(horizon) < 1 or int(horizon) > 24:
        raise ValueError("horizon must be between 1 and 24 candles")
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
        features = _features(parsed, index)
        entry, exit_price = parsed[index][4], parsed[index + int(horizon)][4]
        if features is None or entry <= 0:
            continue
        net_return = exit_price * (1.0 - fee - slip) / (entry * (1.0 + fee + slip)) - 1.0
        samples.append(features)
        labels.append(1 if net_return > 0 else 0)
        future_returns.append(net_return)
    if len(samples) < min_train + 3 * min_validation + int(horizon):
        raise ValueError("Not enough usable samples for three walk-forward validation folds")
    first_test = max(min_train + int(horizon), int(len(samples) * 0.55))
    fold_size = (len(samples) - first_test) // 3
    folds, all_oos = [], []
    for fold in range(3):
        start = first_test + fold * fold_size
        end = len(samples) if fold == 2 else start + fold_size
        train_end = start - int(horizon)
        if train_end < min_train or end - start < min_validation:
            continue
        model = _fit(samples[:train_end], labels[:train_end])
        predictions = []
        for i in range(start, end):
            probability = predict_probability(model, samples[i])
            predictions.append((probability, future_returns[i]))
        metrics = _performance(predictions, probability_threshold)
        metrics.update({"fold": fold + 1, "train_samples": train_end, "validation_samples": end - start})
        folds.append(metrics)
        all_oos.extend(predictions)
    if len(folds) != 3:
        raise ValueError("Could not construct three valid walk-forward folds")
    aggregate = _performance(all_oos, probability_threshold)
    final_model = _fit(samples, labels)
    positive_folds = sum(1 for item in folds if item["net_return_pct"] > 0)
    research_pass = (
        aggregate["trades"] >= 20 and aggregate["net_return_pct"] > 0
        and aggregate["profit_factor"] >= 1.15
        and aggregate["max_drawdown_pct"] <= 15.0 and positive_folds >= 2
    )
    return {
        "status": "SHADOW_ONLY", "research_pass": research_pass, "samples": len(samples),
        "positive_labels_pct": sum(labels) / len(labels) * 100,
        "horizon_candles": int(horizon), "probability_threshold": float(probability_threshold),
        "assumptions": {"fee_rate_per_side": fee, "slippage_bps_per_side": float(slippage_bps)},
        "walk_forward": folds, "out_of_sample": aggregate,
        "model": {"feature_names": list(FEATURE_NAMES), **final_model},
        "warning": "Research only. Candidate is not connected to PAPER order decisions and is never auto-promoted.",
    }
