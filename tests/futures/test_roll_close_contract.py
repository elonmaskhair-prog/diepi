"""Pure acceptance tests for close-only futures roll semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.result_contract import ResultContract
from diepi.futures.engine import FuturesEngine


PRODUCT = "IC"
CONTRACT_A = "ICA.CFX"
CONTRACT_B = "ICB.CFX"
D0 = "20240101"
D1 = "20240102"
D2 = "20240103"
D3 = "20240104"


def _bar(
    open_: float,
    close: float,
    *,
    low: float | None = None,
    high: float | None = None,
    vol: float = 100.0,
) -> dict[str, float]:
    return {
        "open": float(open_),
        "high": float(close if high is None else high),
        "low": float(close if low is None else low),
        "close": float(close),
        "vol": float(vol),
    }


def _write_contracts(
    root: Path,
    bars: dict[str, dict[str, dict[str, float]]],
) -> Path:
    rows: list[dict[str, object]] = []
    for contract, dated_bars in bars.items():
        for trade_date, bar in dated_bars.items():
            rows.append({
                "ts_code": contract,
                "trade_date": trade_date,
                **bar,
            })
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(
        root / f"{PRODUCT}_contracts.parquet", index=False
    )
    return root


def _engine(
    tmp_path: Path,
    bars: dict[str, dict[str, dict[str, float]]],
    *,
    expiry: dict[str, str],
    roll_schedule: dict[str, str] | None = None,
    account: float = 200_000.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    method: str = "volume_t1",
    start: str = D1,
    end: str = D3,
    min_days: int = 1,
) -> FuturesEngine:
    data_dir = _write_contracts(tmp_path / "contracts", bars)
    trading_calendar = sorted({
        trade_date
        for dated_bars in bars.values()
        for trade_date in dated_bars
    })
    return FuturesEngine(
        product=PRODUCT,
        start_date=start,
        end_date=end,
        account_size=account,
        lots=1,
        commission_rate=commission,
        slippage_points=slippage,
        contract_data_dir=str(data_dir),
        continuous_data_dir=str(tmp_path / "no_continuous_mapping"),
        contract_method=method,
        min_days_to_expiry=min_days,
        contract_expiry=expiry,
        roll_schedule=roll_schedule,
        trading_calendar=trading_calendar,
    )


def _signals(*directions: str, dates: tuple[str, ...] = (D1, D2, D3)) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": dates, "direction": directions})


def test_mapping_roll_executes_both_legs_at_same_close_with_two_sided_costs(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D1: _bar(1000, 1000, low=990, high=1010),
                D2: _bar(900, 1010, low=980, high=1020),
            },
            CONTRACT_B: {
                D2: _bar(2000, 1100, low=1080, high=1120),
                D3: _bar(1100, 1100, low=1090, high=1110),
            },
        },
        expiry={CONTRACT_A: "20240216", CONTRACT_B: "20240315"},
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
        commission=0.001,
        slippage=1.0,
        method="mapping",
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))

    day_two = result.daily_nav.set_index("date").loc[D2]
    assert day_two["contract"] == CONTRACT_B
    assert day_two["position"] == "LONG"
    assert day_two["nav"] == pytest.approx(200_777.8)

    old_leg = result.trades.iloc[0]
    new_leg = result.trades.iloc[1]
    assert old_leg["contract"] == CONTRACT_A
    assert old_leg["exit_reason"] == "contract_roll"
    assert old_leg["exit_date"] == D2
    assert old_leg["exit_price"] == pytest.approx(1009.0)
    assert old_leg["commission_yuan"] == pytest.approx(402.0)
    assert old_leg["pnl_yuan"] == pytest.approx(1198.0)

    assert new_leg["contract"] == CONTRACT_B
    assert new_leg["entry_date"] == D2
    assert new_leg["entry_price"] == pytest.approx(1101.0)
    assert new_leg["exit_price"] == pytest.approx(1099.0)
    assert new_leg["commission_yuan"] == pytest.approx(440.0)
    assert new_leg["pnl_yuan"] == pytest.approx(-840.0)
    assert result.final_nav == pytest.approx(200_358.0)
    assert result.final_nav - result.account_size == pytest.approx(
        result.trades["pnl_yuan"].sum()
    )


def test_roll_happens_before_close_margin_check(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {D1: _bar(1000, 1000), D2: _bar(1000, 900)},
            CONTRACT_B: {D2: _bar(100, 100), D3: _bar(100, 100)},
        },
        expiry={CONTRACT_A: "20240216", CONTRACT_B: "20240315"},
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
        account=30_000.0,
        method="mapping",
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))

    day_two = result.daily_nav.set_index("date").loc[D2]
    assert day_two["contract"] == CONTRACT_B
    assert day_two["position"] == "LONG"
    assert result.trades.iloc[0]["exit_reason"] == "contract_roll"
    assert "margin_call" not in set(result.trades["exit_reason"])


def test_new_contract_margin_is_checked_after_close_roll(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {D1: _bar(1000, 1000), D2: _bar(1000, 1000)},
            CONTRACT_B: {D2: _bar(2000, 2000), D3: _bar(2000, 2000)},
        },
        expiry={CONTRACT_A: "20240216", CONTRACT_B: "20240315"},
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
        account=30_000.0,
        method="mapping",
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))

    assert result.daily_nav.set_index("date").loc[D2, "position"] == "FLAT"
    assert list(result.trades["exit_reason"]) == [
        "contract_roll", "margin_call"
    ]
    assert result.trades.iloc[1]["entry_date"] == D2
    assert result.trades.iloc[1]["exit_date"] == D2


def test_expiry_roll_uses_expiry_close_without_overnight_gap(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D0: _bar(1000, 1000, vol=200),
                D1: _bar(1000, 1000, vol=100),
                D2: _bar(700, 1010, vol=10),
            },
            CONTRACT_B: {
                D0: _bar(1100, 1100, vol=50),
                D1: _bar(1100, 1100, vol=300),
                D2: _bar(2000, 1100, vol=300),
                D3: _bar(1100, 1100, vol=300),
            },
        },
        expiry={CONTRACT_A: D2, CONTRACT_B: "20240315"},
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))

    assert result.trades.iloc[0]["exit_reason"] == "contract_expiry"
    assert result.trades.iloc[0]["exit_date"] == D2
    assert result.trades.iloc[0]["exit_price"] == pytest.approx(1010.0)
    assert result.trades.iloc[1]["entry_date"] == D2
    assert result.trades.iloc[1]["entry_price"] == pytest.approx(1100.0)
    assert result.daily_nav.set_index("date").loc[D2, "contract"] == CONTRACT_B


def test_expiry_close_charges_both_sides_and_slippage_enters_nav(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D0: _bar(1000, 1000, vol=200),
                D1: _bar(1000, 1000, vol=100),
                D2: _bar(900, 1010, vol=10),
            },
            CONTRACT_B: {
                D0: _bar(1100, 1100, vol=50),
                D1: _bar(1100, 1100, vol=300),
                D2: _bar(2000, 1100, vol=300),
                D3: _bar(1100, 1100, vol=300),
            },
        },
        expiry={CONTRACT_A: D2, CONTRACT_B: "20240315"},
        commission=0.001,
        slippage=1.0,
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))

    expiry_trade = result.trades.iloc[0]
    assert expiry_trade["exit_reason"] == "contract_expiry"
    assert expiry_trade["exit_price"] == pytest.approx(1009.0)
    assert expiry_trade["commission_yuan"] == pytest.approx(402.0)
    assert expiry_trade["pnl_yuan"] == pytest.approx(1198.0)
    assert result.final_nav - result.account_size == pytest.approx(
        result.trades["pnl_yuan"].sum()
    )


def test_expiry_on_final_observation_does_not_open_and_force_close_replacement(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D0: _bar(1000, 1000, vol=200),
                D1: _bar(1000, 1000, vol=100),
                D2: _bar(900, 1010, vol=10),
            },
            CONTRACT_B: {
                D0: _bar(1100, 1100, vol=50),
                D1: _bar(1100, 1100, vol=300),
                D2: _bar(2000, 1100, vol=300),
            },
        },
        expiry={CONTRACT_A: D2, CONTRACT_B: "20240315"},
        commission=0.001,
        slippage=1.0,
        end=D2,
    )

    result = engine.run(_signals("LONG", "LONG", dates=(D1, D2)))

    assert result.trade_count == 1
    trade = result.trades.iloc[0]
    assert trade["contract"] == CONTRACT_A
    assert trade["exit_date"] == D2
    assert trade["exit_reason"] == "end_of_backtest"
    assert trade["commission_yuan"] == pytest.approx(402.0)
    assert result.final_nav == pytest.approx(201_198.0)
    assert set(result.trades["contract"]) == {CONTRACT_A}


def test_expiry_without_replacement_fails_instead_of_creating_exposure_gap(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D0: _bar(1000, 1000),
                D1: _bar(1000, 1000),
                D2: _bar(1000, 1000),
                D3: _bar(1000, 1000),
            },
        },
        expiry={CONTRACT_A: D2},
    )

    with pytest.raises(ValueError, match="replacement|roll"):
        engine.run(_signals("LONG", "LONG", "LONG"))

    assert engine.last_result_contract is not None
    assert engine.last_result_contract.status.value == "FAILED"


def test_final_day_exit_cost_does_not_overwrite_intraday_nav_worst(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {CONTRACT_A: {D1: _bar(1000, 1000, low=900, high=1010)}},
        expiry={CONTRACT_A: "20240216"},
        roll_schedule={D1: CONTRACT_A},
        account=100_000.0,
        slippage=1.0,
        method="mapping",
        end=D1,
    )

    result = engine.run(_signals("LONG", dates=(D1,)))

    row = result.daily_nav.iloc[0]
    assert result.final_nav == pytest.approx(99_600.0)
    assert row["nav"] == pytest.approx(99_600.0)
    assert row["nav_worst"] == pytest.approx(79_800.0)
    assert result.trades.iloc[0]["pnl_yuan"] == pytest.approx(-400.0)


def test_nav_worst_is_stress_only_and_never_triggers_margin(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {CONTRACT_A: {D1: _bar(1000, 1000, low=950, high=1010)}},
        expiry={CONTRACT_A: "20240216"},
        roll_schedule={D1: CONTRACT_A},
        account=30_000.0,
        method="mapping",
        end=D1,
    )

    result = engine.run(_signals("LONG", dates=(D1,)))

    assert result.daily_nav.iloc[0]["nav_worst"] == pytest.approx(20_000.0)
    assert list(result.trades["exit_reason"]) == ["end_of_backtest"]


def test_engine_scope_and_assumptions_survive_json_roundtrip(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {CONTRACT_A: {D1: _bar(1000, 1000)}},
        expiry={CONTRACT_A: "20240216"},
        roll_schedule={D1: CONTRACT_A},
        method="mapping",
        end=D1,
    )
    result = engine.run(_signals("FLAT", dates=(D1,)))

    assert result.engine_scope == "approximate_index_futures_research"
    for key in (
        "engine_scope",
        "commission_model",
        "slippage_model",
        "margin_maintenance_checkpoint",
        "roll_execution_checkpoint",
        "settlement_model",
        "contract_expiry_schedule_sha256",
        "roll_schedule_sha256",
    ):
        assert isinstance(result.assumptions[key], str)
        assert result.assumptions[key]

    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload["engine_scope"] == result.engine_scope
    assert payload["assumptions"] == result.assumptions
    assert result.result_contract is not None
    contract = ResultContract.from_json(result.result_contract.to_json())
    roundtripped_assumptions = {
        item.key: item.value for item in contract.assumptions
    }
    assert roundtripped_assumptions["engine_scope"] == result.engine_scope
    assert (
        roundtripped_assumptions["roll_execution_checkpoint"]
        == "same_bar_close_exit_then_entry"
    )
