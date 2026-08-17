"""Explicit daily-auction assumptions for tests unrelated to liquidity.

Production defaults remain fail-fast.  Tests that exercise another contract
must opt into this deliberately large fixed cap instead of borrowing the
current day's full-session turnover through the old implicit behavior.
"""

from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy


EXPLICIT_TEST_AUCTION_POLICY = DailyAuctionLiquidityPolicy(
    open_cap=AuctionCapSpec.fixed_yuan(1_000_000_000_000.0),
    close_cap=AuctionCapSpec.fixed_yuan(1_000_000_000_000.0),
)


__all__ = ["EXPLICIT_TEST_AUCTION_POLICY"]
