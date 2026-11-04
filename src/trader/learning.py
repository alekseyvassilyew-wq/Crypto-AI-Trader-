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
    for _ in range(epochs):
        gw1 = [[0.0] * hidden_units for _ in range(width)]
        gb1 = [0.0] * hidden_units
        gw2 = [0.0] * hidden_units
        gb2 = 0.0
        for row, label in zip(normalized, labels):
            hidden = [
                math.tanh(b1[j] + sum(w1[i][j] * row[i] for i in range(width)))
                for j in range(hidden_units)
            ]
            probability = _sigmoid(b2 + sum(w2[j] * hidden[j] for j in range(hidden_units)))
            output_delta = probability - label
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
        b2 -= learning_rate * gb2 / count
        for j in range(hidden_units):
            w2[j] -= learning_rate * (gw2[j] / count + l2 * w2[j])
            b1[j] -= learning_rate * gb1[j] / count
        for i in range(width):
            for j in range(hidden_units):
                w1[i][j] -= learning_rate * (gw1[i][j] / count + l2 * w1[i][j])
    return {
        "architecture": f"{width}-{hidden_units}-1",
        "means": means, "scales": scales,
        "weights_input_hidden": w1, "bias_hidden": b1,
        "weights_hidden_output": w2, "bias_output": b2,
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
            predictions.append((i, probability, future_returns[i]))
        metrics = _performance(predictions, threshold, int(horizon))
        metrics.update({"fold": fold + 1, "train_samples": train_end, "validation_samples": end - start})
        folds.append(metrics)
        all_oos.extend(predictions)
    if len(folds) != 3:
        raise ValueError("Could not construct three valid walk-forward folds")
    aggregate = _performance(all_oos, threshold, int(horizon))
    final_model = _fit(samples, labels)
    positive_folds = sum(1 for item in folds if item["net_return_pct"] > 0)
    research_pass = (
        aggregate["trades"] >= 20 and aggregate["net_return_pct"] > 0
        and (aggregate["profit_factor"] == "INF" or aggregate["profit_factor"] >= 1.15)
        and aggregate["max_drawdown_pct"] <= 15.0 and positive_folds >= 2
    )
    return {
        "status": "SHADOW_ONLY", "research_pass": research_pass, "samples": len(samples),
        "positive_labels_pct": sum(labels) / len(labels) * 100,
        "horizon_candles": int(horizon), "probability_threshold": threshold,
        "assumptions": {"fee_rate_per_side": fee, "slippage_bps_per_side": float(slippage_bps)},
        "walk_forward": folds, "out_of_sample": aggregate,
        "model": {"feature_names": list(FEATURE_NAMES), **final_model},
        "warning": "Research only. Candidate is not connected to PAPER order decisions and is never auto-promoted.",
    }
