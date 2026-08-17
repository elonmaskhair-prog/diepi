#!/usr/bin/env python
"""Public command-line entry point for diePi.

The command surface is deliberately small and lazy: diagnostics and help do
not import the execution engines or the optional desktop GUI.  The historical
``diepi strategy.py ...`` form remains accepted as an alias of ``diepi run``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Optional, Sequence

from . import __brand__, __version__
from .runtime import RuntimePaths


EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
EXIT_INTERRUPTED = 130

_PACKAGE_ROOT = Path(__file__).resolve().parent
_COMMANDS = frozenset(
    {"run", "doctor", "data", "combo", "demo", "examples", "compare", "gui"}
)


def run_backtest(**kwargs):
    """Lazy compatibility proxy used by tests and third-party launchers."""

    from .backtest.cli.runner import run_backtest as implementation

    return implementation(**kwargs)


def _parse_param_value(text: str):
    """Conservatively parse a ``--param`` value.

    Values with leading zeroes (often security identifiers) and non-finite
    spellings remain strings instead of being silently coerced.
    """

    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if re.fullmatch(r"-?(0|[1-9]\d*)", text):
        return int(text)
    if re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?", text) and (
        "." in text or "e" in lowered
    ):
        return float(text)
    return text


def _parse_params(items):
    params = {}
    for item in items or ():
        if "=" not in item:
            raise ValueError(f"--param 必须使用 KEY=VALUE，收到: {item}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key.isidentifier():
            raise ValueError(f"--param 的 KEY 必须是合法变量名，收到: {key}")
        params[key] = _parse_param_value(value.strip())
    return params


def _parse_limit_pct(text):
    if not text:
        return None
    overrides = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"--limit-pct 必须使用 CODE=PCT，收到: {item}")
        code, _, pct = item.partition("=")
        code = code.strip()
        if not code:
            raise ValueError("--limit-pct 的 CODE 不能为空")
        overrides[code] = float(pct)
    return overrides


def _configure_run_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "strategy_file",
        nargs="?",
        help="Python 策略文件；与 --signals/--combo-bundle 三选一",
    )
    parser.add_argument(
        "--signals",
        help=(
            "预计算交易清单 CSV；支持目标权重列 "
            "date,symbol,target_weight 或动作列 date,symbol,action"
        ),
    )
    parser.add_argument(
        "--signals-format",
        default="auto",
        choices=("auto", "target", "action"),
    )
    parser.add_argument(
        "--combo-bundle",
        help=(
            "冻结组合信号目录；包含 targets、close_sells、daily 三份 CSV，"
            "与策略文件/--signals 三选一"
        ),
    )
    parser.add_argument(
        "--combo-tag",
        help="旧式 new_combo_*_<tag>.csv 目录的 tag；规范 bundle 可省略",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="开始日 YYYYMMDD；普通运行默认 20130101，combo 默认其 daily 起点",
    )
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD")
    parser.add_argument("--cash", type=float, default=100_000_000)
    parser.add_argument("--name", help="唯一运行标识；已存在时拒绝覆盖")
    parser.add_argument(
        "--data-root",
        help="本地行情根目录；优先于 DATA_ROOT 环境变量",
    )
    parser.add_argument(
        "--results-root",
        "--output-dir",
        dest="output_dir",
        help="结果根目录；--output-dir 是兼容别名",
    )
    parser.add_argument("--freq", default="daily", choices=("daily", "minute"))
    parser.add_argument(
        "--symbols", help="逗号分隔的指定证券，例如 000001.SZ,510300.SH"
    )
    parser.add_argument("--slippage", type=float, default=0.001)
    parser.add_argument("--commission", type=float, default=0.00025)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument(
        "--open-buy-resize-mode", default="auto", choices=("auto", "legacy")
    )
    parser.add_argument(
        "--stamp-duty",
        default="auto",
        help=(
            "卖出印花税率，默认 auto 按品种和交易日解析；"
            "也可传固定非负数值"
        ),
    )
    parser.add_argument("--transfer-fee-rate", type=float, default=0.0)
    parser.add_argument(
        "--open-buy-fill-mode", default="open+slip", choices=("open+slip", "open")
    )
    parser.add_argument(
        "--open-buy-sizing", default="limit_up", choices=("limit_up", "fill")
    )
    parser.add_argument(
        "--price-mode",
        default="dual",
        choices=("dual", "hfq", "raw"),
        help=(
            "dual=策略看 hfq、撮合用 raw，且两轨必须严格对齐；只有一条价格轨时"
            "请明确选择 hfq 或 raw"
        ),
    )
    parser.add_argument("--t0", help="逗号分隔的 T+0 代码或前缀白名单")
    parser.add_argument("--trading-days", type=int, default=252)
    parser.add_argument("--risk-free-rate", type=float, default=0.03)
    parser.add_argument("--liquidity-cap-ratio", type=float, default=0.8)
    parser.add_argument("--daily-open-cap-yuan", type=float)
    parser.add_argument("--daily-close-cap-yuan", type=float)
    parser.add_argument("--daily-open-previous-day-ratio", type=float)
    parser.add_argument("--daily-close-previous-day-ratio", type=float)
    parser.add_argument(
        "--limit-pct", help="涨跌停覆盖，格式 CODE=PCT[,CODE=PCT]"
    )
    parser.add_argument(
        "--param", action="append", metavar="KEY=VALUE", help="覆盖策略模块变量"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="stdout 仅输出 JSON")
    return parser


def _run_parser(prog: str = "diepi run") -> argparse.ArgumentParser:
    return _configure_run_parser(
        argparse.ArgumentParser(
            prog=prog,
            description="运行本地现金市场回测并保存完整结果证据",
            epilog=(
                "例：diepi run ./ma_cross_strategy.py --symbols "
                "000001.SZ --start 20240101 --end 20241231 "
                "--daily-open-previous-day-ratio 0.1"
            ),
        )
    )


def _infer_signal_symbols(path: Path) -> list[str]:
    """Compatibility helper backed by the same validated one-shot loader."""

    from .backtest.cli.signal_input import load_signal_replay_input

    return list(load_signal_replay_input(path).symbols)


def _resolve_run_inputs(args) -> dict:
    input_modes = sum(
        bool(value)
        for value in (args.strategy_file, args.signals, args.combo_bundle)
    )
    if input_modes != 1:
        raise ValueError(
            "原策略文件与 --signals 二选一；加入 combo 后，必须在策略文件、"
            "--signals 与 --combo-bundle 三者中且只选择一个"
        )
    if args.combo_tag and not args.combo_bundle:
        raise ValueError("--combo-tag 只能与 --combo-bundle 同时使用")

    strategy_file = args.strategy_file
    strategy_params = _parse_params(args.param)
    signals_input = None
    combo_bundle = None
    if args.signals:
        from .backtest.cli.signal_input import load_signal_replay_input

        signals_path = Path(args.signals).expanduser()
        if not signals_path.is_absolute():
            signals_path = Path.cwd() / signals_path
        if strategy_params:
            raise ValueError(
                "--signals 与 --param 冲突：固定重放模板不接受 --param；"
                "请把信号意图写入清单"
            )
        signals_input = load_signal_replay_input(
            signals_path, signal_format=args.signals_format
        )
        strategy_params["SIGNALS_INPUT"] = signals_input
        strategy_file = str(
            _PACKAGE_ROOT / "backtest" / "cli" / "signal_replay_template.py"
        )
    elif args.combo_bundle:
        from .backtest.cli.combo_bundle import load_combo_bundle

        if strategy_params:
            raise ValueError(
                "--combo-bundle 使用固定重放模板，不接受 --param"
            )
        combo_path = Path(args.combo_bundle).expanduser()
        if not combo_path.is_absolute():
            combo_path = Path.cwd() / combo_path
        combo_bundle = load_combo_bundle(combo_path, tag=args.combo_tag)
        strategy_params["COMBO_BUNDLE"] = combo_bundle
        strategy_file = str(
            _PACKAGE_ROOT / "backtest" / "cli" / "combo_replay_template.py"
        )

    start_date = args.start
    end_date = args.end
    if combo_bundle is not None:
        start_date = start_date or combo_bundle.start_date
        end_date = end_date or combo_bundle.end_date
        combo_bundle.validate_requested_scope(start_date, end_date)
    else:
        start_date = start_date or "20130101"

    pool_symbols = None
    if args.symbols:
        if combo_bundle is not None:
            raise ValueError(
                "--combo-bundle 的证券范围由冻结输入决定，不能同时使用 --symbols"
            )
        from .backtest.data.cache_manager import normalize_data_symbol

        pool_symbols = sorted({
            normalize_data_symbol(item.strip())
            for item in args.symbols.split(",")
            if item.strip()
        })
        if not pool_symbols:
            raise ValueError("--symbols 不能为空")
        if signals_input is not None and tuple(pool_symbols) != signals_input.symbols:
            requested = set(pool_symbols)
            frozen = set(signals_input.symbols)
            raise ValueError(
                "--symbols 必须与冻结信号 scope 精确一致；"
                f"缺少={sorted(frozen - requested)}，"
                f"多余={sorted(requested - frozen)}"
            )
    elif signals_input is not None:
        pool_symbols = list(signals_input.symbols)
    elif combo_bundle is not None:
        pool_symbols = list(combo_bundle.symbols)

    stamp_text = str(args.stamp_duty).strip()
    stamp_duty = "auto" if stamp_text.lower() == "auto" else float(stamp_text)
    t0_overrides = (
        {item.strip() for item in args.t0.split(",") if item.strip()}
        if args.t0
        else None
    )

    from .backtest.liquidity import build_daily_auction_liquidity_policy

    build_daily_auction_liquidity_policy(
        open_fixed_yuan=args.daily_open_cap_yuan,
        close_fixed_yuan=args.daily_close_cap_yuan,
        open_previous_day_ratio=args.daily_open_previous_day_ratio,
        close_previous_day_ratio=args.daily_close_previous_day_ratio,
    )
    paths = RuntimePaths.resolve(
        data_root=args.data_root,
        results_root=args.output_dir,
        require_data_root=True,
    )
    return {
        "strategy_file": strategy_file,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": args.cash,
        "data_root": paths.data_root,
        "output_dir": paths.results_root,
        "run_name": args.name,
        "freq": args.freq,
        "slippage": args.slippage,
        "commission": args.commission,
        "open_buy_resize_mode": args.open_buy_resize_mode,
        "pool_symbols": pool_symbols,
        "stamp_duty": stamp_duty,
        "transfer_fee_rate": args.transfer_fee_rate,
        "min_commission": args.min_commission,
        "lot_size": args.lot_size,
        "liquidity_cap_ratio": args.liquidity_cap_ratio,
        "daily_open_cap_yuan": args.daily_open_cap_yuan,
        "daily_close_cap_yuan": args.daily_close_cap_yuan,
        "daily_open_previous_day_ratio": args.daily_open_previous_day_ratio,
        "daily_close_previous_day_ratio": args.daily_close_previous_day_ratio,
        "limit_pct_overrides": _parse_limit_pct(args.limit_pct),
        "strategy_params": strategy_params,
        "open_buy_fill_mode": args.open_buy_fill_mode,
        "open_buy_sizing": args.open_buy_sizing,
        "t0_overrides": t0_overrides,
        "trading_days_per_year": args.trading_days,
        "risk_free_rate": args.risk_free_rate,
        "price_mode": args.price_mode,
        "verbose": not args.quiet,
    }


def _execute_run(argv: Sequence[str], *, prog: str = "diepi run") -> int:
    parser = _run_parser(prog)
    args = parser.parse_args(list(argv))
    try:
        kwargs = _resolve_run_inputs(args)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        output = run_backtest(**kwargs)
        if args.quiet:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
        if not output.get("rankable", False):
            contract = output.get("result_contract") or {}
            reason = contract.get("reason") or {}
            print(
                "结果不可排名: "
                f"{contract.get('status', 'UNCLASSIFIED')}: "
                f"{reason.get('message', '请检查结果契约和诊断产物')}",
                file=sys.stderr,
            )
            return EXIT_VALIDATION
        return EXIT_OK
    except KeyboardInterrupt:
        print("运行已中断", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except Exception as exc:  # engine preserves a structured diagnostic artifact
        print(f"内部错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


def _execute_gui(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="diepi gui", description="启动正式支持的 PySide6 桌面界面"
    )
    parser.add_argument(
        "--data-root",
        help="当前 GUI 运行及历史 K 线重验使用的本地数据根",
    )
    parser.add_argument(
        "--results-root",
        help="CLI/GUI 共享的 RunArtifact 结果根目录",
    )
    args = parser.parse_args(list(argv))
    try:
        from .backtest.ui.main_window import run_app
    except ImportError as exc:
        print(
            "GUI 依赖不可用；请先安装: python -m pip install 'diepi[gui]'\n"
            f"详细信息: {exc}",
            file=sys.stderr,
        )
        return EXIT_VALIDATION
    return int(run_app(
        data_root=args.data_root,
        results_root=args.results_root,
    ) or EXIT_OK)


def _print_root_help() -> None:
    print(
        f"""{__brand__} ({__version__}) — 本地事件驱动研究工具

用法:
  diepi doctor [选项]                 检查安装、路径和 GUI 依赖
  diepi data validate [选项]          只读校验指定本地数据范围
  diepi data extract [选项]           从用户自有数据生成私有范围切片
  diepi combo validate <目录>         只读校验冻结 combo bundle
  diepi demo [工作区]                 生成、校验并运行合成演示
  diepi examples list                 列出 wheel 内置策略示例
  diepi examples copy ma-cross [路径]  复制可编辑的 MA5/MA20 示例
  diepi run <策略.py> [选项]          运行回测
  diepi compare runs <基线> <候选>    比较两个回测工件的经济账本
  diepi gui                            启动 PySide6 桌面界面
  diepi <策略.py> [选项]              兼容的 run 简写

先运行 `diepi doctor`。diePi 不下载行情；正式研究数据由用户自行合法准备。
每个命令可用 `--help` 查看参数。
"""
    )


def dispatch(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        _print_root_help()
        return EXIT_OK
    if args[0] in ("-V", "--version"):
        if len(args) != 1:
            print("--version 不接受其他参数", file=sys.stderr)
            return EXIT_USAGE
        print(f"diepi {__version__}")
        return EXIT_OK

    command, rest = args[0], args[1:]
    if command == "run":
        return _execute_run(rest)
    if command == "doctor":
        from .commands.doctor import main as doctor_main

        return doctor_main(rest)
    if command == "data":
        if not rest or rest[0] not in {"validate", "extract"}:
            print(
                "用法: diepi data {validate|extract} --help", file=sys.stderr
            )
            return EXIT_USAGE
        if rest[0] == "validate":
            from .commands.data_validate import main as validate_main

            return validate_main(rest[1:])
        from .commands.data_extract import main as extract_main

        return extract_main(rest[1:])
    if command == "combo":
        if not rest or rest[0] != "validate":
            print("用法: diepi combo validate --help", file=sys.stderr)
            return EXIT_USAGE
        from .commands.combo_validate import main as combo_validate_main

        return combo_validate_main(rest[1:])
    if command == "demo":
        from .commands.demo import main as demo_main

        return demo_main(rest)
    if command == "examples":
        from .commands.examples import main as examples_main

        return examples_main(rest)
    if command == "compare":
        if not rest or rest[0] != "runs":
            print("用法: diepi compare runs --help", file=sys.stderr)
            return EXIT_USAGE
        from .commands.run_compare import main as compare_main

        return compare_main(rest[1:])
    if command == "gui":
        return _execute_gui(rest)

    # Compatibility with the original command: the first token is a strategy
    # path (or a run option), not an unrecognized subcommand.
    if command not in _COMMANDS:
        return _execute_run(args, prog="diepi")
    return EXIT_USAGE


def main(argv: Optional[Sequence[str]] = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        code = dispatch(argv)
    except KeyboardInterrupt:
        print("操作已中断", file=sys.stderr)
        code = EXIT_INTERRUPTED
    except SystemExit:
        # argparse owns usage errors and their conventional status 2.
        raise
    except Exception as exc:
        print(f"内部错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = EXIT_INTERNAL
    raise SystemExit(code)


if __name__ == "__main__":
    main()


__all__ = [
    "EXIT_INTERNAL",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
    "dispatch",
    "main",
    "run_backtest",
]
