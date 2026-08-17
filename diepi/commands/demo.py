"""CLI adapter for creating the deterministic synthetic demo workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from ..demo import generate_synthetic_demo


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "workspace",
        nargs="?",
        default="diepi_demo",
        help="new workspace path (default: ./diepi_demo)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="create and validate the workspace without running its strategy",
    )
    return parser


def execute(args, *, stdout=None, stderr=None) -> int:
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    try:
        demo = generate_synthetic_demo(args.workspace)
    except (FileExistsError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        error.write(f"diePi demo: {type(exc).__name__}: {exc}\n")
        return 2
    run_output = None
    run_dir = demo.workspace / "results" / "synthetic_demo"
    if not getattr(args, "generate_only", False):
        try:
            from ..backtest.cli.runner import run_backtest

            run_output = run_backtest(
                strategy_file=str(demo.strategy_file),
                start_date=demo.manifest.start_date,
                end_date=demo.manifest.end_date,
                initial_cash=1_000_000.0,
                data_root=demo.data_root,
                output_dir=demo.workspace / "results",
                run_name="synthetic_demo",
                freq="daily",
                pool_symbols=(demo.manifest.symbols[0],),
                daily_open_previous_day_ratio=0.1,
                verbose=False,
            )
        except KeyboardInterrupt:
            error.write("diePi demo: interrupted\n")
            return 130
        except Exception as exc:
            error.write(f"diePi demo run: {type(exc).__name__}: {exc}\n")
            return 3

    payload = demo.to_dict()
    payload["run"] = (
        None
        if run_output is None
        else {
            "artifact_dir": run_output.get(
                "artifact_dir", str(run_dir)
            ),
            "artifact_verified": bool(
                run_output.get("artifact_verified", False)
            ),
            "rankable": bool(run_output.get("rankable", False)),
            "result_contract": run_output.get("result_contract"),
            "result_directory": str(run_dir),
        }
    )
    if getattr(args, "json", False):
        output.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
    else:
        output.write("diePi synthetic demo: READY\n")
        output.write("SYNTHETIC DATA ONLY - no generated row is market data.\n")
        output.write(f"workspace: {demo.workspace}\n")
        output.write(f"data root: {demo.data_root}\n")
        output.write(f"strategy: {demo.strategy_file}\n")
        output.write(
            "validation: PASS "
            f"({demo.validation_report.report_sha256})\n"
        )
        if run_output is not None:
            contract = run_output.get("result_contract") or {}
            output.write(
                "backtest: "
                f"{contract.get('status', 'UNCLASSIFIED')}; "
                f"rankable={bool(run_output.get('rankable', False))}\n"
            )
            output.write(f"result: {run_dir}\n")
    if run_output is not None and not run_output.get("rankable", False):
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi demo",
            description="Create a validated, deterministic synthetic workspace",
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["configure_parser", "execute", "main"]
