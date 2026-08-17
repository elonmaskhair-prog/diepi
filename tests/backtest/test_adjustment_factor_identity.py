"""Strict AFI-1 raw/HFQ adjustment-factor identity tests."""

import copy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from diepi.backtest.data.contract import (
    ADJUSTMENT_FACTOR_IDENTITY_SCHEMA,
    ADJUSTMENT_FACTOR_IDENTITY_VERSION,
    ADJUSTMENT_FACTOR_RELATION,
    ADJUSTMENT_PRICE_ABS_TOLERANCE,
    ADJUSTMENT_PRICE_REL_TOLERANCE,
    AdjustmentFactorError,
    AdjustmentFactorIdentity,
    AdjustmentFactorRunIdentity,
    DataContractError,
    Frequency,
    PriceSpace,
    adjustment_factor_assumption_value,
    adjustment_factor_identity_artifact,
    build_adjustment_factor_run_identity,
    inspect_pair,
    validate_adjustment_factor_ratio,
    validate_and_align_pair,
)
from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.exceptions import ParameterError


SYMBOL = "000001.SZ"
DATES = ("20240102", "20240103", "20240104")


def _factor_frame():
    return pd.DataFrame(
        {
            "ts_code": [SYMBOL] * 4,
            "trade_date": ["20231229", *DATES],
            "adj_factor": [2.0, 2.0, 4.0, 3.0],
        }
    )


def _daily_pair():
    raw_close = np.asarray([10.0, 5.0, 8.0], dtype="float64")
    ratios = np.asarray([1.0, 2.0, 1.5], dtype="float64")
    hfq_close = raw_close * ratios

    def frame(close):
        return pd.DataFrame(
            {
                "trade_date": list(DATES),
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "pre_close": close - 0.1,
                "amount": [1.0, 2.0, 3.0],
            }
        ).set_index("trade_date")

    return frame(hfq_close), frame(raw_close)


def _minute_pair():
    times = pd.to_datetime(
        [
            "2024-01-02 09:30",
            "2024-01-02 09:31",
            "2024-01-03 09:30",
            "2024-01-03 09:31",
            "2024-01-04 09:30",
        ]
    )
    raw_close = np.asarray([10.0, 10.1, 5.0, 5.1, 8.0])
    ratios = np.asarray([1.0, 1.0, 2.0, 2.0, 1.5])

    def frame(close, pre_close):
        return pd.DataFrame(
            {
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "pre_close": pre_close,
                "amount": np.arange(1.0, 6.0),
            },
            index=pd.DatetimeIndex(times, name="trade_time"),
        )

    raw_pre_close = np.asarray([9.9, 9.9, 4.9, 4.9, 7.9])
    hfq_pre_close = np.asarray([9.9, 9.9, 9.8, 9.8, 11.85])
    return frame(raw_close * ratios, hfq_pre_close), frame(raw_close, raw_pre_close)


def _validate(
    factors=None,
    *,
    strategy=None,
    execution=None,
    frequency=Frequency.DAILY,
    strategy_space=PriceSpace.HFQ,
    execution_space=PriceSpace.RAW,
):
    if strategy is None or execution is None:
        strategy, execution = (
            _daily_pair() if frequency is Frequency.DAILY else _minute_pair()
        )
    unit = "thousand_yuan" if frequency is Frequency.DAILY else "yuan"
    return validate_and_align_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=strategy_space,
        execution_price_space=execution_space,
        strategy_amount_unit=unit,
        execution_amount_unit=unit,
        frequency=frequency,
        adjustment_factors=_factor_frame() if factors is None else factors,
        adjustment_factor_source="fixture.adj_factor",
        require_adjustment_factor_identity=True,
    )


def _codes(error):
    return {issue.code for issue in error.value.report.issues}


def test_daily_identity_locks_existing_hfq_direction_and_base_definition():
    aligned = _validate()
    identity = aligned.adjustment_factor_identity

    assert identity is not None
    assert identity.base_date == "20231229"
    assert identity.base_factor == 2.0
    assert identity.relation == ADJUSTMENT_FACTOR_RELATION
    assert identity.scope_start == "20240102"
    assert identity.scope_end == "20240104"
    assert identity.scope_trade_days == 3
    assert identity.validated_close_rows == 3
    assert len(identity.scope_observation_keys_sha256) == 64
    assert identity.strategy_price_space is PriceSpace.HFQ
    assert identity.execution_price_space is PriceSpace.RAW
    assert identity.source == "fixture.adj_factor"
    assert identity.price_abs_tolerance == ADJUSTMENT_PRICE_ABS_TOLERANCE
    assert identity.price_rel_tolerance == ADJUSTMENT_PRICE_REL_TOLERANCE
    assert aligned.report.to_dict()["adjustment_factor_identity"] == (
        identity.to_dict()
    )


def test_reversing_track_roles_preserves_factor_and_scope_identity():
    hfq, raw = _daily_pair()
    forward = _validate(strategy=hfq, execution=raw).adjustment_factor_identity
    reverse = _validate(
        strategy=raw,
        execution=hfq,
        strategy_space=PriceSpace.RAW,
        execution_space=PriceSpace.HFQ,
    ).adjustment_factor_identity

    assert forward.factor_values_sha256 == reverse.factor_values_sha256
    assert forward.scope_trade_dates_sha256 == reverse.scope_trade_dates_sha256
    assert reverse.strategy_price_space is PriceSpace.RAW
    assert reverse.execution_price_space is PriceSpace.HFQ


def test_minute_identity_requires_one_factor_per_trade_day_not_per_bar():
    identity = _validate(frequency=Frequency.MINUTE).adjustment_factor_identity

    assert identity.frequency is Frequency.MINUTE
    assert identity.scope_trade_days == 3
    assert identity.validated_close_rows == 5

    hfq, raw = _minute_pair()
    shorter = _validate(
        frequency=Frequency.MINUTE,
        strategy=hfq.iloc[:-1].copy(deep=True),
        execution=raw.iloc[:-1].copy(deep=True),
    ).adjustment_factor_identity
    assert shorter.scope_end == "20240103"
    assert shorter.scope_observation_keys_sha256 != (
        identity.scope_observation_keys_sha256
    )
    one_bar = _validate(
        frequency=Frequency.MINUTE,
        strategy=hfq.iloc[:1].copy(deep=True),
        execution=raw.iloc[:1].copy(deep=True),
    ).adjustment_factor_identity
    two_bars = _validate(
        frequency=Frequency.MINUTE,
        strategy=hfq.iloc[:2].copy(deep=True),
        execution=raw.iloc[:2].copy(deep=True),
    ).adjustment_factor_identity
    assert one_bar.scope_start == two_bars.scope_start == "20240102"
    assert one_bar.scope_end == two_bars.scope_end == "20240102"
    assert one_bar.scope_observation_keys_sha256 != (
        two_bars.scope_observation_keys_sha256
    )


def test_scope_hash_changes_for_a_different_exact_trade_day_set():
    full = _validate().adjustment_factor_identity
    hfq, raw = _daily_pair()
    subset = _validate(
        strategy=hfq.iloc[:2].copy(deep=True),
        execution=raw.iloc[:2].copy(deep=True),
    ).adjustment_factor_identity

    assert subset.scope_start == "20240102"
    assert subset.scope_end == "20240103"
    assert subset.scope_trade_days == 2
    assert subset.scope_trade_dates_sha256 != full.scope_trade_dates_sha256
    assert subset.factor_values_sha256 != full.factor_values_sha256


def test_equivalent_ratio_with_scaled_exact_source_has_distinct_source_identity():
    baseline = _validate().adjustment_factor_identity
    scaled = _factor_frame()
    scaled["adj_factor"] *= 7.0
    observed = _validate(scaled).adjustment_factor_identity

    assert observed.scope_trade_dates_sha256 == baseline.scope_trade_dates_sha256
    assert observed.base_factor == 14.0
    assert observed.factor_values_sha256 != baseline.factor_values_sha256


@pytest.mark.parametrize("missing_date", DATES)
def test_missing_first_middle_or_last_scoped_factor_fails(missing_date):
    factors = _factor_frame()
    factors = factors[factors["trade_date"] != missing_date].reset_index(drop=True)

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert {"MISSING_ADJ_FACTOR", "ADJ_FACTOR_SCOPE_MISMATCH"} <= _codes(caught)
    missing = [
        issue
        for issue in caught.value.report.issues
        if issue.code == "MISSING_ADJ_FACTOR"
    ]
    assert any(missing_date in sample for issue in missing for sample in issue.sample_keys)
    assert caught.value.report.adjustment_factor_identity is None


def test_duplicate_required_factor_fails_without_deduplication():
    factors = _factor_frame()
    duplicate = factors[factors["trade_date"] == "20240103"]
    factors = pd.concat(
        [factors.iloc[:3], duplicate, factors.iloc[3:]], ignore_index=True
    )

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert "DUPLICATE_ADJ_FACTOR" in _codes(caught)


@pytest.mark.parametrize(
    "bad_value", [np.nan, np.inf, -np.inf, 0.0, -1.0, "2.0", True]
)
@pytest.mark.parametrize("bad_date", DATES)
def test_nonfinite_or_nonpositive_scoped_factor_fails(bad_value, bad_date):
    factors = _factor_frame()
    if isinstance(bad_value, (str, bool)):
        factors["adj_factor"] = factors["adj_factor"].astype(object)
    factors.loc[factors["trade_date"] == bad_date, "adj_factor"] = bad_value

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert "INVALID_ADJ_FACTOR" in _codes(caught)
    assert caught.value.report.adjustment_factor_identity is None


def test_invalid_base_factor_fails_even_when_scoped_rows_are_valid():
    factors = _factor_frame()
    factors.loc[0, "adj_factor"] = 0.0

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert "INVALID_ADJ_FACTOR" in _codes(caught)


def test_required_factor_rows_must_be_source_ordered():
    factors = _factor_frame().iloc[[0, 1, 3, 2]].reset_index(drop=True)

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert "NON_MONOTONIC_ADJ_FACTOR" in _codes(caught)


def test_factor_symbol_scope_mismatch_fails():
    factors = _factor_frame()
    factors.loc[factors["trade_date"] == "20240103", "ts_code"] = "000002.SZ"

    with pytest.raises(DataContractError) as caught:
        _validate(factors)

    assert "ADJ_FACTOR_SCOPE_MISMATCH" in _codes(caught)


def test_close_identity_mismatch_fails_and_does_not_publish_identity():
    hfq, raw = _daily_pair()
    hfq = hfq.copy(deep=True)
    hfq.loc["20240103", "close"] += 0.02

    with pytest.raises(DataContractError) as caught:
        _validate(strategy=hfq, execution=raw)

    assert "PRICE_IDENTITY_MISMATCH" in _codes(caught)
    assert caught.value.report.adjustment_factor_identity is None


def test_frozen_cent_rounding_tolerance_accepts_half_cent_only():
    hfq, raw = _daily_pair()
    accepted = hfq.copy(deep=True)
    accepted.loc["20240103", "close"] += 0.005
    assert _validate(strategy=accepted, execution=raw).report.ok

    rejected = hfq.copy(deep=True)
    rejected.loc["20240103", "close"] += 0.00501
    with pytest.raises(DataContractError) as caught:
        _validate(strategy=rejected, execution=raw)
    assert "PRICE_IDENTITY_MISMATCH" in _codes(caught)


@pytest.mark.parametrize(
    "future_row",
    [
        {"ts_code": SYMBOL, "trade_date": "20240105", "adj_factor": 3.1},
        {"ts_code": SYMBOL, "trade_date": "20240105", "adj_factor": np.nan},
        {"ts_code": SYMBOL, "trade_date": "20240105", "adj_factor": -1.0},
        {"ts_code": "WRONG", "trade_date": "20240105", "adj_factor": 1.0},
        {"ts_code": SYMBOL, "trade_date": "not-a-date", "adj_factor": np.inf},
        {"ts_code": SYMBOL, "trade_date": "20240103", "adj_factor": 999.0},
    ],
)
def test_future_append_cannot_change_historical_identity(future_row):
    baseline = _validate().adjustment_factor_identity
    extended = pd.concat(
        [_factor_frame(), pd.DataFrame([future_row])], ignore_index=True
    )

    observed = _validate(extended).adjustment_factor_identity

    assert observed == baseline
    assert observed.to_json() == baseline.to_json()


def test_input_row_and_container_mutation_cannot_change_published_identity():
    factors = _factor_frame()
    identity = _validate(factors).adjustment_factor_identity
    payload = identity.to_dict()
    factors.loc[:, "adj_factor"] = 999.0
    payload["source"] = "mutated"

    assert identity == _validate().adjustment_factor_identity
    assert identity.source == "fixture.adj_factor"


def test_identity_strict_roundtrip_and_assumption_bundle_are_deterministic():
    daily = _validate().adjustment_factor_identity
    minute = _validate(frequency=Frequency.MINUTE).adjustment_factor_identity

    restored = AdjustmentFactorIdentity.from_dict(copy.deepcopy(daily.to_dict()))
    assert restored == daily
    assert AdjustmentFactorIdentity.from_json(daily.to_json()) == daily
    assert json.loads(daily.to_json()) == daily.to_dict()
    assert daily.schema == ADJUSTMENT_FACTOR_IDENTITY_SCHEMA
    assert daily.identity_version == ADJUSTMENT_FACTOR_IDENTITY_VERSION
    first = adjustment_factor_assumption_value([daily, minute])
    second = adjustment_factor_assumption_value([minute, daily])
    assert first == second
    run_identity = AdjustmentFactorRunIdentity.from_json(first)
    assert run_identity.identity_count == 2
    assert run_identity.symbol_count == 1
    assert run_identity.frequencies == ("daily", "minute")
    assert len(first) < 1000
    assert run_identity == build_adjustment_factor_run_identity([minute, daily])
    assert build_adjustment_factor_run_identity([daily, daily]).identity_count == 1
    artifact = adjustment_factor_identity_artifact([daily, minute])
    assert run_identity.identity_stream_sha256 == hashlib.sha256(
        artifact.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("source"),
        lambda value: value.__setitem__("unknown", 1),
        lambda value: value.__setitem__("schema_version", True),
        lambda value: value.__setitem__("scope_trade_days", True),
        lambda value: value.__setitem__("base_factor", 2),
        lambda value: value.__setitem__("factor_values_sha256", "0" * 63),
        lambda value: value.__setitem__("source", " source"),
        lambda value: value.__setitem__("relation", "raw=hfq*ratio"),
    ],
)
def test_identity_roundtrip_rejects_noncanonical_or_corrupt_payload(mutate):
    payload = _validate().adjustment_factor_identity.to_dict()
    mutate(payload)

    with pytest.raises((TypeError, ValueError)):
        AdjustmentFactorIdentity.from_dict(payload)


def test_identity_json_rejects_pretty_duplicate_and_nonfinite_forms():
    identity = _validate().adjustment_factor_identity
    pretty = json.dumps(identity.to_dict(), indent=2)
    duplicate = identity.to_json().replace(
        '"schema_version":1', '"schema_version":1,"schema_version":1', 1
    )
    nonfinite = identity.to_json().replace('"base_factor":2.0', '"base_factor":NaN')

    for value in (pretty, duplicate, nonfinite):
        with pytest.raises((TypeError, ValueError)):
            AdjustmentFactorIdentity.from_json(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("identity_stream_sha256"),
        lambda value: value.__setitem__("identity_count", True),
        lambda value: value.__setitem__("frequencies", ["minute", "daily"]),
        lambda value: value.__setitem__("sources_sha256", "x" * 64),
        lambda value: value.__setitem__("scope_start", "2024-01-02"),
    ],
)
def test_compact_run_identity_rejects_corruption(mutate):
    daily = _validate().adjustment_factor_identity
    payload = build_adjustment_factor_run_identity([daily]).to_dict()
    mutate(payload)

    with pytest.raises((TypeError, ValueError)):
        AdjustmentFactorRunIdentity.from_dict(payload)


def test_direct_ratio_api_is_exact_day_and_never_carries_or_defaults():
    factors = _factor_frame()
    assert validate_adjustment_factor_ratio(
        factors,
        symbol=SYMBOL,
        trade_date="20240103",
        source="fixture.adj_factor",
    ) == 2.0

    for missing_date in ("20231228", "20240105"):
        with pytest.raises(AdjustmentFactorError) as caught:
            validate_adjustment_factor_ratio(
                factors,
                symbol=SYMBOL,
                trade_date=missing_date,
                source="fixture.adj_factor",
            )
        assert any(
            issue.code == "MISSING_ADJ_FACTOR" for issue in caught.value.issues
        )


class _FactorCache:
    def __init__(self, factors):
        self.factors = factors

    def get_adj_factor(self, _symbol):
        return self.factors.copy(deep=True)


def _ratio_provider(factors):
    provider = object.__new__(DataProvider)
    provider._cache = _FactorCache(factors)
    provider._adj_factor_cache = {}
    provider._adj_factor_ratio_cache = {}
    return provider


def _aligned_provider(factors):
    provider = _ratio_provider(factors)
    provider._context = None
    provider._price_mode = "hfq"
    provider._execution_price_mode = "raw"
    hfq, raw = _daily_pair()

    def get_daily(*_args, **kwargs):
        frame = hfq if kwargs["price_mode"] == "hfq" else raw
        start = kwargs.get("start")
        end = kwargs.get("end")
        selected = frame
        if start is not None:
            selected = selected[selected.index >= start]
        if end is not None:
            selected = selected[selected.index <= end]
        return selected.copy(deep=True)

    provider.get_daily = get_daily
    return provider


def test_provider_get_adj_ratio_uses_strict_exact_day_contract():
    provider = _ratio_provider(_factor_frame())
    assert provider.get_adj_ratio(SYMBOL, "20240104") == 1.5

    with pytest.raises(AdjustmentFactorError):
        provider.get_adj_ratio(SYMBOL, "20240105")
    with pytest.raises(ParameterError):
        provider.get_adj_ratio(None, "20240104")
    with pytest.raises(ParameterError):
        provider.get_adj_ratio(SYMBOL, None)


def test_provider_freezes_the_factor_snapshot_accepted_by_first_pair():
    provider = _aligned_provider(_factor_frame())
    first = provider.get_aligned_pair(
        SYMBOL, frequency="daily", start="20240102", end="20240104"
    ).adjustment_factor_identity
    provider._cache.factors = pd.concat(
        [
            provider._cache.factors,
            pd.DataFrame(
                [{
                    "ts_code": "WRONG",
                    "trade_date": "not-a-date",
                    "adj_factor": np.nan,
                }]
            ),
        ],
        ignore_index=True,
    )

    second = provider.get_aligned_pair(
        SYMBOL, frequency="daily", start="20240102", end="20240104"
    ).adjustment_factor_identity

    assert second == first
    assert provider.get_adj_ratio(SYMBOL, "20240103") == 2.0


def test_same_price_space_needs_no_factor_and_never_fabricates_identity():
    _hfq, raw = _daily_pair()
    aligned = validate_and_align_pair(
        raw,
        raw.copy(deep=True),
        symbol=SYMBOL,
        strategy_price_space="raw",
        execution_price_space="raw",
        strategy_amount_unit="thousand_yuan",
        execution_amount_unit="thousand_yuan",
        frequency="daily",
    )

    assert aligned.report.ok
    assert aligned.adjustment_factor_identity is None
    assert aligned.report.to_dict()["adjustment_factor_identity"] is None


def test_distinct_price_space_required_gate_never_fabricates_missing_identity():
    hfq, raw = _daily_pair()
    with pytest.raises(DataContractError) as caught:
        validate_and_align_pair(
            hfq,
            raw,
            symbol=SYMBOL,
            strategy_price_space="hfq",
            execution_price_space="raw",
            strategy_amount_unit="thousand_yuan",
            execution_amount_unit="thousand_yuan",
            frequency="daily",
            require_adjustment_factor_identity=True,
        )

    assert "MISSING_ADJ_FACTOR" in _codes(caught)
    assert caught.value.report.adjustment_factor_identity is None


def test_inspection_mode_returns_factor_failure_report_without_enforcing_it():
    hfq, raw = _daily_pair()
    broken = _factor_frame()
    broken.loc[broken["trade_date"] == "20240103", "adj_factor"] = np.nan

    report = inspect_pair(
        hfq,
        raw,
        symbol=SYMBOL,
        strategy_price_space="hfq",
        execution_price_space="raw",
        strategy_amount_unit="thousand_yuan",
        execution_amount_unit="thousand_yuan",
        frequency="daily",
        adjustment_factors=broken,
        adjustment_factor_source="fixture.adj_factor",
        require_adjustment_factor_identity=True,
    )

    assert report.status == "fail"
    assert "INVALID_ADJ_FACTOR" in report.issue_codes
    assert report.adjustment_factor_identity is None


def test_factor_gate_configuration_dimensions_are_exact():
    _hfq, raw = _daily_pair()
    kwargs = dict(
        symbol=SYMBOL,
        strategy_price_space="raw",
        execution_price_space="raw",
        strategy_amount_unit="thousand_yuan",
        execution_amount_unit="thousand_yuan",
        frequency="daily",
    )
    with pytest.raises(ValueError, match="one price space"):
        validate_and_align_pair(
            raw, raw.copy(deep=True),
            require_adjustment_factor_identity=True,
            **kwargs,
        )
    with pytest.raises(ValueError, match="invalid when both tracks"):
        validate_and_align_pair(
            raw, raw.copy(deep=True),
            adjustment_factors=_factor_frame(),
            adjustment_factor_source="fixture.adj_factor",
            **kwargs,
        )
    with pytest.raises(TypeError, match="exactly bool"):
        validate_and_align_pair(
            raw, raw.copy(deep=True),
            require_adjustment_factor_identity=1,
            **kwargs,
        )
