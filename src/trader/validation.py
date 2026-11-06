"""Leakage-safe chronological fold construction for research backtests.

Indices are half-open. A configurable embargo separates each training window
from its out-of-sample test window so labels/forecasts cannot straddle the
boundary. This module only partitions data; it never changes trading settings.
"""

def build_walk_forward_folds(
    candles, *, min_train_candles=240, test_candles=120,
    step_candles=None, embargo_candles=3, max_folds=None,
):
    """Build expanding-window folds with disjoint chronological test windows.

    Returns dictionaries containing half-open train and test index ranges.
    Embargo bars are intentionally unused between training and testing.
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

    folds = []
    train_end = min_train_candles
    while True:
        test_start = train_end + embargo_candles
        test_end = test_start + test_candles
        if test_end > len(candles):
            break
        folds.append({
            "train_start": 0, "train_end": train_end,
            "embargo_start": train_end, "embargo_end": test_start,
            "test_start": test_start, "test_end": test_end,
        })
        if max_folds is not None and len(folds) >= max_folds:
            break
        train_end += step
    return folds
