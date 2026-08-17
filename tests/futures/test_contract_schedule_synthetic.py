"""Pure tests for explicit futures expiry schedules and date normalization."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from diepi.futures.contract import ContractSelector
from diepi.futures.engine import FuturesEngine, _load_trading_calendar


PRODUCT = "IC"
CONTRACT_A = "ICA.CFX"
CONTRACT_B = "ICB.CFX"
D1 = "20240102"
D2 = "20240103"
D3 = "20240104"


def _rows(*, iso_dates: bool = False, include_future: bool = False) -> pd.DataFrame:
    dates = ["2024-01-02", "2024-01-03"] if iso_dates else [D1, D2]
    rows = []
    for date_index, trade_date in enumerate(dates):
        rows.extend([
            {
                "ts_code": CONTRACT_A,
                "trade_date": trade_date,
                "open": 1_000.0,
                "high": 1_010.0,
                "low": 990.0,
                "close": 1_000.0,
                "vol": 100.0 if date_index == 0 else 80.0,
            },
            {
                "ts_code": CONTRACT_B,
                "trade_date": trade_date,
                "open": 1_100.0,
                "high": 1_110.0,
                "low": 1_090.0,
                "close": 1_100.0,
                "vol": 50.0 if date_index == 0 else 120.0,
            },
        ])
    if include_future:
        rows.extend([
            {
                "ts_code": CONTRACT_A,
                "trade_date": "20240108",
                "open": 1_000.0,
                "high": 1_010.0,
                "low": 990.0,
                "close": 1_000.0,
                "vol": 10.0,
            },
            {
                "ts_code": CONTRACT_B,
                "trade_date": "20240115",
                "open": 1_100.0,
                "high": 1_110.0,
                "low": 1_090.0,
                "close": 1_100.0,
                "vol": 10.0,
            },
        ])
    return pd.DataFrame(rows)


def _write_contracts(root: Path, frame: pd.DataFrame) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(root / f"{PRODUCT}_contracts.parquet", index=False)
    return root


def _expiry_schedule() -> dict[str, str]:
    return {CONTRACT_A: "20240110", CONTRACT_B: "20240120"}


def test_selector_refuses_to_infer_expiry_from_last_downloaded_row(tmp_path):
    data_dir = _write_contracts(tmp_path / "contracts", _rows())

    with pytest.raises(ValueError, match="explicit contract expiry schedule"):
        ContractSelector(PRODUCT, str(data_dir))


def test_engine_refuses_market_data_as_its_expected_trading_calendar(tmp_path):
    data_dir = _write_contracts(tmp_path / "engine_no_calendar", _rows())

    with pytest.raises(ValueError, match="explicit trading_calendar"):
        FuturesEngine(
            product=PRODUCT,
            start_date=D1,
            end_date=D2,
            contract_data_dir=str(data_dir),
            continuous_data_dir=str(tmp_path / "no_mapping"),
            contract_expiry=_expiry_schedule(),
        )


def test_calendar_table_closed_rows_prove_requested_tail_coverage():
    frame = pd.DataFrame({
        "cal_date": [D1, D2, D3],
        "is_open": [1, 0, 0],
    })

    calendar, source, digest, coverage_start, coverage_end = (
        _load_trading_calendar(frame)
    )

    assert calendar == (D1,)
    assert source == "explicit_dataframe"
    assert len(digest) == 64
    assert (coverage_start, coverage_end) == (D1, D3)


def test_calendar_table_cannot_hide_an_interior_missing_date():
    frame = pd.DataFrame({
        "cal_date": [D1, D3],
        "is_open": [1, 1],
    })

    with pytest.raises(ValueError, match="every calendar date"):
        _load_trading_calendar(frame)


def test_calendar_rejects_conflicting_duplicate_status_rows():
    frame = pd.DataFrame({
        "cal_date": [D1, D1],
        "is_open": [0, 1],
    })

    with pytest.raises(ValueError, match="conflicting is_open"):
        _load_trading_calendar(frame)


def test_open_day_calendar_safely_extends_coverage_over_weekend_only():
    calendar, _source, _digest, coverage_start, coverage_end = (
        _load_trading_calendar(["20240105"])
    )

    assert calendar == ("20240105",)
    assert coverage_start == "20240105"
    assert coverage_end == "20240107"


def test_truncating_future_rows_does_not_change_prior_selection(tmp_path):
    full_dir = _write_contracts(tmp_path / "full", _rows(include_future=True))
    truncated_dir = _write_contracts(tmp_path / "truncated", _rows())
    expiry = _expiry_schedule()

    full = ContractSelector(PRODUCT, str(full_dir), contract_expiry=expiry)
    truncated = ContractSelector(
        PRODUCT, str(truncated_dir), contract_expiry=expiry)

    full_selection = full.select(D2, min_days_remaining=2)
    truncated_selection = truncated.select(D2, min_days_remaining=2)

    assert full_selection is not None
    assert truncated_selection is not None
    assert full_selection.ts_code == truncated_selection.ts_code == CONTRACT_A
    assert full_selection.expiry_date == truncated_selection.expiry_date == "20240110"


def test_equal_t1_volume_selection_is_independent_of_row_order(tmp_path):
    frame = _rows()
    frame.loc[frame["trade_date"] == D1, "vol"] = 100.0
    forward_dir = _write_contracts(tmp_path / "tie_forward", frame)
    reverse_dir = _write_contracts(
        tmp_path / "tie_reverse", frame.iloc[::-1].reset_index(drop=True))

    forward = ContractSelector(
        PRODUCT, str(forward_dir), contract_expiry=_expiry_schedule())
    reverse = ContractSelector(
        PRODUCT, str(reverse_dir), contract_expiry=_expiry_schedule())

    assert forward.select(D2, min_days_remaining=2).ts_code == CONTRACT_A
    assert reverse.select(D2, min_days_remaining=2).ts_code == CONTRACT_A


def test_invalid_t1_volume_fails_instead_of_changing_rank(tmp_path):
    frame = _rows()
    frame.loc[
        (frame["trade_date"] == D1) & (frame["ts_code"] == CONTRACT_A),
        "vol",
    ] = float("nan")
    data_dir = _write_contracts(tmp_path / "invalid_volume", frame)
    selector = ContractSelector(
        PRODUCT, str(data_dir), contract_expiry=_expiry_schedule())

    with pytest.raises(ValueError, match="Invalid T-1 volume"):
        selector.select(D2, min_days_remaining=2)


def test_missing_today_data_for_t1_winner_fails_instead_of_selecting_runner_up(
    tmp_path,
):
    frame = _rows()
    frame = frame[
        ~(
            (frame["trade_date"] == D2)
            & (frame["ts_code"] == CONTRACT_A)
        )
    ].reset_index(drop=True)
    data_dir = _write_contracts(tmp_path / "winner_missing_today", frame)
    selector = ContractSelector(
        PRODUCT, str(data_dir), contract_expiry=_expiry_schedule())

    with pytest.raises(ValueError, match=CONTRACT_A):
        selector.select(D2, min_days_remaining=2)


def test_selector_normalizes_iso_and_compact_trading_dates_identically(tmp_path):
    iso_dir = _write_contracts(tmp_path / "iso", _rows(iso_dates=True))
    compact_dir = _write_contracts(tmp_path / "compact", _rows())

    iso_selector = ContractSelector(
        PRODUCT,
        str(iso_dir),
        contract_expiry={CONTRACT_A: "2024-01-10", CONTRACT_B: "2024-01-20"},
    )
    compact_selector = ContractSelector(
        PRODUCT, str(compact_dir), contract_expiry=_expiry_schedule())

    iso_selection = iso_selector.select("2024-01-03", min_days_remaining=2)
    compact_selection = compact_selector.select(D2, min_days_remaining=2)

    assert iso_selector.calendar == compact_selector.calendar == [D1, D2]
    assert iso_selection is not None
    assert compact_selection is not None
    assert iso_selection.ts_code == compact_selection.ts_code == CONTRACT_A
    assert iso_selection.trade_date == compact_selection.trade_date == D2


def test_explicit_roll_schedule_is_normalized_and_required_for_mapping(tmp_path):
    data_dir = _write_contracts(tmp_path / "roll", _rows())
    expiry = _expiry_schedule()
    without_roll = ContractSelector(
        PRODUCT, str(data_dir), contract_expiry=expiry)
    with pytest.raises(ValueError, match="explicit roll schedule"):
        without_roll.select(D2, method="mapping", min_days_remaining=0)

    with_roll = ContractSelector(
        PRODUCT,
        str(data_dir),
        contract_expiry=expiry,
        roll_schedule={"2024-01-03": CONTRACT_B},
    )
    selection = with_roll.select(
        "2024-01-03", method="mapping", min_days_remaining=0)

    assert selection is not None
    assert selection.ts_code == CONTRACT_B
    assert selection.trade_date == D2
    assert with_roll.roll_schedule_source == "explicit_roll_schedule"


def test_engine_normalizes_iso_window_and_signal_dates(tmp_path):
    data_dir = _write_contracts(tmp_path / "engine", _rows(iso_dates=True))
    engine = FuturesEngine(
        product=PRODUCT,
        start_date="2024-01-02",
        end_date=D2,
        account_size=100_000.0,
        commission_rate=0.0,
        slippage_points=0.0,
        contract_data_dir=str(data_dir),
        continuous_data_dir=str(tmp_path / "no_mapping"),
        contract_expiry=_expiry_schedule(),
        trading_calendar=["2024-01-02", D2],
    )
    signals = pd.DataFrame({
        "trade_date": ["2024-01-02", D2],
        "direction": ["LONG", "LONG"],
    })

    result = engine.run(signals)

    assert result.start_date == D1
    assert result.end_date == D2
    assert result.daily_nav["date"].tolist() == [D1, D2]
    assert result.assumptions["trading_calendar_source"] == "explicit_iterable"
    assert len(result.assumptions["trading_calendar_sha256"]) == 64


def test_explicit_roll_schedule_cannot_be_silently_ignored_by_volume_method(
    tmp_path,
):
    with pytest.raises(ValueError, match="requires contract_method='mapping'"):
        FuturesEngine(
            product=PRODUCT,
            start_date=D1,
            end_date=D2,
            contract_data_dir=str(tmp_path / "not_reached"),
            continuous_data_dir=str(tmp_path / "not_reached"),
            contract_method="volume_t1",
            contract_expiry=_expiry_schedule(),
            roll_schedule={D1: CONTRACT_A},
        )


def test_explicit_roll_schedule_drives_held_contract_change(tmp_path):
    frame = _rows()
    day_three = frame[frame["trade_date"] == D2].copy()
    day_three["trade_date"] = D3
    data_dir = _write_contracts(
        tmp_path / "engine_roll",
        pd.concat([frame, day_three], ignore_index=True),
    )
    engine = FuturesEngine(
        product=PRODUCT,
        start_date=D1,
        end_date=D3,
        account_size=100_000.0,
        commission_rate=0.0,
        slippage_points=0.0,
        contract_data_dir=str(data_dir),
        continuous_data_dir=str(tmp_path / "no_mapping"),
        contract_method="mapping",
        min_days_to_expiry=0,
        contract_expiry=_expiry_schedule(),
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
        trading_calendar=[D1, D2, D3],
    )
    signals = pd.DataFrame({
        "trade_date": [D1, D2, D3],
        "direction": ["LONG", "LONG", "FLAT"],
    })

    result = engine.run(signals)

    assert result.trade_count == 2
    rolled = result.trades.iloc[0]
    replacement = result.trades.iloc[1]
    assert rolled["contract"] == CONTRACT_A
    assert rolled["exit_date"] == D2
    assert rolled["exit_reason"] == "contract_roll"
    assert replacement["contract"] == CONTRACT_B
    assert replacement["entry_date"] == D2


def test_roll_date_mapping_changes_only_at_the_scheduled_close(tmp_path):
    data_dir = _write_contracts(tmp_path / "close_checkpoint", _rows())
    selector = ContractSelector(
        PRODUCT,
        str(data_dir),
        contract_expiry=_expiry_schedule(),
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
    )

    open_selection = selector.select(
        D2, method="mapping", min_days_remaining=0)
    close_selection = selector.select_for_close_roll(
        D2, method="mapping", min_days_remaining=0)

    assert open_selection is not None
    assert close_selection is not None
    assert open_selection.ts_code == CONTRACT_A
    assert close_selection.ts_code == CONTRACT_B
    assert selector.should_roll(CONTRACT_A, D2)


def test_mapping_never_falls_back_to_volume_ranked_contract(tmp_path):
    data_dir = _write_contracts(tmp_path / "no_mapping_fallback", _rows())
    selector = ContractSelector(
        PRODUCT,
        str(data_dir),
        contract_expiry=_expiry_schedule(),
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
    )

    with pytest.raises(ValueError, match="configured minimum"):
        selector.select(D2, method="mapping", min_days_remaining=10)


def test_roll_schedule_rejects_unknown_contract_at_construction(tmp_path):
    data_dir = _write_contracts(tmp_path / "unknown_mapping", _rows())

    with pytest.raises(ValueError, match="unknown contracts"):
        ContractSelector(
            PRODUCT,
            str(data_dir),
            contract_expiry=_expiry_schedule(),
            roll_schedule={D1: "IC_UNKNOWN.CFX"},
        )
