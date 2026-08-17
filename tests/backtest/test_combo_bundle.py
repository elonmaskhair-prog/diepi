from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from diepi import cli
from diepi.artifacts import ArtifactStore
from diepi.backtest.cli import combo_bundle
from diepi.backtest.cli.combo_bundle import (
    COMBO_BUNDLE_SCHEMA,
    COMBO_CSV_MAX_BYTES,
    COMBO_MANIFEST_MAX_BYTES,
    ComboReplayBundle,
    load_combo_bundle,
)
from diepi.backtest.cli.runner import compile_strategy, run_backtest
from diepi.backtest.cli.runner import _replay_input_console_summary
from diepi.demo import generate_synthetic_demo


def _write_bundle(root: Path, dates, *, symbol="000001.SZ") -> Path:
    root.mkdir(parents=True)
    pd.DataFrame(
        [
            {"trade_date": dates[0], "symbol": symbol, "target_weight": 1.0},
        ]
    ).to_csv(root / "targets.csv", index=False)
    pd.DataFrame(
        [
            {
                "trade_date": dates[-1],
                "symbol": symbol,
                "exit_price": "close",
            }
        ]
    ).to_csv(root / "close_sells.csv", index=False)
    pd.DataFrame(
        {
            "date": list(dates),
            "invested_weight": [1.0] + [0.0] * (len(dates) - 1),
            "cash_weight": [0.0] + [1.0] * (len(dates) - 1),
        }
    ).to_csv(root / "daily.csv", index=False)
    return root


def test_combo_bundle_loads_legacy_and_canonical_snapshots(tmp_path):
    dates = ("20240102", "20240103", "20240104")
    canonical = load_combo_bundle(_write_bundle(tmp_path / "canonical", dates))

    assert type(canonical) is ComboReplayBundle
    assert canonical.tag == "canonical"
    assert canonical.targets_for(dates[0]) == {"000001.SZ": 1.0}
    assert canonical.close_sells_for(dates[-1]) == ("000001.SZ",)
    assert canonical.manifest_dict()["schema"] == COMBO_BUNDLE_SCHEMA

    artifact_root = tmp_path / "artifact bundle"
    artifact_root.mkdir()
    for _, path, payload in canonical.artifact_inputs():
        target = artifact_root / Path(path).name
        target.write_bytes(payload)
    restored = load_combo_bundle(artifact_root)
    assert restored.manifest_dict() == canonical.manifest_dict()

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for source, name in (
        (canonical.targets_path, "new_combo_targets_alpha.csv"),
        (canonical.close_sells_path, "new_combo_close_sells_alpha.csv"),
        (canonical.daily_path, "new_combo_daily_alpha.csv"),
    ):
        (legacy / name).write_bytes(source.read_bytes())
    assert load_combo_bundle(legacy, tag="alpha").tag == "alpha"


def test_runner_revalidates_combo_bytes_before_execution(tmp_path):
    dates = ("20240102", "20240103", "20240104")
    canonical = load_combo_bundle(_write_bundle(tmp_path / "combo", dates))
    forged = replace(
        canonical,
        _targets_by_date={dates[0]: (("000001.SZ", 0.8),)},
    )
    template = (
        Path(__file__).parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "combo_replay_template.py"
    )

    with pytest.raises(ValueError, match="does not match"):
        run_backtest(
            strategy_file=str(template),
            start_date=dates[0],
            end_date=dates[-1],
            output_dir=tmp_path / "results",
            run_name="forged-combo",
            data_root=tmp_path,
            pool_symbols=["000001.SZ"],
            strategy_params={"COMBO_BUNDLE": forged},
            verbose=False,
        )


@pytest.mark.parametrize("noncanonical_version", [True, 1.0])
def test_combo_manifest_rejects_noncanonical_numeric_types(
    tmp_path, noncanonical_version
):
    dates = ("20240102", "20240103", "20240104")
    source = load_combo_bundle(_write_bundle(tmp_path / "source", dates))
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    for _, path, payload in source.artifact_inputs():
        (canonical / Path(path).name).write_bytes(payload)

    manifest_path = canonical / "diepi_combo.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = noncanonical_version
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="与实际 CSV 不一致"):
        load_combo_bundle(canonical)


def test_canonical_combo_manifest_must_match_actual_csv_payloads(tmp_path):
    root = _write_bundle(
        tmp_path / "manifested", ("20240102", "20240103", "20240104")
    )
    bundle = load_combo_bundle(root)
    (root / "diepi_combo.json").write_bytes(bundle.manifest_bytes())
    assert load_combo_bundle(root).manifest_dict() == bundle.manifest_dict()

    manifest = json.loads((root / "diepi_combo.json").read_text(encoding="utf-8"))
    manifest["sha256"]["targets"] = "0" * 64
    (root / "diepi_combo.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="与实际 CSV 不一致"):
        load_combo_bundle(root)


def test_canonical_combo_manifest_rejects_duplicate_json_keys(tmp_path):
    root = _write_bundle(
        tmp_path / "duplicates", ("20240102", "20240103")
    )
    (root / "diepi_combo.json").write_text(
        '{"schema":"diepi.combo_replay_bundle",'
        '"schema":"diepi.combo_replay_bundle","schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复 JSON key"):
        load_combo_bundle(root)


@pytest.mark.parametrize(
    "relative,limit",
    [
        ("diepi_combo.json", COMBO_MANIFEST_MAX_BYTES),
        ("daily.csv", COMBO_CSV_MAX_BYTES),
    ],
)
def test_combo_bundle_rejects_sparse_oversize_members(
    tmp_path, relative, limit
):
    root = _write_bundle(
        tmp_path / relative.replace(".", "-"), ("20240102", "20240103")
    )
    target = root / relative
    with target.open("wb") as handle:
        handle.seek(limit)
        handle.write(b"x")

    with pytest.raises(ValueError, match="safety limit"):
        load_combo_bundle(root)


def test_combo_member_enforces_actual_bytes_limit_after_lstat(
    tmp_path, monkeypatch
):
    source = tmp_path / "bounded.csv"
    source.write_bytes(b"12345")
    actual = source.stat()
    original_lstat = combo_bundle.os.lstat

    def understated_lstat(path):
        if Path(path) == source:
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_size=4,
                st_mtime_ns=actual.st_mtime_ns,
                st_file_attributes=getattr(actual, "st_file_attributes", 0),
            )
        return original_lstat(path)

    monkeypatch.setattr(combo_bundle.os, "lstat", understated_lstat)
    with pytest.raises(ValueError, match="safety limit"):
        combo_bundle._read_bounded_bytes(
            source, "test member", max_bytes=4
        )


@pytest.mark.parametrize("prefix", [b"", b"\n"])
def test_combo_csv_rejects_duplicate_headers(prefix):
    payload = prefix + (
        b"trade_date,symbol,target_weight,target_weight\n"
        b"20240102,000001.SZ,0.1,0.9\n"
    )
    with pytest.raises(ValueError, match="header"):
        combo_bundle._read_csv(payload, "combo targets")


@pytest.mark.parametrize(
    "payload,message",
    [
        (
            b"trade_date,symbol,target_weight\n"
            b"20240102,000001.SZ,0.2\x000.9\n",
            "NUL",
        ),
        (
            b"trade_date,symbol,target_weight\n"
            b"JUNK,20240102,000001.SZ,0.5\n",
            "fields",
        ),
    ],
)
def test_combo_csv_rejects_silent_pandas_coercions(payload, message):
    with pytest.raises(ValueError, match=message):
        combo_bundle._read_csv(payload, "combo targets")


@pytest.mark.parametrize("token", ["nan", "NA", "NULL"])
def test_combo_csv_preserves_explicit_na_tokens(token):
    payload = (
        "trade_date,symbol,target_weight\n"
        f"20240102,000001.SZ,{token}\n"
    ).encode()
    frame = combo_bundle._read_csv(payload, "combo targets")
    assert frame.loc[0, "target_weight"] == token


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("target_outside_daily", "不在 daily 覆盖"),
        ("duplicate_target", "同日同标的重复"),
        ("bad_close_price", "只支持 close"),
        ("bad_symbol", "规范的六位证券代码"),
        ("daily_target_mismatch", "与 targets 当日权重和不一致"),
    ],
)
def test_combo_bundle_fails_closed_on_ambiguous_inputs(tmp_path, mutation, message):
    root = _write_bundle(
        tmp_path / mutation, ("20240102", "20240103", "20240104")
    )
    if mutation == "target_outside_daily":
        frame = pd.read_csv(root / "targets.csv", dtype=str)
        frame.loc[0, "trade_date"] = "20240105"
        frame.to_csv(root / "targets.csv", index=False)
    elif mutation == "duplicate_target":
        frame = pd.read_csv(root / "targets.csv", dtype=str)
        pd.concat([frame, frame], ignore_index=True).to_csv(
            root / "targets.csv", index=False
        )
    elif mutation == "bad_close_price":
        frame = pd.read_csv(root / "close_sells.csv", dtype=str)
        frame.loc[0, "exit_price"] = "open"
        frame.to_csv(root / "close_sells.csv", index=False)
    elif mutation == "bad_symbol":
        frame = pd.read_csv(root / "targets.csv", dtype=str)
        frame.loc[0, "symbol"] = "not-a-symbol"
        frame.to_csv(root / "targets.csv", index=False)
    else:
        frame = pd.read_csv(root / "daily.csv", dtype=str)
        frame.loc[0, "invested_weight"] = "0.5"
        frame.loc[0, "cash_weight"] = "0.5"
        frame.to_csv(root / "daily.csv", index=False)

    with pytest.raises(ValueError, match=message):
        load_combo_bundle(root)


def test_combo_template_submits_close_exit_after_open(tmp_path):
    bundle = load_combo_bundle(
        _write_bundle(tmp_path / "combo", ("20240102", "20240103"))
    )
    source = (
        Path(__file__).parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "combo_replay_template.py"
    ).read_text(encoding="utf-8")
    strategy = compile_strategy(source, {"COMBO_BUNDLE": bundle})()

    class Position:
        shares = 100
        available_shares = 100
        market_value = 1_000.0

    class Context:
        current_date = "20240103"

        def __init__(self):
            self.open_sells = []
            self.scheduled = []

        def get_positions(self):
            return {"000001.SZ": Position()}

        def get_position(self, symbol):
            return self.get_positions().get(symbol)

        def get_total_asset(self):
            return 1_000.0

        def sell_at_open(self, symbol, **quantity):
            self.open_sells.append((symbol, quantity))

        def buy_at_open(self, symbol, **quantity):
            raise AssertionError("same-day close exit must not also buy at open")

        def schedule_at_close(self, side, symbol, **quantity):
            self.scheduled.append((side, symbol, quantity))

    context = Context()
    strategy.on_before_market_open(context)
    assert context.open_sells == []
    assert context.scheduled == []
    strategy.on_after_open(context, {})
    assert context.scheduled == [
        ("sell", "000001.SZ", {"percent": 1.0})
    ]


def test_combo_first_day_overlap_preserves_close_priority_and_warns(tmp_path):
    bundle = load_combo_bundle(
        _write_bundle(tmp_path / "combo", ("20240102",))
    )
    source = (
        Path(__file__).parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "combo_replay_template.py"
    ).read_text(encoding="utf-8")
    strategy = compile_strategy(source, {"COMBO_BUNDLE": bundle})()

    class Context:
        current_date = "20240102"

        def __init__(self):
            self.warnings = []
            self.buys = []

        @staticmethod
        def get_positions():
            return {}

        @staticmethod
        def get_position(_symbol):
            return None

        @staticmethod
        def get_total_asset():
            return 1_000.0

        def add_result_warning(self, code, message):
            self.warnings.append((code, message))

        def buy_at_open(self, symbol, **quantity):
            self.buys.append((symbol, quantity))

        @staticmethod
        def sell_at_open(*_args, **_kwargs):
            raise AssertionError("an absent warm-start position cannot be sold")

    context = Context()
    strategy.on_init(context)
    assert strategy.on_before_market_open(context) == ["000001.SZ"]

    assert context.buys == []
    assert context.warnings[0][0] == "COMBO_WARM_START_UNAVAILABLE"
    strategy.on_finish(context)
    assert [code for code, _ in context.warnings] == [
        "COMBO_WARM_START_UNAVAILABLE"
    ]


def test_combo_unconsumed_dates_are_machine_readable(tmp_path):
    bundle = load_combo_bundle(
        _write_bundle(tmp_path / "combo", ("20240102", "20240103"))
    )
    source = (
        Path(__file__).parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "combo_replay_template.py"
    ).read_text(encoding="utf-8")
    strategy = compile_strategy(source, {"COMBO_BUNDLE": bundle})()

    class Context:
        current_date = "20240102"

        def __init__(self):
            self.warnings = []

        @staticmethod
        def get_positions():
            return {}

        @staticmethod
        def get_total_asset():
            return 1_000.0

        @staticmethod
        def buy_at_open(*_args, **_kwargs):
            return None

        @staticmethod
        def sell_at_open(*_args, **_kwargs):
            return None

        def add_result_warning(self, code, message):
            self.warnings.append((code, message))

    context = Context()
    strategy.on_init(context)
    strategy.on_before_market_open(context)
    strategy.on_finish(context)

    assert context.warnings[0][0] == "UNCONSUMED_COMBO_DATES"
    assert "20240103" in context.warnings[0][1]


def test_combo_template_rejects_missing_engine_trade_day(tmp_path):
    bundle = load_combo_bundle(
        _write_bundle(tmp_path / "combo", ("20240102", "20240104"))
    )
    source = (
        Path(__file__).parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "combo_replay_template.py"
    ).read_text(encoding="utf-8")
    strategy = compile_strategy(source, {"COMBO_BUNDLE": bundle})()

    class Context:
        current_date = "20240103"

        @staticmethod
        def get_positions():
            return {}

    with pytest.raises(ValueError, match="missing an engine trade day"):
        strategy.on_before_market_open(Context())


def test_combo_console_summary_is_bounded_and_path_private(tmp_path):
    secret_root = tmp_path / "private client alpha combo"
    bundle = load_combo_bundle(
        _write_bundle(secret_root, ("20240102", "20240103")),
        tag="private-combo",
    )

    summary = _replay_input_console_summary(None, bundle)

    assert summary is not None
    assert "targets=1" in summary
    assert "close_sells=1" in summary
    assert "daily=2" in summary
    assert "dates=20240102..20240103" in summary
    assert "symbols=1" in summary
    assert "private client" not in summary
    assert str(secret_root) not in summary
    assert "known_dates" not in summary


def test_combo_validate_cli_is_read_only_stable_and_path_private(
    tmp_path, capsys
):
    secret_root = _write_bundle(
        tmp_path / "private client combo",
        ("20240102", "20240103"),
    )
    before = {
        path.relative_to(secret_root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in secret_root.rglob("*")
        if path.is_file()
    }

    exit_code = cli.dispatch((
        "combo",
        "validate",
        str(secret_root),
        "--tag",
        "reviewed-v1",
        "--json",
    ))

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    after = {
        path.relative_to(secret_root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in secret_root.rglob("*")
        if path.is_file()
    }
    assert exit_code == cli.EXIT_OK
    assert captured.err == ""
    assert report["status"] == "VALID"
    assert report["bundle"]["tag"] == "reviewed-v1"
    assert report["bundle"]["rows"] == {
        "close_sells": 1,
        "daily": 2,
        "targets": 1,
    }
    assert str(secret_root) not in captured.out
    assert before == after
    assert not (secret_root / "diepi_combo.json").exists()


def test_combo_validate_cli_invalid_bundle_has_stable_private_error(
    tmp_path, capsys
):
    missing = tmp_path / "private customer" / "missing combo"

    exit_code = cli.dispatch((
        "combo", "validate", str(missing), "--json"
    ))

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == cli.EXIT_VALIDATION
    assert captured.err == ""
    assert report["status"] == "INVALID"
    assert report["error"]["type"] == "FileNotFoundError"
    assert "<bundle>" in report["error"]["message"]
    assert str(missing) not in captured.out
    assert not missing.exists()


def test_cli_combo_run_snapshots_all_inputs_without_absolute_paths(
    tmp_path, capsys
):
    demo = generate_synthetic_demo(tmp_path / "demo")
    calendar = pd.read_parquet(
        demo.data_root / "parquet" / "metadata" / "common" / "trade_cal.parquet"
    )
    dates = tuple(
        calendar.loc[calendar["is_open"].astype(int).eq(1), "cal_date"].astype(str)
    )
    bundle_root = _write_bundle(
        tmp_path / "frozen combo", dates, symbol=demo.manifest.symbols[0]
    )
    results = tmp_path / "results"

    exit_code = cli.dispatch(
        (
            "run",
            "--combo-bundle",
            str(bundle_root),
            "--combo-tag",
            "synthetic-combo",
            "--data-root",
            str(demo.data_root),
            "--results-root",
            str(results),
            "--cash",
            "1000000",
            "--stamp-duty",
            "0",
            "--daily-open-cap-yuan",
            "1000000000",
            "--daily-close-cap-yuan",
            "1000000000",
            "--name",
            "combo-run",
            "-q",
        )
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    loaded = ArtifactStore.load(output["artifact_dir"])

    assert loaded.is_rankable is True
    for role in (
        "combo_manifest_source",
        "combo_targets_source",
        "combo_close_sells_source",
        "combo_daily_source",
    ):
        assert loaded.read_bytes(role)
    config = dict(loaded.config)
    assert config["parameters"]["strategy_params"] == {
        "COMBO_BUNDLE": "inputs/combo/diepi_combo.json"
    }
    assert str(bundle_root) not in json.dumps(config, ensure_ascii=False)
    assert {
        source.logical_path for source in loaded.provenance.sources
        if source.kind == "combo_input"
    } == {
        "inputs/combo/diepi_combo.json",
        "inputs/combo/targets.csv",
        "inputs/combo/close_sells.csv",
        "inputs/combo/daily.csv",
    }


def test_cli_combo_overlap_subwindow_is_verified_partial_artifact(
    tmp_path, capsys
):
    demo = generate_synthetic_demo(tmp_path / "demo")
    calendar = pd.read_parquet(
        demo.data_root / "parquet" / "metadata" / "common" / "trade_cal.parquet"
    )
    first_date = str(
        calendar.loc[calendar["is_open"].astype(int).eq(1), "cal_date"].iloc[0]
    )
    bundle_root = _write_bundle(
        tmp_path / "overlap combo",
        (first_date,),
        symbol=demo.manifest.symbols[0],
    )
    results = tmp_path / "results"

    exit_code = cli.dispatch((
        "run",
        "--combo-bundle",
        str(bundle_root),
        "--combo-tag",
        "overlap-v1",
        "--data-root",
        str(demo.data_root),
        "--results-root",
        str(results),
        "--cash",
        "1000000",
        "--daily-open-cap-yuan",
        "1000000000",
        "--daily-close-cap-yuan",
        "1000000000",
        "--name",
        "combo-overlap",
        "-q",
    ))

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    loaded = ArtifactStore.load(output["artifact_dir"])
    contract = loaded.outcome.result_contract
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is False
    assert contract.reason.code == "COMBO_WARM_START_UNAVAILABLE"
    assert "COMBO_WARM_START_UNAVAILABLE" in {
        warning.code for warning in contract.warnings
    }


def test_cli_combo_scope_cannot_be_replaced_with_symbols_argument(
    tmp_path, capsys
):
    bundle_root = _write_bundle(
        tmp_path / "frozen-combo", ("20240102", "20240103")
    )

    exit_code = cli.dispatch(
        (
            "run",
            "--combo-bundle",
            str(bundle_root),
            "--symbols",
            "510300.SH",
            "--data-root",
            str(tmp_path),
        )
    )

    assert exit_code == cli.EXIT_USAGE
    assert "证券范围由冻结输入决定" in capsys.readouterr().err


def test_cli_combo_rejects_template_parameter_overrides(tmp_path, capsys):
    bundle_root = _write_bundle(
        tmp_path / "frozen-combo", ("20240102", "20240103")
    )

    exit_code = cli.dispatch((
        "run",
        "--combo-bundle",
        str(bundle_root),
        "--param",
        "WEIGHT_TOL=0.5",
        "--data-root",
        str(tmp_path),
    ))

    assert exit_code == cli.EXIT_USAGE
    assert "不接受 --param" in capsys.readouterr().err
