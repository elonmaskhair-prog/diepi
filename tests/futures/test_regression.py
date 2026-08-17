"""Regression tests for FuturesEngine fixes #1-8.

Tests:
1. Contract expiry → same-day rollover re-entry
2. hold_days matches actual holding
3. High min_days_remaining doesn't infinite-recurse (fix #4)
4. Low-capital account rejected for insufficient margin (fix #5)
5. Duplicate date input raises error (fix #6)
6. Combiner rejects non-identical leg calendars
"""
from __future__ import annotations

import sys
import traceback
from functools import lru_cache
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from diepi.futures import FuturesEngine, FuturesCombiner
from diepi.futures.contract import ContractSelector


pytestmark = pytest.mark.integration


PASS = 0
FAIL = 0


@lru_cache(maxsize=None)
def _snapshot_expiry_schedule(product: str) -> dict[str, str]:
    """Explicit legacy integration fixture for the known complete snapshot.

    Production code must receive an independent schedule and never performs
    this inference.  The truncation-invariance unit test supplies a fixed
    schedule and exercises that production contract directly.
    """
    from diepi.backtest.config import PARQUET_ROOT

    path = Path(PARQUET_ROOT) / "futures_daily" / f"{product}_contracts.parquet"
    frame = pd.read_parquet(path, columns=["ts_code", "trade_date"])
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame.groupby("ts_code")["trade_date"].max().to_dict()


@lru_cache(maxsize=1)
def _snapshot_trading_calendar() -> tuple[str, ...]:
    """Use the independent exchange calendar, never contract quote dates."""
    from diepi.backtest.config import METADATA_PATHS

    frame = pd.read_parquet(
        METADATA_PATHS["trade_cal"], columns=["cal_date", "is_open"]
    )
    return tuple(sorted(
        frame.loc[frame["is_open"].astype(int) == 1, "cal_date"]
        .astype(str)
        .unique()
        .tolist()
    ))


def _integration_engine(**kwargs) -> FuturesEngine:
    kwargs.setdefault(
        "contract_expiry", _snapshot_expiry_schedule(kwargs["product"]))
    kwargs.setdefault("trading_calendar", _snapshot_trading_calendar())
    return FuturesEngine(**kwargs)


class CheckFailure(AssertionError):
    """Assertion raised by ``check`` and recognized by the direct runner."""


def check(name: str, condition: bool, detail: str = ""):
    """Record a readable check and fail normally under pytest."""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        message = f"{name} — {detail}" if detail else name
        print(f"  [FAIL] {message}")
        raise CheckFailure(message)


def test_1_contract_expiry_reentry():
    """Fix #1/#2: If contract expires mid-holding, engine exits and re-enters same day."""
    print("\n=== Test 1: Contract expiry → same-day rollover ===")

    # Use a SHORT signal spanning a known contract boundary
    # IC contracts expire ~3rd Friday. We'll use a long LONG block that forces a rollover.
    engine = _integration_engine(product="IC", start_date="20190301", end_date="20190331",
                                 account_size=300_000, lots=1)
    cal = engine._selector.calendar
    cal = [d for d in cal if "20190301" <= d <= "20190331"]

    # All LONG — will force at least one contract expiry + rollover
    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])

    result = engine.run(signals)
    check("Result is valid", result.is_valid, str(result.validation_errors))

    # Should have at least one same-close contract-expiry trade.
    if not result.trades.empty:
        expired = result.trades[result.trades["exit_reason"] == "contract_expiry"]
        check("Has contract_expiry trade", len(expired) > 0,
              f"exit_reasons: {result.trades['exit_reason'].unique().tolist()}")
        # D-6 requires the replacement to enter at the same scheduled close.
        if len(expired) > 0:
            exp_idx = result.trades.index[result.trades["exit_reason"] == "contract_expiry"][0]
            if exp_idx + 1 < len(result.trades):
                next_trade = result.trades.iloc[exp_idx + 1]
                check("Same-close re-entry after expiry",
                      next_trade["entry_date"] == expired.iloc[0]["exit_date"],
                      f"expired exit={expired.iloc[0]['exit_date']}, next entry={next_trade['entry_date']}")
    else:
        check("Has trades", False, "No trades generated")


def test_2_hold_days():
    """Fix #3: hold_days = 0 on entry day, actual nights held."""
    print("\n=== Test 2: hold_days accuracy ===")

    engine = _integration_engine(product="IC", start_date="20250101", end_date="20250228",
                                 account_size=300_000, lots=1)
    cal = engine._selector.calendar
    cal = [d for d in cal if "20250101" <= d <= "20250228"]

    # LONG for 5 days, then FLAT
    signals = []
    for i, d in enumerate(cal):
        if i < 5:
            signals.append({"trade_date": d, "direction": "LONG"})
        else:
            signals.append({"trade_date": d, "direction": "FLAT"})

    result = engine.run(pd.DataFrame(signals))
    check("Result is valid", result.is_valid, str(result.validation_errors))

    if not result.trades.empty:
        t = result.trades.iloc[0]
        # Entry day = 0, then 4 more days, exit on day 5 (when FLAT signal arrives)
        # hold_days should be 5 (the number of days position was held, entry day=0, first full day=1, ..., day 4, exit on day 5)
        entry_idx = cal.index(t["entry_date"])
        exit_idx = cal.index(t["exit_date"])
        actual_days = exit_idx - entry_idx
        check(f"hold_days={t['hold_days']} matches calendar diff={actual_days}",
              t["hold_days"] == actual_days,
              f"entry={t['entry_date']} exit={t['exit_date']}")
    else:
        check("Has trades", False, "No trades")


def test_3_no_recursion():
    """Fix #4: High min_days_remaining doesn't cause infinite recursion."""
    print("\n=== Test 3: No recursion with extreme min_days ===")

    engine = _integration_engine(product="IC", start_date="20250101", end_date="20250228",
                                 account_size=300_000, lots=1, min_days_to_expiry=999)
    cal = engine._selector.calendar
    cal = [d for d in cal if "20250101" <= d <= "20250228"]

    # LONG signal but min_days=999 means no contract qualifies → should stay flat
    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])

    try:
        result = engine.run(signals)
        check("No recursion error", True)
        check("No trades (no contract qualifies)", result.trade_count == 0,
              f"trade_count={result.trade_count}")
    except RecursionError:
        check("No recursion error", False, "RecursionError raised!")


def test_4_margin_check():
    """Fix #5: Low-capital account can't open IC position."""
    print("\n=== Test 4: Margin check ===")

    # IC ~5800 * 200 = ~1.16M notional, margin 14% = ~162K needed
    # 10K account should fail margin check
    engine = _integration_engine(product="IC", start_date="20250101", end_date="20250228",
                                 account_size=10_000, lots=1)
    cal = engine._selector.calendar
    cal = [d for d in cal if "20250101" <= d <= "20250228"]

    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])
    result = engine.run(signals)

    check("Result is valid", result.is_valid, str(result.validation_errors))
    check("No trades (insufficient margin)", result.trade_count == 0,
          f"trade_count={result.trade_count}")
    check("NAV unchanged", abs(result.final_nav - 10_000) < 1,
          f"final_nav={result.final_nav}")


def test_5_duplicate_dates():
    """Fix #6: Duplicate trade_date raises ValueError."""
    print("\n=== Test 5: Duplicate date rejection ===")

    engine = _integration_engine(product="IC", start_date="20250101", end_date="20250131",
                                 account_size=300_000, lots=1)

    signals = pd.DataFrame([
        {"trade_date": "20250102", "direction": "LONG"},
        {"trade_date": "20250102", "direction": "SHORT"},  # duplicate!
        {"trade_date": "20250103", "direction": "FLAT"},
    ])

    try:
        engine.run(signals)
        check("Raises on duplicate dates", False, "No exception raised")
    except ValueError as e:
        check("Raises on duplicate dates", "Duplicate" in str(e), str(e))


def test_6_combiner_rejects_mismatched_calendars():
    """Combination must never union or forward-fill leg calendars."""
    print("\n=== Test 6: Combiner exact-calendar requirement ===")

    # Run IC short and IM long over overlapping but not identical periods
    ic_engine = _integration_engine(product="IC", start_date="20230101", end_date="20230331",
                                    account_size=300_000, lots=1)
    ic_cal = [d for d in ic_engine._selector.calendar if "20230101" <= d <= "20230331"]

    im_engine = _integration_engine(product="IM", start_date="20230201", end_date="20230331",
                                    account_size=300_000, lots=1)
    im_cal = [d for d in im_engine._selector.calendar if "20230201" <= d <= "20230331"]

    # IC: all LONG Jan-Mar; IM: all LONG Feb-Mar
    ic_signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in ic_cal])
    im_signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in im_cal])

    ic_result = ic_engine.run(ic_signals)
    im_result = im_engine.run(im_signals)

    try:
        FuturesCombiner.combine(
            [ic_result, im_result], ["IC_long", "IM_long"])
        check("Rejects mismatched calendars", False, "No exception raised")
    except ValueError as exc:
        check(
            "Rejects mismatched calendars",
            "calendars must match exactly" in str(exc),
            str(exc),
        )


def test_7_validate_check3():
    """Validate check #3: position past expiry detected."""
    print("\n=== Test 7: Validate check #3 (position past expiry) ===")

    # If engine is correct, this should never trigger. We verify by
    # running a normal backtest and confirming check #3 passes.
    engine = _integration_engine(product="IC", start_date="20190301", end_date="20190331",
                                 account_size=300_000, lots=1)
    cal = [d for d in engine._selector.calendar if "20190301" <= d <= "20190331"]
    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])

    result = engine.run(signals)
    check("Check #3 passes (no expired positions)", result.is_valid,
          str(result.validation_errors))

    # Verify that the validator actually ran by checking position days exist
    pos_days = result.daily_nav[result.daily_nav["position"] != "FLAT"]
    check("Has position days to validate", len(pos_days) > 0)


def test_8_margin_maintenance():
    """Margin maintenance: position liquidated when NAV drops below required margin."""
    print("\n=== Test 8: Margin maintenance (daily check) ===")

    # IC 300K LONG from 20150601. The 2015 crash should trigger margin call.
    engine = _integration_engine(product="IC", start_date="20150601", end_date="20150731",
                                 account_size=300_000, lots=1)
    cal = [d for d in engine._selector.calendar if "20150601" <= d <= "20150731"]
    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])

    result = engine.run(signals)
    check("Result is valid", result.is_valid, str(result.validation_errors))

    # Should have at least one margin_call exit during the crash
    if not result.trades.empty:
        mc_trades = result.trades[result.trades["exit_reason"] == "margin_call"]
        check("Has margin_call exit during 2015 crash", len(mc_trades) > 0,
              f"exit_reasons: {result.trades['exit_reason'].unique().tolist()}")

        # After margin call, NAV should never be deeply negative
        min_nav = result.daily_nav["nav"].min()
        check(f"NAV stays positive (min={min_nav:,.0f})", min_nav > 0)

        # Margin-call day should still preserve the intraday worst mark.
        if len(mc_trades) > 0:
            mc_day = mc_trades.iloc[0]["exit_date"]
            row = result.daily_nav[result.daily_nav["date"] == mc_day]
            if not row.empty:
                row = row.iloc[0]
                check("Margin-call day keeps worst NAV", row["nav_worst"] < row["nav"],
                      f"nav={row['nav']:,.0f} nav_worst={row['nav_worst']:,.0f}")
    else:
        check("Has trades", False, "No trades")


def test_9_expiry_exit_date():
    """Contract expiry exit_date should be the last valid day, not the discovery day."""
    print("\n=== Test 9: Expiry exit_date accuracy ===")

    # LONG IC across March 2019 expiry (IC1903 expires 20190315)
    engine = _integration_engine(product="IC", start_date="20190301", end_date="20190331",
                                 account_size=300_000, lots=1)
    cal = [d for d in engine._selector.calendar if "20190301" <= d <= "20190331"]
    signals = pd.DataFrame([{"trade_date": d, "direction": "LONG"} for d in cal])

    result = engine.run(signals)
    check("Result is valid", result.is_valid, str(result.validation_errors))

    if not result.trades.empty:
        expired = result.trades[result.trades["exit_reason"] == "contract_expiry"]
        if len(expired) > 0:
            t = expired.iloc[0]
            # exit_date should be the last valid day of the contract, not the next day
            last_valid = engine._selector.get_expiry(t["contract"])
            check(f"exit_date ({t['exit_date']}) <= contract last day ({last_valid})",
                  t["exit_date"] <= last_valid,
                  f"exit_date={t['exit_date']} but contract last data={last_valid}")
        else:
            check("Has contract_expiry trade", False,
                  f"exit_reasons: {result.trades['exit_reason'].unique().tolist()}")
    else:
        check("Has trades", False, "No trades")


def test_10_single_day_backtest():
    """Single-day backtest should not raise validation error."""
    print("\n=== Test 10: Single-day backtest ===")

    engine = _integration_engine(product="IC", start_date="20250102", end_date="20250102",
                                 account_size=300_000, lots=1)

    signals = pd.DataFrame([{"trade_date": "20250102", "direction": "LONG"}])

    try:
        result = engine.run(signals)
        check("No validation error on single-day", result.is_valid,
              str(result.validation_errors))
        check("Has 1 trade", result.trade_count == 1,
              f"trade_count={result.trade_count}")
        if not result.trades.empty:
            t = result.trades.iloc[0]
            check(f"entry==exit=20250102", t["entry_date"] == t["exit_date"] == "20250102")
    except CheckFailure:
        raise
    except Exception as e:
        check("No exception on single-day", False, str(e))


def test_11_no_same_day_reentry_after_margin_call():
    """Margin call is a close event; engine must not re-enter on the same date."""
    print("\n=== Test 11: No same-day re-entry after margin call ===")

    engine = _integration_engine(product="IC", start_date="20190401", end_date="20210228",
                                 account_size=300_000, lots=1)
    cal = [d for d in engine._selector.calendar if "20190401" <= d <= "20210228"]
    signals = pd.DataFrame([{"trade_date": d, "direction": "SHORT"} for d in cal])

    result = engine.run(signals)
    check("Result is valid", result.is_valid, str(result.validation_errors))

    if result.trade_count >= 2:
        bad_pairs = []
        trades = result.trades.reset_index(drop=True)
        for i in range(len(trades) - 1):
            if trades.loc[i, "exit_reason"] == "margin_call":
                if trades.loc[i, "exit_date"] == trades.loc[i + 1, "entry_date"]:
                    bad_pairs.append((trades.loc[i, "exit_date"], trades.loc[i, "contract"],
                                      trades.loc[i + 1, "contract"]))
        check("No same-day re-entry after margin call", len(bad_pairs) == 0, str(bad_pairs[:3]))

        mc_days = result.trades[result.trades["exit_reason"] == "margin_call"]["exit_date"].tolist()
        if mc_days:
            rows = result.daily_nav[result.daily_nav["date"].isin(mc_days)]
            all_flat = bool((rows["position"] == "FLAT").all()) if not rows.empty else True
            check("Margin-call exit days end flat", all_flat, rows.to_dict("records")[:3])
    else:
        check("Has enough trades to evaluate", False, f"trade_count={result.trade_count}")


if __name__ == "__main__":
    tests = [
        test_1_contract_expiry_reentry,
        test_2_hold_days,
        test_3_no_recursion,
        test_4_margin_check,
        test_5_duplicate_dates,
        test_6_combiner_rejects_mismatched_calendars,
        test_7_validate_check3,
        test_8_margin_maintenance,
        test_9_expiry_exit_date,
        test_10_single_day_backtest,
        test_11_no_same_day_reentry_after_margin_call,
    ]

    for t in tests:
        try:
            t()
        except CheckFailure:
            # ``check`` already recorded and printed this failure.
            continue
        except Exception:
            FAIL += 1
            print(f"  [ERROR] {t.__name__} raised unexpected exception:")
            traceback.print_exc()

    print(f"\n{'=' * 50}")
    print(f"  TOTAL: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 50}")
    sys.exit(FAIL)
