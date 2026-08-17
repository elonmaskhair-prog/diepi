"""Persistence tests for the optional result status contract."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.data.result_storage import ResultStorage
from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.broker.replay import CashAuditBundle, CashReplaySeed
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)


def _portfolio_result():
    return PortfolioResult(
        start_date="20240102",
        end_date="20240131",
        initial_cash=1_000_000.0,
        final_value=1_100_000.0,
        total_return=0.1,
        annual_return=1.2,
        max_drawdown=0.03,
        trade_count=4,
        sharpe_ratio=1.5,
    )


def _audited_portfolio_result():
    seed = CashReplaySeed(
        as_of=datetime(2024, 1, 2),
        account_id="cash-account",
        initial_cash=1_000_000.0,
        initial_frozen_cash=0.0,
    )
    audit = CashAuditBundle.from_parts(seed, ExecutionEventJournal())
    return PortfolioResult(
        start_date="20240102",
        end_date="20240131",
        initial_cash=1_000_000.0,
        final_value=1_000_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        win_rate=None,
        sharpe_ratio=None,
        trades=[],
        cash_audit=audit,
    )


def _success_contract():
    return ResultContract(
        status=ResultStatus.SUCCESS,
        warnings=(ResultWarning("NOTICE", "Execution was conservative"),),
        assumptions=(
            ResultAssumption("execution.model", "worst_bar"),
        ),
        actual_interval=ActualInterval("2024-01-02", "2024-01-31"),
        data_coverage=DataCoverage(20, 20, 1.0),
    )


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(ResultStorage, "RESULTS_DIR", str(tmp_path))
    from diepi.backtest.data.report_generator import ReportGenerator

    monkeypatch.setattr(
        ReportGenerator,
        "generate",
        staticmethod(lambda *args, **kwargs: None),
    )
    return tmp_path


def _read_meta(folder):
    return json.loads((Path(folder) / "meta.json").read_text(encoding="utf-8"))


def _write_meta(folder, meta):
    (Path(folder) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_save_and_load_round_trip_optional_result_contract(isolated_storage):
    original = _portfolio_result()
    original.result_contract = _success_contract()

    folder = ResultStorage.save(original, {"frequency": "daily"}, "pass")
    meta = _read_meta(folder)
    restored, config, code = ResultStorage.load(folder)

    assert meta["version"] == "1.2"
    assert meta["result_contract"] == original.result_contract.to_dict()
    assert restored.result_contract == original.result_contract
    assert restored.result_contract is not original.result_contract
    assert config == {"frequency": "daily"}
    assert code == "pass"


def test_daily_metric_vectors_and_drawdown_variants_round_trip(
    isolated_storage,
):
    original = _portfolio_result()
    original.max_drawdown = 0.1
    original.max_drawdown_close_nav = 0.1
    original.max_drawdown_intraday_low_nav = 0.2
    original.max_drawdown_intraday_high_to_low = 0.3
    original.daily_values = pd.DataFrame(
        {
            "cash": [900_000.0, 1_100_000.0],
            "market_value": [0.0, 0.0],
            "total_value": [900_000.0, 1_100_000.0],
            # The first return is relative to initial_cash, so pct_change()
            # cannot reconstruct it from the persisted observations.
            "daily_return": [-0.1, 1_100_000.0 / 900_000.0 - 1.0],
            "drawdown_close_nav": [0.1, 0.0],
            "drawdown_intraday_low_nav": [0.2, 0.05],
            "drawdown_intraday_high_to_low": [0.3, 0.04],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    original.daily_values.index.name = "date"

    folder = ResultStorage.save(original, {}, "")
    meta = _read_meta(folder)
    persisted = pd.read_csv(
        Path(folder) / "每日净值.csv", encoding="utf-8-sig"
    )
    restored, _, _ = ResultStorage.load(folder)

    metric_columns = [
        "daily_return",
        "drawdown_close_nav",
        "drawdown_intraday_low_nav",
        "drawdown_intraday_high_to_low",
    ]
    assert persisted.loc[0, "daily_return"] == pytest.approx(-0.1)
    for column in metric_columns:
        assert persisted[column].tolist() == pytest.approx(
            original.daily_values[column].tolist()
        )
        assert restored.daily_values[column].tolist() == pytest.approx(
            original.daily_values[column].tolist()
        )
    assert meta["max_drawdown_close_nav"] == pytest.approx(0.1)
    assert meta["max_drawdown_intraday_low_nav"] == pytest.approx(0.2)
    assert meta["max_drawdown_intraday_high_to_low"] == pytest.approx(0.3)
    assert restored.max_drawdown_close_nav == pytest.approx(0.1)
    assert restored.max_drawdown_intraday_low_nav == pytest.approx(0.2)
    assert restored.max_drawdown_intraday_high_to_low == pytest.approx(0.3)


def test_trade_without_profit_saves_and_loads_zero_profit(
    isolated_storage, monkeypatch
):
    monkeypatch.setattr(
        ResultStorage,
        "_batch_get_stock_names",
        classmethod(lambda cls, symbols: {}),
    )
    original = _portfolio_result()
    original.trade_count = 1
    original.trades = [
        {
            "time": "20240102",
            "symbol": "000001.SZ",
            "direction": "BUY",
            "shares": 100,
            "price": 10.0,
            "amount": 1_000.0,
        }
    ]

    folder = ResultStorage.save(original, {}, "")
    restored, _, _ = ResultStorage.load(folder)

    assert restored.trades[0]["profit"] == pytest.approx(0.0)


def test_save_and_load_result_without_contract_stays_unclassified(
    isolated_storage,
):
    original = _portfolio_result()

    folder = ResultStorage.save(original, {}, "")
    meta = _read_meta(folder)
    restored, _, _ = ResultStorage.load(folder)

    assert meta["result_contract"] is None
    assert restored.result_contract is None


def test_load_legacy_archive_missing_contract_does_not_infer_success(
    isolated_storage,
):
    original = _portfolio_result()
    folder = ResultStorage.save(original, {}, "")
    meta = _read_meta(folder)
    del meta["result_contract"]
    meta["version"] = "1.0"
    _write_meta(folder, meta)

    restored, _, _ = ResultStorage.load(folder)

    assert restored.total_return > 0
    assert restored.result_contract is None


def test_load_explicit_null_contract_does_not_infer_success(isolated_storage):
    folder = ResultStorage.save(_portfolio_result(), {}, "")

    restored, _, _ = ResultStorage.load(folder)

    assert restored.result_contract is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda contract: contract.pop("status"),
        lambda contract: contract.update({"unknown": "field"}),
        lambda contract: contract.update({"schema_version": 0}),
        lambda contract: contract.update({"semantics_version": "v0"}),
        lambda contract: contract.update({"rankable": False}),
        lambda contract: contract["data_coverage"].update({"ratio": float("nan")}),
        lambda contract: contract["data_coverage"].update(
            {"missing_observations": 1}
        ),
    ],
)
def test_load_rejects_corrupt_or_unsupported_stored_contract(
    isolated_storage, mutate
):
    original = _portfolio_result()
    original.result_contract = _success_contract()
    folder = ResultStorage.save(original, {}, "")
    meta = _read_meta(folder)
    mutate(meta["result_contract"])
    _write_meta(folder, meta)

    with pytest.raises((TypeError, ValueError)):
        ResultStorage.load(folder)


def test_load_rejects_duplicate_keys_inside_stored_contract(isolated_storage):
    original = _portfolio_result()
    original.result_contract = _success_contract()
    folder = ResultStorage.save(original, {}, "")
    meta_path = Path(folder) / "meta.json"
    raw = meta_path.read_text(encoding="utf-8")
    raw = raw.replace(
        '"schema_version": 1,',
        '"schema_version": 1,\n    "schema_version": 1,',
        1,
    )
    meta_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        ResultStorage.load(folder)


def test_save_rejects_foreign_contract_shape_and_cleans_partial_folder(
    isolated_storage,
):
    result = _portfolio_result()
    result.result_contract = _success_contract().to_dict()

    with pytest.raises(TypeError, match="exactly ResultContract"):
        ResultStorage.save(result, {}, "")

    assert list(isolated_storage.iterdir()) == []


def test_save_rejects_nonfinite_json_config_and_cleans_partial_folder(
    isolated_storage,
):
    result = _portfolio_result()
    result.result_contract = _success_contract()

    with pytest.raises(ValueError, match="Out of range float values"):
        ResultStorage.save(result, {"slippage": float("nan")}, "pass")

    assert list(isolated_storage.iterdir()) == []


def test_list_records_exposes_rankability_and_marks_legacy_unclassified(
    isolated_storage,
):
    legacy_folder = isolated_storage / "legacy"
    current_folder = isolated_storage / "current"
    legacy_folder.mkdir()
    current_folder.mkdir()

    common = {
        "start_date": "20240102",
        "end_date": "20240131",
        "total_return": 9.0,
    }
    _write_meta(
        legacy_folder,
        {**common, "save_time": "2024-01-01 00:00:00", "version": "1.0"},
    )
    _write_meta(
        current_folder,
        {
            **common,
            "save_time": "2024-01-02 00:00:00",
            "version": "1.1",
            "result_contract": _success_contract().to_dict(),
        },
    )

    records = {record["folder_name"]: record for record in ResultStorage.list_records()}

    assert records["legacy"]["result_status"] == "LEGACY_UNCLASSIFIED"
    assert records["legacy"]["rankable"] is False
    assert records["legacy"]["total_return"] == 9.0
    assert records["current"]["result_status"] == "SUCCESS"
    assert records["current"]["rankable"] is True


def test_list_records_skips_archive_with_invalid_contract(isolated_storage):
    folder = isolated_storage / "corrupt"
    folder.mkdir()
    _write_meta(
        folder,
        {
            "save_time": "2024-01-01 00:00:00",
            "result_contract": {"schema_version": 0},
        },
    )

    assert ResultStorage.list_records() == []


def test_non_success_contract_is_restored_without_becoming_rankable(
    isolated_storage,
):
    original = _portfolio_result()
    original.result_contract = ResultContract(
        status=ResultStatus.FAILED,
        reason=ResultReason("RUN_FAILED", "The run failed"),
    )

    folder = ResultStorage.save(original, {}, "")
    restored, _, _ = ResultStorage.load(folder)

    assert restored.result_contract.status is ResultStatus.FAILED
    assert restored.result_contract.is_rankable is False


def test_cash_audit_artifacts_are_bound_and_round_trip(isolated_storage):
    original = _audited_portfolio_result()

    folder = ResultStorage.save(original, {}, "pass")
    meta = _read_meta(folder)
    restored, _, _ = ResultStorage.load(folder)

    for key in ("seed", "journal"):
        descriptor = meta["cash_audit"][key]
        raw = (Path(folder) / descriptor["path"]).read_bytes()
        assert descriptor["byte_length"] == len(raw)
        assert descriptor["sha256"] == hashlib.sha256(raw).hexdigest()
    assert restored.cash_audit == original.cash_audit
    assert restored.initial_cash == original.initial_cash
    assert restored.final_value == original.final_value
    assert restored.total_return == original.total_return


def test_cash_audit_artifact_byte_tamper_is_rejected(isolated_storage):
    folder = ResultStorage.save(_audited_portfolio_result(), {}, "")
    artifact = Path(folder) / ResultStorage.EXECUTION_EVENT_JOURNAL_FILE
    artifact.write_bytes(artifact.read_bytes() + b" ")

    with pytest.raises(ValueError, match="byte_length mismatch"):
        ResultStorage.load(folder)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("initial_cash", 999_999.0, "initial_cash"),
        ("final_value", 999_999.0, "final_value"),
        ("total_return", 0.1, "total_return"),
        ("trade_count", 1, "trade_count"),
        ("win_rate", 0.0, "win_rate"),
    ],
)
def test_load_rejects_meta_that_disagrees_with_cash_replay(
    isolated_storage, field, value, message
):
    folder = ResultStorage.save(_audited_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    meta[field] = value
    _write_meta(folder, meta)

    with pytest.raises(ValueError, match=message):
        ResultStorage.load(folder)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update({"path": "../cash_replay_seed.json"}),
        lambda item: item.update({"byte_length": True}),
        lambda item: item.update({"sha256": item["sha256"].upper()}),
        lambda item: item.update({"unknown": 1}),
    ],
)
def test_cash_audit_descriptor_tamper_is_rejected(
    isolated_storage, mutate
):
    folder = ResultStorage.save(_audited_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    mutate(meta["cash_audit"]["seed"])
    _write_meta(folder, meta)

    with pytest.raises((TypeError, ValueError)):
        ResultStorage.load(folder)


def test_cash_audit_artifact_symlink_is_rejected(isolated_storage):
    folder = ResultStorage.save(_audited_portfolio_result(), {}, "")
    artifact = Path(folder) / ResultStorage.CASH_REPLAY_SEED_FILE
    outside = isolated_storage / "outside-seed.json"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        os.symlink(outside, artifact)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(ValueError, match="must not be a symlink"):
        ResultStorage.load(folder)


def test_list_records_validates_only_cash_audit_manifest(
    isolated_storage, monkeypatch
):
    folder = ResultStorage.save(_audited_portfolio_result(), {}, "")

    def forbidden_full_load(*args, **kwargs):
        raise AssertionError("list_records must not read/replay cash artifacts")

    monkeypatch.setattr(
        ResultStorage,
        "_deserialize_cash_audit",
        classmethod(forbidden_full_load),
    )

    records = ResultStorage.list_records()

    assert [item["path"] for item in records] == [folder]


def test_repeated_saves_publish_distinct_complete_archives(isolated_storage):
    first = ResultStorage.save(_audited_portfolio_result(), {}, "first")
    second = ResultStorage.save(_audited_portfolio_result(), {}, "second")

    assert first != second
    assert Path(first).is_dir()
    assert Path(second).is_dir()
    assert not list(isolated_storage.glob(f"{ResultStorage.STAGING_PREFIX}*"))


def test_failed_save_preserves_published_archive_and_cleans_only_staging(
    isolated_storage, monkeypatch
):
    first = ResultStorage.save(_audited_portfolio_result(), {}, "first")
    from diepi.backtest.data.report_generator import ReportGenerator

    def fail_report(*args, **kwargs):
        raise RuntimeError("synthetic report failure")

    monkeypatch.setattr(ReportGenerator, "generate", fail_report)

    with pytest.raises(RuntimeError, match="synthetic report failure"):
        ResultStorage.save(_audited_portfolio_result(), {}, "second")

    assert Path(first).is_dir()
    assert (Path(first) / "meta.json").is_file()
    assert not list(isolated_storage.glob(f"{ResultStorage.STAGING_PREFIX}*"))


def test_save_revalidates_mutated_cash_result_before_staging(
    isolated_storage,
):
    result = _audited_portfolio_result()
    result.final_value += 1.0

    with pytest.raises(ValueError, match="final_value"):
        ResultStorage.save(result, {}, "")

    assert list(isolated_storage.iterdir()) == []


def test_delete_removes_only_a_direct_published_result(isolated_storage):
    record = isolated_storage / "published-result"
    record.mkdir()
    _write_meta(record, {"version": ResultStorage.VERSION})

    assert ResultStorage.delete(record) is True
    assert not record.exists()
    assert ResultStorage.delete(record) is False


def test_delete_rejects_root_outside_nested_and_non_result_directories(
    isolated_storage,
):
    # A forged marker at the root must not make RESULTS_DIR itself deletable.
    _write_meta(isolated_storage, {"version": ResultStorage.VERSION})

    outside = isolated_storage.parent / f"{isolated_storage.name}-outside"
    outside.mkdir()
    _write_meta(outside, {"version": ResultStorage.VERSION})

    record = isolated_storage / "published-result"
    nested = record / "nested"
    nested.mkdir(parents=True)
    _write_meta(record, {"version": ResultStorage.VERSION})
    _write_meta(nested, {"version": ResultStorage.VERSION})

    non_result = isolated_storage / "ordinary-directory"
    non_result.mkdir()

    for refused in (isolated_storage, outside, nested, non_result):
        assert ResultStorage.delete(refused) is False
        assert refused.is_dir()


def test_delete_rejects_symlinked_result_directory(isolated_storage):
    outside = isolated_storage.parent / f"{isolated_storage.name}-target"
    outside.mkdir()
    _write_meta(outside, {"version": ResultStorage.VERSION})
    link = isolated_storage / "linked-result"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    assert ResultStorage.delete(link) is False
    assert outside.is_dir()
