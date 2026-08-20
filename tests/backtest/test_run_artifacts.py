"""Round-trip, integrity, and publication tests for RunArtifact v1."""

from datetime import datetime
import hashlib
import json
import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from diepi.artifacts import (
    ArtifactPayload,
    ArtifactStore,
    EngineKind,
    ErrorCategory,
    RunError,
    RunOutcome,
    RunProvenance,
    RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES,
    RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES,
    RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT,
    RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES,
    SourceFingerprint,
    build_futures_outcome,
    build_parallel_outcome,
    load_legacy_result,
)
import diepi.artifacts.storage as artifact_storage
from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.broker.replay import CashAuditBundle, CashReplaySeed
from diepi.backtest.broker import Account, Broker, cash_replay_trade_records
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.target_execution import TargetExecutionBundle
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.parallel_runner import ParallelResult
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.data.result_storage import ResultStorage
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultContract,
    ResultReason,
    ResultStatus,
)
from diepi.futures.result import FuturesResult, ValidationState


def test_source_fingerprint_streams_real_temp_file_and_parquet(tmp_path):
    binary = tmp_path / 'plain.bin'
    binary.write_bytes(b'plain-source')
    parquet = tmp_path / 'daily.parquet'
    pd.DataFrame([{
        'trade_date': '20240102', 'close': 10.0,
    }]).to_parquet(parquet, index=False)

    plain_source = SourceFingerprint.from_file(
        binary, root=tmp_path, kind='market_data'
    )
    parquet_source = SourceFingerprint.from_file(
        parquet, root=tmp_path, kind='market_data'
    )

    assert plain_source.byte_length == len(b'plain-source')
    assert plain_source.logical_path == 'plain.bin'
    assert parquet_source.byte_length == parquet.stat().st_size
    assert parquet_source.logical_path == 'daily.parquet'


def test_source_fingerprint_rejects_handle_generation_change(
    tmp_path, monkeypatch,
):
    import diepi.artifacts.provenance as provenance_module

    source = tmp_path / 'source.bin'
    source.write_bytes(b'stable-size')
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        value = real_fstat(fd)
        calls += 1
        if calls < 2:
            return value
        return SimpleNamespace(
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns + 1,
        )

    monkeypatch.setattr(provenance_module.os, 'fstat', changing_fstat)

    with pytest.raises(OSError, match='changed while fingerprinting'):
        SourceFingerprint.from_file(
            source, root=tmp_path, kind='market_data'
        )


def test_source_fingerprint_rejects_a_hard_link(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"shared-source")
    link = tmp_path / "source.bin"
    try:
        os.link(target, link)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")

    with pytest.raises(ValueError, match="single-link regular file"):
        SourceFingerprint.from_file(
            link, root=tmp_path, kind="market_data"
        )


def _contract():
    return ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval("2024-01-02", "2024-01-02"),
        data_coverage=DataCoverage(1, 1, 1.0),
    )


def _daily_values():
    frame = pd.DataFrame(
        {
            "cash": [1_000.0],
            "market_value": [0.0],
            "total_value": [1_000.0],
            "daily_return": [0.0],
        },
        index=pd.to_datetime(["20240102"], format="%Y%m%d"),
    )
    frame.index.name = "date"
    return frame


def _audit():
    seed = CashReplaySeed(
        as_of=datetime(2024, 1, 2),
        account_id="cash-account",
        initial_cash=1_000.0,
        initial_frozen_cash=0.0,
    )
    return CashAuditBundle.from_parts(seed, ExecutionEventJournal())


def _single_result():
    return BacktestResult(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240102",
        initial_cash=1_000.0,
        final_value=1_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        win_rate=None,
        sharpe_ratio=None,
        daily_values=_daily_values(),
        trades=[],
        result_contract=_contract(),
        target_execution=TargetExecutionBundle.empty(),
        cash_audit=_audit(),
    )


def _portfolio_result():
    return PortfolioResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=1_000.0,
        final_value=1_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        win_rate=None,
        sharpe_ratio=None,
        daily_values=_daily_values(),
        trades=[],
        position_history=[
            {"date": "20240102", "symbol": "600000.SH", "quantity": 0}
        ],
        execution_stats={"submitted": 0, "fill_ratio": 0.0},
        result_contract=_contract(),
        target_execution=TargetExecutionBundle.empty(),
        cash_audit=_audit(),
    )


def _traded_single_result():
    account = Account(initial_cash=50_000.0)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(
        account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        transfer_fee_rate=0.00002,
    )
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 31),
        eligible_from=datetime(2024, 1, 2, 9, 32),
        expire_date="20240102",
    ):
        broker.buy_at_market("600000.SH", shares=100, current_price=10.0)
    broker.execute_market_orders(
        BarData(
            symbol="600000.SH",
            trade_time=datetime(2024, 1, 2, 9, 32),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            amount=1_000_000.0,
            pre_close=10.0,
        ),
        match_at=datetime(2024, 1, 2, 9, 32),
    )
    audit = CashAuditBundle.from_parts(seed, broker.event_journal)
    replay = audit.replay()
    trades = cash_replay_trade_records(audit)
    daily = pd.DataFrame(
        {
            "cash": [replay.state.cash],
            "market_value": [replay.state.market_value],
            "total_value": [replay.state.nav],
            "daily_return": [replay.state.nav / seed.initial_nav - 1.0],
        },
        index=pd.to_datetime(["20240102"], format="%Y%m%d"),
    )
    daily.index.name = "date"
    return BacktestResult(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240102",
        initial_cash=seed.initial_nav,
        final_value=replay.state.nav,
        total_return=replay.state.nav / seed.initial_nav - 1.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=len(replay.trades),
        win_rate=replay.win_rate,
        sharpe_ratio=None,
        daily_values=daily,
        trades=trades,
        result_contract=_contract(),
        target_execution=TargetExecutionBundle.empty(),
        cash_audit=audit,
    )


@pytest.mark.parametrize(
    "engine_kind,result_factory,result_type",
    (
        (EngineKind.CASH_SINGLE, _single_result, BacktestResult),
        (EngineKind.CASH_PORTFOLIO, _portfolio_result, PortfolioResult),
    ),
)
def test_cash_artifact_round_trip_is_complete_and_verified(
    tmp_path, engine_kind, result_factory, result_type
):
    result = result_factory()
    outcome = RunOutcome.from_result(result, engine_kind=engine_kind)
    source = SourceFingerprint.from_bytes(
        payload=b"date,close\n20240102,10.0\n",
        logical_path="fixture/prices.csv",
        kind="market_data",
    )
    provenance = RunProvenance.build(sources=(source,))

    artifact = ArtifactStore.save(
        outcome,
        tmp_path / "run-001",
        config={"engine": engine_kind.value, "commission_rate": 0.0003},
        provenance=provenance,
        strategy_source="class DemoStrategy: pass\n",
        signals_source="date,signal\n20240102,hold\n",
    )
    loaded = ArtifactStore.load(artifact)

    assert type(loaded.result) is result_type
    assert loaded.outcome.result_contract == result.result_contract
    assert loaded.result.target_execution == result.target_execution
    assert loaded.result.cash_audit == result.cash_audit
    assert loaded.result.trades == result.trades
    pd.testing.assert_frame_equal(loaded.result.daily_values, result.daily_values)
    assert dict(loaded.config) == {
        "commission_rate": 0.0003,
        "engine": engine_kind.value,
    }
    assert loaded.provenance == provenance
    assert loaded.is_rankable
    assert ArtifactStore.verify(artifact) == loaded.manifest
    if result_type is PortfolioResult:
        assert loaded.result.position_history == result.position_history
        assert loaded.result.execution_stats == result.execution_stats

    expected_files = {"manifest.json"}
    for descriptor in loaded.manifest.artifacts.values():
        member = artifact.joinpath(*descriptor.path.split("/"))
        payload = member.read_bytes()
        assert len(payload) == descriptor.byte_length
        assert hashlib.sha256(payload).hexdigest() == descriptor.sha256
        expected_files.add(descriptor.path)
    actual_files = {
        path.relative_to(artifact).as_posix()
        for path in artifact.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files


def test_hash_tamper_is_rejected(tmp_path):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(outcome, tmp_path / "run", config={})
    result_path = artifact / "result.json"
    result_path.write_bytes(result_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="(length|hash) mismatch"):
        ArtifactStore.load(artifact)


def test_manifest_traversal_path_is_rejected_before_member_read(tmp_path):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(outcome, tmp_path / "run", config={})
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["result"]["path"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="dot components"):
        ArtifactStore.load(artifact)


def test_oversized_manifest_is_rejected_from_metadata(tmp_path):
    artifact = tmp_path / "oversized-manifest"
    artifact.mkdir()
    with (artifact / "manifest.json").open("wb") as stream:
        stream.seek(RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES)
        stream.write(b"x")

    with pytest.raises(ValueError, match="manifest.*size limit"):
        ArtifactStore.load(artifact)


def test_manifest_payload_count_bomb_is_rejected_before_payload_read(
    tmp_path, monkeypatch,
):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(outcome, tmp_path / "count-bomb", config={})
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    template = next(iter(manifest["artifacts"].values())).copy()
    template.update(
        byte_length=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    index = 0
    while len(manifest["artifacts"]) <= RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT:
        descriptor = template.copy()
        descriptor["path"] = "bomb/{:05d}.json".format(index)
        manifest["artifacts"]["bomb_{:05d}".format(index)] = descriptor
        index += 1
    manifest_path.write_text(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    assert manifest_path.stat().st_size <= RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES

    payload_reads = []
    real_read = artifact_storage._read_plain_file

    def tracked_read(root, relative_path, **kwargs):
        if relative_path != "manifest.json":
            payload_reads.append(relative_path)
        return real_read(root, relative_path, **kwargs)

    monkeypatch.setattr(artifact_storage, "_read_plain_file", tracked_read)
    with pytest.raises(ValueError, match="artifact count.*v1 limit"):
        ArtifactStore.load(artifact)
    assert payload_reads == []


@pytest.mark.parametrize("limit_kind", ("single", "total"))
def test_manifest_declared_byte_limits_reject_before_payload_read(
    tmp_path, monkeypatch, limit_kind,
):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(
        outcome, tmp_path / "declared-{}".format(limit_kind), config={}
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptors = list(manifest["artifacts"].values())
    if limit_kind == "single":
        descriptors[0]["byte_length"] = RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES + 1
        message = "byte_length.*v1 limit"
    else:
        required = (
            RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES
            // RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES
            + 1
        )
        assert len(descriptors) >= required
        for descriptor in descriptors[:required]:
            descriptor["byte_length"] = RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES
        message = "byte_length total.*v1 limit"
    manifest_path.write_text(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )

    payload_reads = []
    real_read = artifact_storage._read_plain_file

    def tracked_read(root, relative_path, **kwargs):
        if relative_path != "manifest.json":
            payload_reads.append(relative_path)
        return real_read(root, relative_path, **kwargs)

    monkeypatch.setattr(artifact_storage, "_read_plain_file", tracked_read)
    with pytest.raises(ValueError, match=message):
        ArtifactStore.load(artifact)
    assert payload_reads == []


def test_all_member_lengths_are_preflighted_before_first_payload_read(
    tmp_path, monkeypatch,
):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(outcome, tmp_path / "late-mismatch", config={})
    manifest = json.loads(
        (artifact / "manifest.json").read_text(encoding="utf-8")
    )
    last_role = sorted(manifest["artifacts"])[-1]
    last_path = artifact.joinpath(
        *manifest["artifacts"][last_role]["path"].split("/")
    )
    last_path.write_bytes(last_path.read_bytes() + b"x")

    payload_reads = []
    real_read = artifact_storage._read_plain_file

    def tracked_read(root, relative_path, **kwargs):
        if relative_path != "manifest.json":
            payload_reads.append(relative_path)
        return real_read(root, relative_path, **kwargs)

    monkeypatch.setattr(artifact_storage, "_read_plain_file", tracked_read)
    with pytest.raises(ValueError, match="length mismatch"):
        ArtifactStore.load(artifact)
    assert payload_reads == []


def test_semantic_tamper_is_rejected_even_with_rehashed_descriptor(tmp_path):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(outcome, tmp_path / "run", config={})
    trades_path = artifact / "tables" / "trades.json"
    payload = json.loads(trades_path.read_text(encoding="utf-8"))
    payload["records"].append({"tampered": True})
    new_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    trades_path.write_bytes(new_bytes)
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["artifacts"]["trades"]
    descriptor["byte_length"] = len(new_bytes)
    descriptor["sha256"] = hashlib.sha256(new_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cash replay fill view"):
        ArtifactStore.load(artifact)


def test_save_is_atomic_and_never_overwrites(tmp_path, monkeypatch):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    target = tmp_path / "run"

    def fail_manifest(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(ArtifactStore, "_write_manifest", fail_manifest)
    with pytest.raises(OSError, match="simulated"):
        ArtifactStore.save(outcome, target, config={})
    assert not target.exists()
    assert not list(tmp_path.glob(".diepi-artifact-staging-*"))

    monkeypatch.undo()
    ArtifactStore.save(outcome, target, config={})
    with pytest.raises(FileExistsError, match="already exists"):
        ArtifactStore.save(outcome, target, config={})


def test_failed_outcome_round_trip_includes_structured_traceback(tmp_path):
    contract = ResultContract(
        status=ResultStatus.FAILED,
        reason=ResultReason("STRATEGY_EXCEPTION", "Strategy raised"),
    )
    error = RunError(
        code="STRATEGY_EXCEPTION",
        category=ErrorCategory.STRATEGY,
        phase="strategy",
        exception_type="RuntimeError",
        message="boom",
        traceback_role="traceback",
    )
    outcome = RunOutcome(
        engine_kind=EngineKind.CASH_SINGLE,
        result_contract=contract,
        result_role=None,
        error=error,
    )
    artifact = ArtifactStore.save(
        outcome,
        tmp_path / "failed-run",
        config={"symbol": "600000.SH"},
        traceback_text="Traceback... RuntimeError: boom\n",
    )

    loaded = ArtifactStore.load(artifact)
    assert loaded.result is None
    assert loaded.outcome.error == error
    assert loaded.read_bytes("traceback").endswith(b"boom\n")
    assert not loaded.is_rankable


def test_extra_payloads_share_manifest_integrity_and_namespace(tmp_path):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    summary = ArtifactPayload(
        role="compat_summary",
        path="compat/summary.json",
        media_type="application/json",
        data=b'{"status":"SUCCESS"}',
    )
    curve = ArtifactPayload(
        role="compat_equity_curve",
        path="compat/equity_curve.csv",
        media_type="text/csv; charset=utf-8",
        data=b"date,value\n20240102,1000\n",
    )
    artifact = ArtifactStore.save(
        outcome,
        tmp_path / "run-extra",
        config={},
        extra_payloads=(summary, curve),
    )
    loaded = ArtifactStore.load(artifact)
    assert loaded.read_bytes("compat_summary") == summary.data
    assert loaded.read_bytes("compat_equity_curve") == curve.data

    with pytest.raises(ValueError, match="duplicate artifact role"):
        ArtifactStore.save(
            outcome,
            tmp_path / "duplicate-role",
            config={},
            extra_payloads=(
                ArtifactPayload(
                    role="config",
                    path="compat/config-copy.json",
                    media_type="application/json",
                    data=b"{}",
                ),
            ),
        )
    assert not (tmp_path / "duplicate-role").exists()


def test_artifact_config_normalizes_numpy_arrays(tmp_path):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    artifact = ArtifactStore.save(
        outcome,
        tmp_path / "numpy-config",
        config={"weights": np.array([0.25, 0.75])},
    )

    loaded = ArtifactStore.load(artifact)
    assert dict(loaded.config) == {"weights": [0.25, 0.75]}


def test_parallel_artifact_round_trip_preserves_child_contract_evidence(tmp_path):
    child = _traded_single_result()
    result = ParallelResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=1_000.0,
        total_symbols=1,
        success_count=1,
        failed_count=0,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
        top_performers=[{"symbol": "600000.SH", "return": 0.0, "sharpe": None}],
        worst_performers=[{"symbol": "600000.SH", "return": 0.0, "sharpe": None}],
        results={"600000.SH": child},
        ranking_scope=("2024-01-02", "2024-01-02", 1, 1),
        ranking_observation_ids=("2024-01-02",),
        requested_symbols=("600000.SH",),
    )
    outcome = build_parallel_outcome(result)
    artifact = ArtifactStore.save(outcome, tmp_path / "parallel", config={})
    loaded = ArtifactStore.load(artifact)

    assert type(loaded.result) is ParallelResult
    assert loaded.result.is_rankable
    assert loaded.result.requested_symbols == ("600000.SH",)
    restored_child = loaded.result.results["600000.SH"]
    assert restored_child.result_contract.status is child.result_contract.status
    assert (
        restored_child.result_contract.actual_interval
        == child.result_contract.actual_interval
    )
    assert (
        restored_child.result_contract.data_coverage
        == child.result_contract.data_coverage
    )
    assert restored_child.result_contract == child.result_contract
    pd.testing.assert_frame_equal(
        restored_child.daily_values, child.daily_values
    )
    assert restored_child.trades == child.trades
    assert restored_child.target_execution == child.target_execution
    assert restored_child.cash_audit == child.cash_audit
    assert restored_child.total_return == child.total_return
    assert loaded.is_rankable


def test_parallel_and_futures_require_explicit_outcome_builders():
    parallel = ParallelResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=1_000.0,
        total_symbols=0,
        success_count=0,
        failed_count=0,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
    )
    with pytest.raises(ValueError, match="explicit orchestration"):
        RunOutcome.from_result(
            parallel, engine_kind=EngineKind.CASH_PARALLEL
        )
    with pytest.raises(ValueError, match="explicit orchestration"):
        RunOutcome.from_result(
            _futures_result(), engine_kind=EngineKind.INDEX_FUTURES
        )


def test_parallel_partial_artifact_keeps_failed_child_and_is_not_rankable(tmp_path):
    result = ParallelResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=1_000.0,
        total_symbols=2,
        success_count=1,
        failed_count=1,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
        results={"600000.SH": _single_result()},
        errors={"BAD": "RUN_FAILED: synthetic"},
        ranking_error="INCOMPLETE_SYMBOL_COVERAGE: synthetic",
        requested_symbols=("600000.SH", "BAD"),
    )
    outcome = build_parallel_outcome(result)
    artifact = ArtifactStore.save(outcome, tmp_path / "parallel-partial", config={})
    loaded = ArtifactStore.load(artifact)

    assert loaded.outcome.result_contract.status is ResultStatus.PARTIAL
    assert loaded.result.errors == {"BAD": "RUN_FAILED: synthetic"}
    assert not loaded.is_rankable


def test_parallel_child_semantic_tamper_is_rejected_after_rehash(tmp_path):
    child = _traded_single_result()
    result = ParallelResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=50_000.0,
        total_symbols=1,
        success_count=1,
        failed_count=0,
        avg_return=child.total_return,
        avg_annual_return=child.annual_return,
        avg_max_drawdown=child.max_drawdown,
        avg_sharpe=None,
        avg_win_rate=None,
        results={child.symbol: child},
        ranking_scope=("2024-01-02", "2024-01-02", 1, 1),
        ranking_observation_ids=("2024-01-02",),
        requested_symbols=(child.symbol,),
    )
    artifact = ArtifactStore.save(
        build_parallel_outcome(result), tmp_path / "parallel-tamper", config={}
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    role = "parallel_child_0000_trades"
    descriptor = manifest["artifacts"][role]
    trades_path = artifact.joinpath(*descriptor["path"].split("/"))
    payload = json.loads(trades_path.read_text(encoding="utf-8"))
    payload["records"] = []
    tampered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    trades_path.write_bytes(tampered)
    descriptor["byte_length"] = len(tampered)
    descriptor["sha256"] = hashlib.sha256(tampered).hexdigest()
    manifest_path.write_text(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cash replay fill view"):
        ArtifactStore.load(artifact)


def _futures_result(*, embedded_contract=True, with_trade=False):
    nav = 1_010.0 if with_trade else 1_000.0
    trade_rows = (
        [
            {
                "entry_date": "20240102",
                "exit_date": "20240102",
                "pnl_yuan": 10.0,
                "commission_yuan": 0.0,
                "direction": "LONG",
                "contract": "IC9999.CFX",
                "exit_reason": "signal_flat",
            }
        ]
        if with_trade
        else []
    )
    daily = pd.DataFrame(
        {
            "date": ["20240102"],
            "nav": [nav],
            "nav_worst": [nav],
            "position": ["FLAT"],
            "contract": [""],
        }
    )
    result = FuturesResult(
        product="IC",
        start_date="20240102",
        end_date="20240102",
        account_size=1_000.0,
        lots=1,
        multiplier=200,
        final_nav=nav,
        total_return=nav / 1_000.0 - 1.0,
        trade_count=len(trade_rows),
        win_rate=1.0 if with_trade else None,
        avg_trade_pnl=10.0 if with_trade else 0.0,
        daily_nav=daily,
        trades=pd.DataFrame(trade_rows),
        expected_calendar_len=1,
        result_contract=_contract() if embedded_contract else None,
    )
    # The cash helper contract happens to have the same one-day exact scope.
    assert result.validate()
    return result


@pytest.mark.parametrize(
    "embedded_contract,with_trade", ((True, True), (False, False))
)
def test_futures_artifact_round_trip_with_explicit_contract_source(
    tmp_path, embedded_contract, with_trade
):
    result = _futures_result(
        embedded_contract=embedded_contract, with_trade=with_trade
    )
    outcome = build_futures_outcome(result)
    artifact = ArtifactStore.save(
        outcome,
        tmp_path / ("futures-current" if embedded_contract else "futures-legacy"),
        config={},
    )
    loaded = ArtifactStore.load(artifact)

    assert type(loaded.result) is FuturesResult
    assert loaded.result.validation_state is ValidationState.VALID
    pd.testing.assert_frame_equal(loaded.result.daily_nav, result.daily_nav)
    pd.testing.assert_frame_equal(loaded.result.trades, result.trades)
    assert loaded.result.event_journal_json == result.event_journal_json
    assert loaded.outcome.result_contract == outcome.result_contract
    if embedded_contract:
        assert loaded.result.result_contract == outcome.result_contract
    else:
        assert loaded.result.result_contract is None
        assumptions = {
            item.key: item.value
            for item in outcome.result_contract.assumptions
        }
        assert assumptions["orchestration.contract_source"] == (
            "validated_futures_daily_scope_v1"
        )


def test_legacy_loader_is_explicitly_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(ResultStorage, "RESULTS_DIR", str(tmp_path))
    from diepi.backtest.data.report_generator import ReportGenerator

    monkeypatch.setattr(
        ReportGenerator,
        "generate",
        staticmethod(lambda *args, **kwargs: None),
    )
    result = _portfolio_result()
    result.position_history = []
    folder = ResultStorage.save(result, {"frequency": "daily"}, "pass\n")

    loaded = load_legacy_result(folder)

    assert loaded.artifact_verified is False
    assert loaded.is_rankable is False
    assert loaded.result.result_contract == result.result_contract
    assert dict(loaded.config) == {"frequency": "daily"}
    assert loaded.strategy_source == "pass\n"


@pytest.mark.parametrize("run_id", ("CON", "bad."))
def test_nonportable_run_id_is_rejected_without_staging(tmp_path, run_id):
    outcome = RunOutcome.from_result(
        _single_result(), engine_kind=EngineKind.CASH_SINGLE
    )
    with pytest.raises(ValueError, match="run_id"):
        ArtifactStore.save(outcome, tmp_path / run_id, config={})
    assert not list(tmp_path.glob(".diepi-artifact-staging-*"))
