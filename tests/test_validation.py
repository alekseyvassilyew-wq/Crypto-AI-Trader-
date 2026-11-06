import pytest

from src.trader.validation import build_walk_forward_folds


def test_expanding_folds_are_chronological_disjoint_and_embargoed():
    rows = [[index] for index in range(700)]
    folds = build_walk_forward_folds(
        rows, min_train_candles=200, test_candles=100,
        step_candles=100, embargo_candles=3,
    )
    assert folds == [
        {"train_start": 0, "train_end": 200, "embargo_start": 200, "embargo_end": 203, "test_start": 203, "test_end": 303},
        {"train_start": 0, "train_end": 300, "embargo_start": 300, "embargo_end": 303, "test_start": 303, "test_end": 403},
        {"train_start": 0, "train_end": 400, "embargo_start": 400, "embargo_end": 403, "test_start": 403, "test_end": 503},
        {"train_start": 0, "train_end": 500, "embargo_start": 500, "embargo_end": 503, "test_start": 503, "test_end": 603},
    ]
    for previous, current in zip(folds, folds[1:]):
        assert current["train_end"] > previous["train_end"]
        assert current["test_start"] >= previous["test_end"]


def test_fold_builder_omits_incomplete_final_test_window():
    folds = build_walk_forward_folds(list(range(352)), min_train_candles=200, test_candles=100, embargo_candles=3)
    assert len(folds) == 1
    assert folds[0]["test_end"] == 303


def test_fold_builder_supports_a_bounded_number_of_folds():
    folds = build_walk_forward_folds(list(range(1200)), min_train_candles=200, test_candles=100, embargo_candles=3, max_folds=2)
    assert len(folds) == 2


@pytest.mark.parametrize("kwargs", [
    {"min_train_candles": 1}, {"test_candles": 0}, {"embargo_candles": -1},
    {"step_candles": 99, "test_candles": 100}, {"max_folds": 0},
    {"min_train_candles": True},
])
def test_fold_builder_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        build_walk_forward_folds(list(range(500)), **kwargs)


def test_fold_builder_rejects_non_sequence_input():
    with pytest.raises(ValueError, match="list or tuple"):
        build_walk_forward_folds(iter(range(500)))



def test_time_based_embargo_expands_when_candles_are_more_frequent_than_expected():
    # After the training slice, candles arrive every 10 minutes. A 3-hour
    # embargo therefore needs 18 rows, not the nominal three hourly rows.
    timestamps = [index * 3600 for index in range(200)]
    timestamps.extend(199 * 3600 + 3600 + index * 600 for index in range(40))
    candles = [[timestamp, 1, 1, 1, 1] for timestamp in timestamps]

    folds = build_walk_forward_folds(
        candles,
        min_train_candles=200,
        test_candles=10,
        embargo_seconds=3 * 3600,
        interval_seconds=3600,
    )

    assert folds
    first = folds[0]
    assert first["test_start"] == 218
    assert first["embargo_candles"] == 18
    assert timestamps[first["test_start"]] >= timestamps[199] + 3600 + 3 * 3600
    assert timestamps[first["test_start"] - 1] < timestamps[199] + 3600 + 3 * 3600


def test_time_based_embargo_handles_large_data_gaps_without_fixed_row_assumption():
    timestamps = [index * 3600 for index in range(200)]
    timestamps.extend([timestamps[-1] + 3600 + 4 * 3600 + index * 3600 for index in range(20)])
    candles = [[timestamp, 1, 1, 1, 1] for timestamp in timestamps]

    folds = build_walk_forward_folds(
        candles,
        min_train_candles=200,
        test_candles=10,
        embargo_seconds=3 * 3600,
        interval_seconds=3600,
    )

    assert folds[0]["test_start"] == 200
    assert folds[0]["embargo_candles"] == 0


@pytest.mark.parametrize("kwargs", [
    {"embargo_seconds": -1, "interval_seconds": 3600},
    {"embargo_seconds": 3600, "interval_seconds": 0},
    {"embargo_seconds": 3600},
])
def test_time_based_embargo_rejects_invalid_parameters(kwargs):
    candles = [[index * 3600, 1, 1, 1, 1] for index in range(300)]
    with pytest.raises(ValueError):
        build_walk_forward_folds(candles, **kwargs)


def test_time_based_embargo_rejects_non_monotonic_timestamps():
    candles = [[index * 3600, 1, 1, 1, 1] for index in range(300)]
    candles[250][0] = candles[249][0]
    with pytest.raises(ValueError, match="strictly increasing"):
        build_walk_forward_folds(
            candles,
            embargo_seconds=3 * 3600,
            interval_seconds=3600,
        )



def test_time_based_embargo_returns_no_folds_when_history_is_shorter_than_training_window():
    candles = [[index * 3600, 1, 1, 1, 1] for index in range(20)]
    assert build_walk_forward_folds(
        candles,
        min_train_candles=200,
        test_candles=10,
        embargo_seconds=3 * 3600,
        interval_seconds=3600,
    ) == []
