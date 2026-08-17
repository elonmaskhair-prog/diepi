"""Frozen signal-input boundary tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pickle
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

from diepi import cli
from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import compile_strategy, run_backtest
from diepi.backtest.cli.signal_input import (
    SIGNAL_MAX_SAFE_SHARES,
    SignalInstruction,
    SignalReplayInput,
    load_signal_replay_input,
)
from diepi.backtest.cli.runner import _replay_input_console_summary
from diepi.demo import generate_synthetic_demo


def test_target_adapter_freezes_bytes_scope_and_manifest() -> None:
    payload = (
        b"date,symbol,target_weight\n"
        b"2024-01-02,000001,0.2\n"
        b"20240102,600000.SH,0.6\n"
        b"20240102,000001,0.4\n"
    )

    frozen = SignalReplayInput.from_bytes(
        payload, source_name="research-signals.csv"
    )

    assert frozen.source_bytes is payload
    assert frozen.signal_format == "target"
    assert frozen.symbols == ("000001", "600000.SH")
    assert frozen.dates == ("20240102",)
    assert [row.target_weight for row in frozen.rows_for("2024-01-02")] == [
        0.6,
        0.4,
    ]
    assert frozen.warnings and "末行" in frozen.warnings[0]
    assert frozen.artifact_inputs() == (
        ("signals_source", "inputs/signals.csv", payload),
    )
    manifest = frozen.manifest_dict()
    assert manifest == {
        "adapter": "csv_target_action_v1",
        "format": "target",
        "rows": 2,
        "schema": "diepi.signal_replay_input",
        "schema_version": 1,
        "scope": {
            "dates": ["20240102"],
            "symbols": ["000001", "600000.SH"],
        },
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_name": "research-signals.csv",
    }
    json.dumps(manifest)
    assert pickle.loads(pickle.dumps(frozen)) == frozen


def test_action_adapter_validates_quantities_before_execution() -> None:
    frozen = SignalReplayInput.from_bytes(
        (
            "date,symbol,action,percent,shares,amount\n"
            "20240102,A,buy,0.25,,\n"
            f"20240102,B,buy,,{SIGNAL_MAX_SAFE_SHARES},\n"
            "20240102,C,buy,,,1000.5\n"
            "20240103,A,sell,,,\n"
        ).encode(),
        signal_format="action",
    )

    rows = frozen.rows_for("20240102")
    assert rows[0].percent == 0.25
    assert rows[1].shares == SIGNAL_MAX_SAFE_SHARES
    assert rows[2].amount == 1000.5
    assert frozen.rows_for("20240103")[0].action == "sell"

    invalid = (
        ("percent", "date,symbol,action,percent\n20240102,A,buy,1.01\n"),
        ("shares", "date,symbol,action,shares\n20240102,A,buy,1.5\n"),
        (
            "shares",
            "date,symbol,action,shares\n"
            f"20240102,A,buy,{SIGNAL_MAX_SAFE_SHARES + 1}\n",
        ),
        (
            "shares",
            "date,symbol,action,shares\n"
            "20240102,A,buy,1e1000000000\n",
        ),
        ("quantity", "date,symbol,action\n20240102,A,buy\n"),
        (
            "at most one",
            "date,symbol,action,percent,shares\n20240102,A,buy,0.5,100\n",
        ),
        ("amount", "date,symbol,action,amount\n20240102,A,sell,100\n"),
    )
    for expected, text in invalid:
        with pytest.raises(ValueError, match=expected):
            SignalReplayInput.from_bytes(text.encode(), signal_format="action")


def test_instruction_constructor_cannot_bypass_quantity_contract() -> None:
    with pytest.raises(ValueError, match="percent"):
        SignalInstruction(
            date="20240102", symbol="A", action="buy", percent=1.1
        )
    with pytest.raises(ValueError, match="shares"):
        SignalInstruction(
            date="20240102",
            symbol="A",
            action="buy",
            shares=SIGNAL_MAX_SAFE_SHARES + 1,
        )
    with pytest.raises(ValueError, match="amount"):
        SignalInstruction(
            date="20240102", symbol="A", action="buy", amount=float("inf")
        )


def test_csv_adapter_rejects_mixed_target_and_action_semantics() -> None:
    payload = (
        b"date,symbol,target_weight,action,shares\n"
        b"20240102,000001.SZ,0,buy,100\n"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        SignalReplayInput.from_bytes(payload)
    with pytest.raises(ValueError, match="conflicting semantic columns"):
        SignalReplayInput.from_bytes(payload, signal_format="target")
    with pytest.raises(ValueError, match="conflicting semantic columns"):
        SignalReplayInput.from_bytes(payload, signal_format="action")


@pytest.mark.parametrize("prefix", [b"", b"\n"])
def test_csv_adapter_rejects_duplicate_headers(prefix: bytes) -> None:
    payload = prefix + (
        b"date,symbol,target_weight,target_weight\n"
        b"20240102,000001.SZ,0.1,0.9\n"
    )
    with pytest.raises(ValueError, match="header"):
        SignalReplayInput.from_bytes(payload)


@pytest.mark.parametrize(
    "payload,message",
    [
        (
            b"date,symbol,target_weight\n"
            b"20240102,000001.SZ,0.2\x000.9\n",
            "NUL",
        ),
        (
            b"date,symbol,target_weight\n"
            b"JUNK,20240102,000001.SZ,0.5\n",
            "fields",
        ),
    ],
)
def test_csv_adapter_rejects_silent_pandas_coercions(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        SignalReplayInput.from_bytes(payload)


@pytest.mark.parametrize("token", ["nan", "NA", "NULL"])
@pytest.mark.parametrize("quantity", ["percent", "shares", "amount"])
def test_action_adapter_does_not_treat_explicit_na_tokens_as_empty(
    token: str, quantity: str,
) -> None:
    payload = (
        f"date,symbol,action,{quantity}\n"
        f"20240102,A,sell,{token}\n"
    ).encode()
    with pytest.raises(ValueError):
        SignalReplayInput.from_bytes(payload, signal_format="action")


def test_loader_reads_exact_regular_file_once(tmp_path: Path) -> None:
    path = tmp_path / "signals.csv"
    payload = b"date,symbol,target_weight\n20240102,000001.SZ,0.5\n"
    path.write_bytes(payload)

    frozen = load_signal_replay_input(path)

    assert frozen.source_bytes == payload
    assert frozen.source_name == "signals.csv"


def test_loader_fails_closed_if_handle_generation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from diepi.backtest.cli import signal_input

    path = tmp_path / "signals.csv"
    path.write_text(
        "date,symbol,target_weight\n20240102,A,0.5\n", encoding="utf-8"
    )
    real_fstat = os.fstat
    calls = 0

    def unstable_fstat(fd):
        nonlocal calls
        info = real_fstat(fd)
        calls += 1
        values = {
            "st_mode": info.st_mode,
            "st_dev": info.st_dev,
            "st_ino": info.st_ino,
            "st_size": info.st_size,
            "st_mtime_ns": info.st_mtime_ns + (1 if calls == 2 else 0),
            "st_file_attributes": getattr(info, "st_file_attributes", 0),
        }
        return SimpleNamespace(**values)

    monkeypatch.setattr(signal_input.os, "fstat", unstable_fstat)
    with pytest.raises(OSError, match="读取期间"):
        load_signal_replay_input(path)


def test_loader_rejects_link_or_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "signals.csv"
    path.write_text(
        "date,symbol,target_weight\n20240102,A,0.5\n", encoding="utf-8"
    )
    real_lstat = Path.lstat

    def linked_lstat(candidate):
        info = real_lstat(candidate)
        if candidate == path:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o644,
                st_file_attributes=0,
            )
        return info

    monkeypatch.setattr(Path, "lstat", linked_lstat)
    with pytest.raises(ValueError, match="链接"):
        load_signal_replay_input(path)


def test_cli_loads_once_and_requires_exact_signal_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from diepi.backtest.cli import signal_input

    path = tmp_path / "signals.csv"
    path.write_text(
        "date,symbol,target_weight\n"
        "20240102,000001.SZ,0.5\n"
        "20240102,600000.SH,0.5\n",
        encoding="utf-8",
    )
    data_root = tmp_path / "data"
    data_root.mkdir()
    calls = 0
    real_load = signal_input.load_signal_replay_input

    def counted_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(signal_input, "load_signal_replay_input", counted_load)
    parser = cli._run_parser()
    args = parser.parse_args([
        "--signals",
        str(path),
        "--symbols",
        "600000.SH,000001.SZ",
        "--data-root",
        str(data_root),
        "--output-dir",
        str(tmp_path / "results"),
    ])

    kwargs = cli._resolve_run_inputs(args)

    assert calls == 1
    frozen = kwargs["strategy_params"]["SIGNALS_INPUT"]
    assert type(frozen) is SignalReplayInput
    assert kwargs["pool_symbols"] == ["000001.SZ", "600000.SH"]

    mismatch = parser.parse_args([
        "--signals",
        str(path),
        "--symbols",
        "000001.SZ",
    ])
    with pytest.raises(ValueError, match="精确一致"):
        cli._resolve_run_inputs(mismatch)
    assert calls == 2

    parameterized = parser.parse_args([
        "--signals",
        str(path),
        "--param",
        "EXTRA=1",
    ])
    with pytest.raises(ValueError, match="不接受 --param"):
        cli._resolve_run_inputs(parameterized)


def test_template_executes_only_frozen_instructions_after_path_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signals.csv"
    original = b"date,symbol,target_weight\n20240102,A,0.75\n"
    path.write_bytes(original)
    frozen = load_signal_replay_input(path)
    path.write_bytes(b"date,symbol,target_weight\n20240102,B,0.0\n")
    template = (
        Path(__file__).resolve().parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "signal_replay_template.py"
    ).read_text(encoding="utf-8")
    strategy = compile_strategy(template, {"SIGNALS_INPUT": frozen})()

    class Context:
        current_date = "20240102"

        def __init__(self):
            self.targets = None

        def rebalance(self, targets):
            self.targets = targets

        @staticmethod
        def get_positions():
            return {}

    context = Context()
    strategy.on_init(context)
    requested = strategy.on_before_market_open(context)

    assert context.targets == {"A": 0.75}
    assert requested == ["A"]
    assert frozen.source_bytes == original


def test_runner_artifact_and_execution_use_same_frozen_bytes(
    tmp_path: Path,
    capsys,
) -> None:
    demo = generate_synthetic_demo(tmp_path / "demo")
    symbol = demo.manifest.symbols[0]
    original = (
        "date,symbol,target_weight\n"
        f"{demo.manifest.start_date},{symbol},0.8\n"
    ).encode()
    # A valid source basename may collide with a canonical Artifact directory;
    # the compatibility copy must be renamed without changing primary bytes.
    path = tmp_path / "INPUTS"
    path.write_bytes(original)
    frozen = load_signal_replay_input(path)
    path.write_bytes(
        (
            "date,symbol,target_weight\n"
            f"{demo.manifest.start_date},{symbol},0.0\n"
        ).encode()
    )
    template = (
        Path(__file__).resolve().parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "signal_replay_template.py"
    )

    output = run_backtest(
        strategy_file=str(template),
        start_date=demo.manifest.start_date,
        end_date=demo.manifest.end_date,
        initial_cash=1_000_000.0,
        data_root=demo.data_root,
        output_dir=tmp_path / "results",
        run_name="frozen-signal-run",
        pool_symbols=[symbol],
        strategy_params={"SIGNALS_INPUT": frozen},
        daily_open_cap_yuan=1_000_000_000.0,
        verbose=True,
    )

    console = capsys.readouterr()
    assert "输入摘要: signals format=target rows=1" in console.out
    assert "SignalInstruction" not in console.out
    assert "source_bytes" not in console.out
    assert str(path.resolve()) not in console.out

    loaded = ArtifactStore.load(output["artifact_dir"])
    params = loaded.config["parameters"]["strategy_params"]
    assert loaded.result.trade_count >= 1
    assert loaded.read_bytes("signals_source") == original
    assert params["SIGNALS_INPUT"] == "inputs/signals.csv"
    assert params["SIGNALS_FORMAT"] == "target"
    assert params["SIGNALS_MANIFEST"] == frozen.manifest_dict()
    assert params["SIGNALS_MANIFEST"]["scope"]["symbols"] == [symbol]
    assert params["SIGNALS_MANIFEST"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert (Path(output["artifact_dir"]) / "signals_INPUTS").read_bytes() == original


def test_signal_console_summary_is_bounded_and_path_private() -> None:
    frozen = SignalReplayInput.from_bytes(
        b"date,symbol,target_weight\n"
        b"20240102,000001.SZ,0.5\n"
        b"20240103,600000.SH,0.5\n",
        source_name="private-research-signals.csv",
    )

    summary = _replay_input_console_summary(frozen, None)

    assert summary is not None
    assert "format=target" in summary
    assert "rows=2" in summary
    assert "dates=2(20240102..20240103)" in summary
    assert "symbols=2" in summary
    assert "private-research" not in summary
    assert "000001.SZ" not in summary
    assert "SignalInstruction" not in summary


def test_runner_rejects_path_injection_and_mixed_frozen_inputs(tmp_path: Path) -> None:
    strategy = tmp_path / "strategy.py"
    strategy.write_text("def on_init(ctx):\n    pass\n", encoding="utf-8")
    frozen = SignalReplayInput.from_bytes(
        b"date,symbol,target_weight\n20240102,A,0.5\n"
    )
    common = {
        "strategy_file": str(strategy),
        "output_dir": tmp_path / "results",
        "data_root": tmp_path,
        "verbose": False,
    }
    with pytest.raises(ValueError, match="路径注入"):
        run_backtest(**common, strategy_params={"SIGNALS_FILE": "signals.csv"})
    with pytest.raises(ValueError, match="路径注入"):
        run_backtest(**common, strategy_params={"SIGNALS_FILE": ""})
    with pytest.raises(TypeError, match="SIGNALS_INPUT"):
        run_backtest(**common, strategy_params={"SIGNALS_INPUT": None})
    with pytest.raises(TypeError, match="COMBO_BUNDLE"):
        run_backtest(**common, strategy_params={"COMBO_BUNDLE": None})
    with pytest.raises(ValueError, match="不能同时"):
        run_backtest(
            **common,
            strategy_params={"SIGNALS_INPUT": frozen, "COMBO_BUNDLE": object()},
        )


def test_runner_revalidates_signal_bytes_template_and_scope(tmp_path: Path) -> None:
    canonical = SignalReplayInput.from_bytes(
        b"date,symbol,target_weight\n20240102,A,0\n"
    )
    forged = replace(
        canonical,
        instructions=(
            SignalInstruction(
                date="20240102", symbol="A", target_weight=0.8
            ),
        ),
    )
    template = (
        Path(__file__).resolve().parents[2]
        / "diepi"
        / "backtest"
        / "cli"
        / "signal_replay_template.py"
    )
    common = {
        "strategy_file": str(template),
        "start_date": "20240102",
        "end_date": "20240102",
        "output_dir": tmp_path / "results",
        "data_root": tmp_path,
        "verbose": False,
    }
    with pytest.raises(ValueError, match="do not match"):
        run_backtest(
            **common,
            run_name="forged-signals",
            pool_symbols=["A"],
            strategy_params={"SIGNALS_INPUT": forged},
        )

    custom = tmp_path / "custom.py"
    custom.write_text("def on_init(ctx):\n    pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="signal replay template"):
        run_backtest(
            **{**common, "strategy_file": str(custom)},
            run_name="custom-signals",
            pool_symbols=["A"],
            strategy_params={"SIGNALS_INPUT": canonical},
        )
    with pytest.raises(ValueError, match="scope"):
        run_backtest(
            **common,
            run_name="wrong-scope",
            pool_symbols=["B"],
            strategy_params={"SIGNALS_INPUT": canonical},
        )
    with pytest.raises(ValueError, match="exactly SIGNALS_INPUT"):
        run_backtest(
            **common,
            run_name="extra-signal-param",
            pool_symbols=["A"],
            strategy_params={"SIGNALS_INPUT": canonical, "EXTRA": 1},
        )
