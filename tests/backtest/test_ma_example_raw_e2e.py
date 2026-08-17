"""The installable MA example runs on the documented raw-minimal layout."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import run_backtest
from diepi.examples import get_example


SYMBOL = "588000.SH"
OPEN_DATES = (
    "20240506", "20240507", "20240508", "20240509", "20240510",
    "20240513", "20240514", "20240515", "20240516", "20240517",
    "20240520", "20240521", "20240522", "20240523", "20240524",
    "20240527", "20240528", "20240529", "20240530", "20240531",
    "20240603", "20240604", "20240605", "20240606", "20240607",
    "20240611", "20240612", "20240613", "20240614", "20240617",
    "20240618", "20240619", "20240620", "20240621", "20240624",
    "20240625", "20240626", "20240627", "20240628",
)


def _write_raw_minimal(root: Path) -> None:
    prices = [10.0] * 20 + [11.0] * 5 + [9.0] * 14
    rows = []
    for index, (date, price) in enumerate(zip(OPEN_DATES, prices)):
        rows.append(
            {
                "ts_code": SYMBOL,
                "trade_date": date,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "pre_close": prices[index - 1] if index else price,
                "amount": 1_000_000.0,
            }
        )
    target = root / f"parquet/timeseries/etf_daily_raw/{SYMBOL}.parquet"
    target.parent.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(target, index=False)


def test_ma_example_raw_minimal_uses_bundled_calendar_and_publishes_artifact(
    tmp_path,
):
    data_root = tmp_path / "data"
    _write_raw_minimal(data_root)
    assert not (data_root / "parquet/metadata/common/trade_cal.parquet").exists()

    output = run_backtest(
        strategy_file=str(get_example("ma-cross").source_path),
        start_date=OPEN_DATES[0],
        end_date=OPEN_DATES[-1],
        initial_cash=1_000_000.0,
        data_root=data_root,
        output_dir=tmp_path / "results",
        run_name="ma-raw-minimal",
        pool_symbols=[SYMBOL],
        price_mode="raw",
        stamp_duty=0.0,
        commission=0.0,
        min_commission=0.0,
        slippage=0.0,
        daily_open_cap_yuan=1_000_000_000.0,
        open_buy_fill_mode="open",
        open_buy_sizing="fill",
        verbose=False,
    )

    loaded = ArtifactStore.load(output["artifact_dir"])
    assert output["artifact_verified"] is True
    assert output["rankable"] is True
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assumptions = {
        item.key: item.value
        for item in loaded.outcome.result_contract.assumptions
    }
    assert assumptions["calendar.source"] == "bundled"
    assert assumptions["calendar.id"] == "cn-a-share-2010-2026-v1"
    assert assumptions["calendar.content_sha256"] == (
        "882bb722fd15cbe4ceddb2370422c30fead8c6e81f842300221996ea3cb44a59"
    )
    assert assumptions["execution.strategy_price_mode"] == "raw"
    assert assumptions["execution.execution_price_mode"] == "raw"
    assert assumptions["corporate_action.adjustment_factor_model"] == (
        "disabled_same_price_space"
    )
    assert [trade["direction"] for trade in loaded.result.trades] == ["BUY", "SELL"]
    assert "UP_CROSS" in loaded.result.trades[0]["note"]
    assert "DOWN_CROSS" in loaded.result.trades[1]["note"]
