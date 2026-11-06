"""Leakage-safe chronological fold construction for research backtests.

Indices are half-open. A configurable embargo separates each training window
from its out-of-sample test window so labels/forecasts cannot straddle the
boundary. This module only partitions data; it never changes trading settings.
"""


def build_walk_forward_folds(
    candles, *, min_train_candles=240, test_candles=120,
    step_candles=None, embargo_candles=3, max_folds=None,
    embargo_seconds=None, interval_seconds=None, timestamp_index=0,
):
    """Build expanding-window folds with disjoint chronological test windows.

    By default, embargo_candles preserves the original row-count behavior.
    When embargo_seconds is supplied, test_start is the first candle whose
    timestamp is at least the final training candle's close time plus the
    requested embargo duration. This prevents missing/irregular bars from
    silently shortening a time-based forecast embargo.

    Returns dictionaries containing half-open train and test index ranges.
    Only complete test windows are returned.
    """
    if not isinstance(candles, (list, tuple)):
        raise ValueError("candles must be a list or tuple")
    for name, value in (
        ("min_train_candles", min_train_candles),
        ("test_candles", test_candles),
        ("embargo_candles", embargo_candles),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if min_train_candles < 2:
        raise ValueError("min_train_candles must be at least 2")
    if test_candles < 1:
        raise ValueError("test_candles must be at least 1")
    if embargo_candles < 0:
        raise ValueError("embargo_candles must be non-negative")
    step = test_candles if step_candles is None else step_candles
    if isinstance(step, bool) or not isinstance(step, int) or step < test_candles:
        raise ValueError("step_candles must be an integer at least test_candles")
    if max_folds is not None:
        if isinstance(max_folds, bool) or not isinstance(max_folds, int) or max_folds < 1:
            raise ValueError("max_folds must be a positive integer")

    time_based = embargo_seconds is not None
    if time_based:
        if isinstance(embargo_seconds, bool) or not isinstance(embargo_seconds, (int, float)):
            raise ValueError("embargo_seconds must be a non-negative number")
        if embargo_seconds < 0:
            raise ValueError("embargo_seconds must be non-negative")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be a positive number")
        if isinstance(timestamp_index, bool) or not isinstance(timestamp_index, int) or timestamp_index < 0:
            raise ValueError("timestamp_index must be a non-negative integer")
        timestamps = []
        for row in candles:
            try:
                timestamps.append(float(row[timestamp_index]))
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError("candles must contain numeric timestamps") from exc
        if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
            raise ValueError("candle timestamps must be strictly increasing")
    else:
        timestamps = None

    folds = []
    train_end = min_train_candles
    while True:
        if train_end > len(candles):
            break
        if time_based:
            cutoff = timestamps[train_end - 1] + float(interval_seconds) + float(embargo_seconds)
            test_start = train_end
            while test_start < len(candles) and timestamps[test_start] < cutoff:
                test_start += 1
            actual_embargo_candles = test_start - train_end
        else:
            test_start = train_end + embargo_candles
            actual_embargo_candles = embargo_candles
        test_end = test_start + test_candles
        if test_end > len(candles):
            break
        fold = {
            "train_start": 0, "train_end": train_end,
            "embargo_start": train_end, "embargo_end": test_start,
            "test_start": test_start, "test_end": test_end,
        }
        if time_based:
            fold["embargo_candles"] = actual_embargo_candles
            fold["embargo_seconds"] = float(embargo_seconds)
        folds.append(fold)
        if max_folds is not None and len(folds) >= max_folds:
            break
        train_end += step
    return folds
