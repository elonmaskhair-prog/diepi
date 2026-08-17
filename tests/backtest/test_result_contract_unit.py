"""Pure unit tests for the engine-independent result status contract."""

from dataclasses import FrozenInstanceError
import json
import math

import pytest

from diepi.backtest.result_contract import (
    SCHEMA_VERSION,
    SEMANTICS_VERSION,
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)


def _reason(code="RUN_FAILED", message="The run failed"):
    return ResultReason(code, message)


def _interval(start="2024-01-02", end="2024-01-31"):
    return ActualInterval(start, end)


def _coverage(expected=20, actual=20):
    ratio = 0.0 if expected == 0 else actual / expected
    return DataCoverage(expected, actual, float(ratio))


def _success(**changes):
    values = {
        "status": ResultStatus.SUCCESS,
        "actual_interval": _interval(),
        "data_coverage": _coverage(),
    }
    values.update(changes)
    return ResultContract(**values)


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (ResultStatus.SUCCESS, "SUCCESS"),
        (ResultStatus.PARTIAL, "PARTIAL"),
        (ResultStatus.INVALID, "INVALID"),
        (ResultStatus.FAILED, "FAILED"),
        (ResultStatus.CANCELED, "CANCELED"),
    ],
)
def test_result_status_has_stable_public_values(member, value):
    assert member.value == value
    assert str(member) == f"ResultStatus.{value}"


@pytest.mark.parametrize(
    ("factory", "args"),
    [
        (ResultReason, ("DATA_INVALID", "Input data is invalid")),
        (ResultReason, ("X", "x")),
        (ResultWarning, ("LOW_LIQUIDITY", "Liquidity cap was binding")),
        (ResultWarning, ("W2", "Two")),
    ],
)
def test_diagnostics_accept_stable_codes_and_trimmed_text(factory, args):
    item = factory(*args)
    assert item.to_dict() == {"code": args[0], "message": args[1]}


@pytest.mark.parametrize(
    ("factory", "args", "error"),
    [
        (ResultReason, (1, "message"), TypeError),
        (ResultReason, (True, "message"), TypeError),
        (ResultReason, ("", "message"), ValueError),
        (ResultReason, ("lower", "message"), ValueError),
        (ResultReason, ("HAS-DASH", "message"), ValueError),
        (ResultReason, ("_LEADING", "message"), ValueError),
        (ResultReason, ("SPACE CODE", "message"), ValueError),
        (ResultReason, ("OK", 1), TypeError),
        (ResultReason, ("OK", None), TypeError),
        (ResultReason, ("OK", ""), ValueError),
        (ResultReason, ("OK", " leading"), ValueError),
        (ResultReason, ("OK", "trailing "), ValueError),
        (ResultReason, ("OK", "line\nbreak"), ValueError),
        (ResultWarning, (1, "message"), TypeError),
        (ResultWarning, ("bad", "message"), ValueError),
        (ResultWarning, ("OK", 1.0), TypeError),
        (ResultWarning, ("OK", ""), ValueError),
        (ResultWarning, ("OK", "tab\there"), ValueError),
    ],
)
def test_diagnostics_reject_ambiguous_or_non_exact_values(factory, args, error):
    with pytest.raises(error):
        factory(*args)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("execution_model", "worst_bar"),
        ("margin.close_checkpoint", "daily_close_only"),
        ("v1", "enabled"),
        ("signal_policy", "strict"),
        ("auction_cap", "zero"),
    ],
)
def test_assumptions_accept_orderable_immutable_strings(key, value):
    assumption = ResultAssumption(key, value)
    assert assumption.to_dict() == {"key": key, "value": value}


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        (1, "value", TypeError),
        (True, "value", TypeError),
        ("", "value", ValueError),
        ("UPPER", "value", ValueError),
        ("has-dash", "value", ValueError),
        ("has space", "value", ValueError),
        ("_leading", "value", ValueError),
        ("trailing_", "value", ValueError),
        ("double..dot", "value", ValueError),
        ("key", 1, TypeError),
        ("key", True, TypeError),
        ("key", None, TypeError),
        ("key", "", ValueError),
        ("key", " untrimmed", ValueError),
        ("key", "bad\nvalue", ValueError),
    ],
)
def test_assumptions_reject_unstable_keys_or_values(key, value, error):
    with pytest.raises(error):
        ResultAssumption(key, value)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2024-01-01", "2024-01-01"),
        ("2024-01-01", "2024-12-31"),
        ("2000-02-29", "2000-02-29"),
        ("0001-01-01", "9999-12-31"),
        ("2024-02-28", "2024-02-29"),
    ],
)
def test_actual_interval_accepts_inclusive_iso_ranges(start, end):
    interval = ActualInterval(start, end)
    assert interval.to_dict() == {"start_date": start, "end_date": end}


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        (20240101, "2024-01-02", TypeError),
        ("2024-01-01", 20240102, TypeError),
        (True, "2024-01-02", TypeError),
        ("20240101", "2024-01-02", ValueError),
        ("2024/01/01", "2024-01-02", ValueError),
        ("2024-1-01", "2024-01-02", ValueError),
        ("2024-01-1", "2024-01-02", ValueError),
        ("2024-13-01", "2024-01-02", ValueError),
        ("2024-00-01", "2024-01-02", ValueError),
        ("2024-02-30", "2024-03-01", ValueError),
        ("2023-02-29", "2023-03-01", ValueError),
        ("2024-01-02", "2024-01-01", ValueError),
        ("", "2024-01-01", ValueError),
        ("2024-01-01", "", ValueError),
    ],
)
def test_actual_interval_rejects_noncanonical_or_reversed_dates(start, end, error):
    with pytest.raises(error):
        ActualInterval(start, end)


@pytest.mark.parametrize(
    ("expected", "actual", "ratio", "missing"),
    [
        (0, 0, 0.0, 0),
        (1, 0, 0.0, 1),
        (1, 1, 1.0, 0),
        (2, 1, 0.5, 1),
        (3, 1, 1.0 / 3.0, 2),
        (10_000, 9_999, 0.9999, 1),
    ],
)
def test_data_coverage_accepts_consistent_finite_counts(
    expected, actual, ratio, missing
):
    coverage = DataCoverage(expected, actual, ratio)
    assert coverage.missing_observations == missing
    assert coverage.to_dict() == {
        "expected_observations": expected,
        "actual_observations": actual,
        "missing_observations": missing,
        "ratio": ratio,
    }


@pytest.mark.parametrize(
    ("expected", "actual", "ratio", "error"),
    [
        (True, 0, 0.0, TypeError),
        (1.0, 0, 0.0, TypeError),
        ("1", 0, 0.0, TypeError),
        (1, True, 1.0, TypeError),
        (1, 0.0, 0.0, TypeError),
        (1, "0", 0.0, TypeError),
        (-1, 0, 0.0, ValueError),
        (1, -1, 0.0, ValueError),
        (0, 1, 1.0, ValueError),
        (1, 2, 1.0, ValueError),
        (1, 1, 1, TypeError),
        (1, 1, True, TypeError),
        (1, 1, "1.0", TypeError),
        (1, 1, float("nan"), ValueError),
        (1, 1, float("inf"), ValueError),
        (1, 1, float("-inf"), ValueError),
        (1, 1, -0.1, ValueError),
        (1, 1, 1.1, ValueError),
        (0, 0, 1.0, ValueError),
        (2, 1, 0.4, ValueError),
        (3, 2, 0.666, ValueError),
        (100, 99, 0.98, ValueError),
    ],
)
def test_data_coverage_rejects_invalid_types_ranges_and_ratios(
    expected, actual, ratio, error
):
    with pytest.raises(error):
        DataCoverage(expected, actual, ratio)


@pytest.mark.parametrize(
    "contract",
    [
        _success(),
        ResultContract(
            ResultStatus.PARTIAL,
            _reason("EARLY_STOP", "Stopped before the requested end"),
            actual_interval=_interval("2024-01-02", "2024-01-10"),
            data_coverage=_coverage(20, 7),
        ),
        ResultContract(ResultStatus.INVALID, _reason("BAD_CONFIG", "Bad config")),
        ResultContract(
            ResultStatus.INVALID,
            _reason("NO_DATA", "No usable data"),
            data_coverage=_coverage(20, 0),
        ),
        ResultContract(ResultStatus.FAILED, _reason()),
        ResultContract(
            ResultStatus.FAILED,
            _reason(),
            actual_interval=_interval("2024-01-02", "2024-01-05"),
            data_coverage=_coverage(20, 4),
        ),
        ResultContract(
            ResultStatus.CANCELED,
            _reason("USER_CANCELED", "Canceled by caller"),
        ),
        ResultContract(
            ResultStatus.CANCELED,
            _reason("USER_CANCELED", "Canceled by caller"),
            actual_interval=_interval("2024-01-02", "2024-01-03"),
            data_coverage=_coverage(20, 2),
        ),
    ],
)
def test_valid_status_shapes_are_explicit_and_serializable(contract):
    payload = contract.to_dict()
    assert payload["status"] == contract.status.value
    assert payload["rankable"] is (contract.status is ResultStatus.SUCCESS)
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    ("status", "reason", "interval", "coverage", "message"),
    [
        (ResultStatus.SUCCESS, _reason(), _interval(), _coverage(), "reason"),
        (ResultStatus.SUCCESS, None, None, _coverage(), "actual_interval"),
        (ResultStatus.SUCCESS, None, _interval(), None, "data_coverage"),
        (ResultStatus.SUCCESS, None, _interval(), _coverage(20, 10), "complete"),
        (ResultStatus.SUCCESS, None, None, None, "actual_interval"),
        (ResultStatus.PARTIAL, None, _interval(), _coverage(20, 10), "reason"),
        (ResultStatus.PARTIAL, _reason(), None, _coverage(20, 10), "actual_interval"),
        (ResultStatus.PARTIAL, _reason(), _interval(), None, "data_coverage"),
        (ResultStatus.PARTIAL, _reason(), None, _coverage(20, 0), "actual_interval"),
        (ResultStatus.INVALID, None, None, None, "reason"),
        (ResultStatus.INVALID, _reason(), _interval(), _coverage(), "actual interval"),
        (ResultStatus.INVALID, _reason(), None, _coverage(20, 1), "actual_interval"),
        (ResultStatus.FAILED, None, None, None, "reason"),
        (ResultStatus.FAILED, _reason(), _interval(), _coverage(20, 0), "positive"),
        (ResultStatus.FAILED, _reason(), None, _coverage(20, 1), "actual_interval"),
        (ResultStatus.CANCELED, None, None, None, "reason"),
        (ResultStatus.CANCELED, _reason(), _interval(), None, "coverage"),
        (ResultStatus.CANCELED, _reason(), None, _coverage(2, 1), "actual_interval"),
    ],
)
def test_result_contract_rejects_incoherent_status_or_progress_shapes(
    status, reason, interval, coverage, message
):
    with pytest.raises(ValueError, match=message):
        ResultContract(
            status=status,
            reason=reason,
            actual_interval=interval,
            data_coverage=coverage,
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("status", "SUCCESS", TypeError),
        ("status", 1, TypeError),
        ("reason", {"code": "X", "message": "x"}, TypeError),
        ("warnings", [], TypeError),
        ("warnings", (_reason(),), TypeError),
        ("assumptions", [], TypeError),
        ("assumptions", (ResultWarning("X", "x"),), TypeError),
        ("actual_interval", ("2024-01-01", "2024-01-02"), TypeError),
        ("data_coverage", {"ratio": 1.0}, TypeError),
    ],
)
def test_result_contract_rejects_non_exact_nested_types(field, value, error):
    values = {
        "status": ResultStatus.SUCCESS,
        "actual_interval": _interval(),
        "data_coverage": _coverage(),
    }
    values[field] = value
    with pytest.raises(error):
        ResultContract(**values)


@pytest.mark.parametrize(
    ("warnings", "assumptions", "message"),
    [
        (
            (ResultWarning("DUP", "one"), ResultWarning("DUP", "two")),
            (),
            "warning codes",
        ),
        (
            (),
            (
                ResultAssumption("same", "one"),
                ResultAssumption("same", "two"),
            ),
            "assumption keys",
        ),
    ],
)
def test_result_contract_rejects_duplicate_diagnostic_identities(
    warnings, assumptions, message
):
    with pytest.raises(ValueError, match=message):
        _success(warnings=warnings, assumptions=assumptions)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("semantics_version", 1, TypeError),
        ("semantics_version", True, TypeError),
        ("semantics_version", "v0", ValueError),
        ("semantics_version", "V1", ValueError),
        ("schema_version", True, TypeError),
        ("schema_version", 1.0, TypeError),
        ("schema_version", 0, ValueError),
        ("schema_version", 2, ValueError),
    ],
)
def test_contract_rejects_unknown_or_non_exact_versions(field, value, error):
    with pytest.raises(error):
        _success(**{field: value})


@pytest.mark.parametrize(
    ("status", "rankable"),
    [
        (ResultStatus.SUCCESS, True),
        (ResultStatus.PARTIAL, False),
        (ResultStatus.INVALID, False),
        (ResultStatus.FAILED, False),
        (ResultStatus.CANCELED, False),
    ],
)
def test_only_success_is_rankable(status, rankable):
    if status is ResultStatus.SUCCESS:
        contract = _success()
    elif status is ResultStatus.PARTIAL:
        contract = ResultContract(
            status,
            _reason("PARTIAL_RUN", "Partial run"),
            actual_interval=_interval(),
            data_coverage=_coverage(20, 10),
        )
    else:
        contract = ResultContract(status, _reason())

    assert contract.is_rankable is rankable
    if rankable:
        assert contract.require_rankable() is contract
    else:
        with pytest.raises(ValueError, match="not rankable"):
            contract.require_rankable()


def test_serialization_is_canonical_clock_free_and_preserves_tuple_order():
    contract = _success(
        warnings=(
            ResultWarning("FIRST", "first warning"),
            ResultWarning("SECOND", "second warning"),
        ),
        assumptions=(
            ResultAssumption("z_model", "worst_bar"),
            ResultAssumption("a_policy", "strict"),
        ),
    )

    first = contract.to_json()
    second = contract.to_json()
    payload = json.loads(first)

    assert first == second
    assert first == json.dumps(
        contract.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert [item["code"] for item in payload["warnings"]] == ["FIRST", "SECOND"]
    assert [item["key"] for item in payload["assumptions"]] == [
        "z_model",
        "a_policy",
    ]
    assert not any(
        token in first.lower()
        for token in ("recorded_at", "created_at", "timestamp", "datetime.now")
    )


def test_success_serialization_has_versions_coverage_and_no_fake_error():
    payload = _success().to_dict()
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "semantics_version": SEMANTICS_VERSION,
        "status": "SUCCESS",
        "rankable": True,
        "reason": None,
        "warnings": [],
        "assumptions": [],
        "actual_interval": {
            "start_date": "2024-01-02",
            "end_date": "2024-01-31",
        },
        "data_coverage": {
            "expected_observations": 20,
            "actual_observations": 20,
            "missing_observations": 0,
            "ratio": 1.0,
        },
    }


@pytest.mark.parametrize(
    "status",
    [
        ResultStatus.PARTIAL,
        ResultStatus.INVALID,
        ResultStatus.FAILED,
        ResultStatus.CANCELED,
    ],
)
def test_non_success_serialization_cannot_masquerade_as_success(status):
    if status is ResultStatus.PARTIAL:
        contract = ResultContract(
            status,
            _reason("INCOMPLETE", "Incomplete run"),
            actual_interval=_interval(),
            data_coverage=_coverage(2, 1),
        )
    else:
        contract = ResultContract(status, _reason())
    payload = contract.to_dict()
    assert payload["status"] != "SUCCESS"
    assert payload["rankable"] is False
    assert payload["reason"] is not None


@pytest.mark.parametrize(
    ("constructor", "field", "replacement"),
    [
        (_reason, "code", "CHANGED"),
        (lambda: ResultWarning("W", "warning"), "message", "changed"),
        (lambda: ResultAssumption("key", "value"), "value", "changed"),
        (_interval, "end_date", "2025-01-01"),
        (_coverage, "ratio", 0.5),
        (_success, "status", ResultStatus.FAILED),
    ],
)
def test_all_public_contract_nodes_are_frozen(constructor, field, replacement):
    instance = constructor()
    with pytest.raises(FrozenInstanceError):
        setattr(instance, field, replacement)


def test_serialized_mutation_does_not_mutate_contract_state():
    contract = _success(
        warnings=(ResultWarning("NOTICE", "notice"),),
        assumptions=(ResultAssumption("model", "worst_bar"),),
    )
    payload = contract.to_dict()
    payload["warnings"][0]["message"] = "mutated"
    payload["warnings"].append({"code": "NEW", "message": "new"})
    payload["assumptions"][0]["value"] = "mutated"
    payload["data_coverage"]["ratio"] = math.nan

    fresh = contract.to_dict()
    assert fresh["warnings"] == [{"code": "NOTICE", "message": "notice"}]
    assert fresh["assumptions"] == [{"key": "model", "value": "worst_bar"}]
    assert fresh["data_coverage"]["ratio"] == 1.0


def test_contract_module_has_no_pandas_payload_or_mutable_internal_container():
    contract = _success(
        warnings=(ResultWarning("NOTICE", "notice"),),
        assumptions=(ResultAssumption("model", "worst_bar"),),
    )
    assert type(contract.warnings) is tuple
    assert type(contract.assumptions) is tuple
    assert all(
        "pandas" not in type(value).__module__
        for value in contract.__dict__.values()
        if value is not None
    )


def _wire_contracts():
    return (
        _success(
            warnings=(
                ResultWarning("FIRST", "First warning"),
                ResultWarning("SECOND", "Second warning"),
            ),
            assumptions=(
                ResultAssumption("execution.model", "worst_bar"),
                ResultAssumption("signal_policy", "strict"),
            ),
        ),
        ResultContract(
            ResultStatus.PARTIAL,
            _reason("EARLY_STOP", "Stopped early"),
            actual_interval=_interval("2024-01-02", "2024-01-10"),
            data_coverage=_coverage(20, 7),
        ),
        ResultContract(
            ResultStatus.INVALID,
            _reason("NO_DATA", "No usable data"),
            data_coverage=_coverage(20, 0),
        ),
        ResultContract(ResultStatus.FAILED, _reason()),
        ResultContract(
            ResultStatus.CANCELED,
            _reason("USER_CANCELED", "Canceled by caller"),
        ),
    )


@pytest.mark.parametrize("contract", _wire_contracts())
def test_from_dict_and_from_json_round_trip_every_status(contract):
    from_dict = ResultContract.from_dict(contract.to_dict())
    from_json = ResultContract.from_json(contract.to_json())

    assert from_dict == contract
    assert from_json == contract
    assert from_json.to_json() == contract.to_json()


def test_from_dict_preserves_serialized_warning_and_assumption_order():
    contract = ResultContract.from_dict(_wire_contracts()[0].to_dict())

    assert [warning.code for warning in contract.warnings] == [
        "FIRST",
        "SECOND",
    ]
    assert [assumption.key for assumption in contract.assumptions] == [
        "execution.model",
        "signal_policy",
    ]


@pytest.mark.parametrize("field", sorted(_success().to_dict()))
def test_from_dict_rejects_every_missing_top_level_field(field):
    payload = _success().to_dict()
    del payload[field]

    with pytest.raises(ValueError, match="missing required"):
        ResultContract.from_dict(payload)


def test_from_dict_rejects_unknown_top_level_field():
    payload = _success().to_dict()
    payload["future_field"] = "not understood"

    with pytest.raises(ValueError, match="unknown field"):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", 2, ValueError),
        ("schema_version", 1.0, TypeError),
        ("schema_version", True, TypeError),
        ("semantics_version", "v0", ValueError),
        ("semantics_version", 1, TypeError),
        ("status", "UNKNOWN", ValueError),
        ("status", ResultStatus.SUCCESS, TypeError),
        ("rankable", 1, TypeError),
        ("rankable", "true", TypeError),
        ("warnings", (), TypeError),
        ("assumptions", (), TypeError),
    ],
)
def test_from_dict_rejects_old_unknown_or_non_wire_values(field, value, error):
    payload = _success().to_dict()
    payload[field] = value

    with pytest.raises(error):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("reason",), {"code": "X"}),
        (("reason",), {"code": "X", "message": "x", "extra": 1}),
        (("warnings",), [{"code": "W"}]),
        (
            ("warnings",),
            [{"code": "W", "message": "warning", "extra": 1}],
        ),
        (("assumptions",), [{"key": "model"}]),
        (
            ("assumptions",),
            [{"key": "model", "value": "strict", "extra": 1}],
        ),
        (("actual_interval",), {"start_date": "2024-01-02"}),
        (
            ("actual_interval",),
            {
                "start_date": "2024-01-02",
                "end_date": "2024-01-31",
                "extra": 1,
            },
        ),
        (
            ("data_coverage",),
            {
                "expected_observations": 20,
                "actual_observations": 20,
                "ratio": 1.0,
            },
        ),
        (
            ("data_coverage",),
            {
                "expected_observations": 20,
                "actual_observations": 20,
                "missing_observations": 0,
                "ratio": 1.0,
                "extra": 1,
            },
        ),
    ],
)
def test_from_dict_rejects_missing_or_unknown_nested_fields(path, replacement):
    payload = ResultContract(
        ResultStatus.PARTIAL,
        _reason("PARTIAL_RUN", "Partial run"),
        warnings=(ResultWarning("W", "warning"),),
        assumptions=(ResultAssumption("model", "strict"),),
        actual_interval=_interval(),
        data_coverage=_coverage(20, 10),
    ).to_dict()
    payload[path[0]] = replacement

    with pytest.raises(ValueError):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", []),
        ("warnings", {}),
        ("warnings", ["warning"]),
        ("assumptions", {}),
        ("assumptions", ["assumption"]),
        ("actual_interval", []),
        ("data_coverage", []),
    ],
)
def test_from_dict_rejects_wrong_nested_container_types(field, value):
    payload = _success().to_dict()
    payload[field] = value

    with pytest.raises(TypeError):
        ResultContract.from_dict(payload)


def test_from_dict_rejects_rankable_derived_field_tampering():
    payload = _success().to_dict()
    payload["rankable"] = False

    with pytest.raises(ValueError, match="rankable"):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize("missing", [1, -1, True, 0.0])
def test_from_dict_rejects_missing_observations_tampering(missing):
    payload = _success().to_dict()
    payload["data_coverage"]["missing_observations"] = missing

    with pytest.raises((TypeError, ValueError), match="missing_observations"):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize("ratio", [math.nan, math.inf, -math.inf])
def test_from_dict_rejects_non_finite_coverage_ratio(ratio):
    payload = _success().to_dict()
    payload["data_coverage"]["ratio"] = ratio

    with pytest.raises(ValueError, match="finite"):
        ResultContract.from_dict(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_from_json_rejects_nonstandard_non_finite_constants(constant):
    payload = _success().to_json().replace('"ratio":1.0', f'"ratio":{constant}')

    with pytest.raises(ValueError, match="non-finite JSON constant"):
        ResultContract.from_json(payload)


def test_from_json_rejects_overflowing_finite_number():
    payload = _success().to_json().replace('"ratio":1.0', '"ratio":1e999')

    with pytest.raises(ValueError, match="finite"):
        ResultContract.from_json(payload)


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"schema_version":1}',
        _success().to_json().replace(
            '"start_date":"2024-01-02"',
            '"start_date":"2024-01-02","start_date":"2024-01-02"',
        ),
    ],
)
def test_from_json_rejects_duplicate_keys_at_any_depth(payload):
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        ResultContract.from_json(payload)


@pytest.mark.parametrize("payload", ["null", "[]", '"value"', "1"])
def test_from_json_rejects_non_object_roots(payload):
    with pytest.raises(TypeError, match="dict"):
        ResultContract.from_json(payload)


@pytest.mark.parametrize("payload", [b"{}", None, {}, 1])
def test_from_json_requires_an_exact_string(payload):
    with pytest.raises(TypeError, match="exactly str"):
        ResultContract.from_json(payload)


def test_from_dict_copies_mutable_wire_containers():
    payload = _wire_contracts()[0].to_dict()
    contract = ResultContract.from_dict(payload)

    payload["warnings"][0]["message"] = "mutated"
    payload["assumptions"].clear()
    payload["data_coverage"]["ratio"] = math.nan

    assert contract.warnings[0].message == "First warning"
    assert len(contract.assumptions) == 2
    assert contract.data_coverage.ratio == 1.0
