# -*- coding: utf-8 -*-
"""
涨跌停规则测试

覆盖历史 bug F3：所有 ETF 前缀一律按 10% 处理，
而 588xxx 科创板 ETF 与创业板跟踪 ETF (159915 等) 现实是 20%——
在标的单日波动 >10% 的日子（如 KC50 2026-07-21 +11%），
旧规则会错误拒掉本可成交的单。
"""

import json
from dataclasses import replace

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.rulebook import (
    LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE,
    LIMIT_BAND_SNAPSHOT_HASH,
    LIMIT_BAND_SNAPSHOT_COVERAGE,
    LIMIT_BAND_SOURCE_VERSION,
    LimitBandRoundingPolicy,
    LimitBandRuleNotFoundError,
    LimitBandService,
    RuleBook,
    UnsupportedInstrumentError,
)


def _broker(**kwargs) -> Broker:
    return Broker(account=Account(initial_cash=1_000_000.0), **kwargs)


class TestBuiltinLimitRules:
    """内置规则"""

    def test_star_etf_20pct(self):
        b = _broker()
        assert b._get_limit_pct('588000.SH', '20260721') == pytest.approx(0.20)
        assert b._get_limit_pct('588080.SH', '20260721') == pytest.approx(0.20)
        assert b._get_limit_pct('589000.SH', '20260721') == pytest.approx(0.20)

    def test_star_etf_real_20pct_bar_passes_default_rule(self):
        b = _broker()
        b.set_date('20241008')
        bar = BarData(
            symbol='588000.SH', trade_time='20241008',
            open=1.140, high=1.140, low=1.045, close=1.140,
            vol=226_048_131.07, amount=25_494_037_870.0,
            pre_close=0.950,
        )

        b.validate_execution_bar_price_band(bar)

    def test_wrong_10pct_override_conflicting_with_real_star_etf_bar_fails(self):
        b = _broker(limit_pct_overrides={'588000': 0.10})
        b.set_date('20241008')
        bar = BarData(
            symbol='588000.SH', trade_time='20241008',
            open=1.140, high=1.140, low=1.045, close=1.140,
            vol=226_048_131.07, amount=25_494_037_870.0,
            pre_close=0.950,
        )

        with pytest.raises(ValueError, match=(
                r"588000\.SH.*20241008.*band=\[0\.855,1\.045\].*"
                r"limit_pct_override=0\.1")):
            b.validate_execution_bar_price_band(bar)

    def test_corporate_action_factor_reconciles_unadjusted_pre_close(self):
        b = _broker()
        b.set_date('20240105')
        bar = BarData(
            symbol='000001.SZ', trade_time='20240105',
            open=5.30, high=5.41, low=5.19, close=5.35,
            vol=1_000_000.0, amount=5_300_000.0,
            pre_close=10.50, adjustment_factor_ratio=2.0,
        )

        assert b._price_band_reference(bar) == pytest.approx(5.25)
        b.validate_execution_bar_price_band(bar)

    def test_gem_etf_20pct_after_reform(self):
        b = _broker()
        assert b._get_limit_pct('159915.SZ', '20200824') == pytest.approx(0.20)
        assert b._get_limit_pct('159915.SZ', '20200821') == pytest.approx(0.10)
        # 双创50 全系（2024-09-30 疯牛日实证 20% 涨停）
        for code in ('159780', '159781', '159782', '159783'):
            assert b._get_limit_pct(f'{code}.SZ', '20260721') == pytest.approx(0.20)

    @pytest.mark.parametrize(
        'symbol',
        ['160143.SZ', '160926.SZ', '161040.SZ', '168207.SZ'],
    )
    def test_audited_shenzhen_lofs_use_20pct_band_after_reform(self, symbol):
        service = LimitBandService()

        before = service.get_rule(symbol, '20200821')
        after = service.get_rule(symbol, '20200824')

        assert before.limit_pct == pytest.approx(0.10)
        assert before.rule_id == 'fund-10pct-pre-reform'
        assert after.limit_pct == pytest.approx(0.20)
        assert after.rule_id == 'metadata-or-observed-fund-20pct'

    def test_ordinary_etf_10pct(self):
        b = _broker()
        assert b._get_limit_pct('510300.SH', '20260721') == pytest.approx(0.10)
        assert b._get_limit_pct('159919.SZ', '20260721') == pytest.approx(0.10)
        assert b._get_limit_pct('511990.SH', '20260721') == pytest.approx(0.10)

    def test_stock_boards(self):
        b = _broker()
        assert b._get_limit_pct('000001.SZ', '20260721') == pytest.approx(0.10)
        assert b._get_limit_pct('688012.SH', '20260721') == pytest.approx(0.20)
        assert b._get_limit_pct('689009.SH', '20260721') == pytest.approx(0.20)
        assert b._get_limit_pct('300750.SZ', '20200824') == pytest.approx(0.20)
        assert b._get_limit_pct('300750.SZ', '20200821') == pytest.approx(0.10)
        # 302xxx 创业板新代码段（302132 中航成飞，2024+ 实证 20% 涨停）
        assert b._get_limit_pct('302132.SZ', '20250601') == pytest.approx(0.20)
        assert b._get_limit_pct('430047.BJ', '20211115') == pytest.approx(0.30)
        with pytest.raises(LimitBandRuleNotFoundError, match='BSE snapshot'):
            b._get_limit_pct('430047.BJ', '20211112')

    def test_rules_expose_effective_window_and_snapshot_identity(self):
        service = LimitBandService()
        before = service.get_rule('159915.SZ', '20200821')
        after = service.get_rule('159915.SZ', '20200824')

        assert before.effective_to == '20200823'
        assert after.effective_from == '20200824'
        assert before.rule_id == 'fund-10pct-pre-reform'
        assert after.rule_id == 'metadata-or-observed-fund-20pct'
        assert before.source_version == after.source_version == LIMIT_BAND_SOURCE_VERSION
        assert before.snapshot_hash == after.snapshot_hash == LIMIT_BAND_SNAPSHOT_HASH
        assert len(LIMIT_BAND_SNAPSHOT_HASH) == 64

        payload = after.to_dict()
        assert (
            payload['fund_snapshot_freeze_date']
            == LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE
            == '20260807'
        )
        assert payload['snapshot_coverage'] == LIMIT_BAND_SNAPSHOT_COVERAGE
        assert 'bse-rules-from-20211115' in LIMIT_BAND_SNAPSHOT_COVERAGE
        assert (
            'post-freeze-fund-issuance-not-detectable'
            in LIMIT_BAND_SNAPSHOT_COVERAGE
        )
        assert (
            'residual-misclassification-risk'
            in LIMIT_BAND_SNAPSHOT_COVERAGE
        )

    def test_bse_pre_open_dates_fail_fast_instead_of_inventing_a_band(self):
        service = LimitBandService()

        with pytest.raises(
            LimitBandRuleNotFoundError,
            match=r"BSE snapshot coverage begins 20211115",
        ):
            service.get_rule('430047.BJ', '20211114')

        opened = service.get_rule('430047.BJ', '20211115')
        assert opened.limit_pct == pytest.approx(0.30)
        assert opened.effective_from == '20211115'

    @pytest.mark.parametrize(
        ('symbol', 'effective_from'),
        [('159381.SZ', '20250324'), ('159388.SZ', '20250407')],
    )
    def test_metadata_union_closes_known_false_10pct_holes(
        self, symbol, effective_from
    ):
        service = LimitBandService()
        rule = service.get_rule(symbol, effective_from)

        assert rule.limit_pct == pytest.approx(0.20)
        assert rule.effective_from == effective_from

        previous_day = str(int(effective_from) - 1)
        with pytest.raises(LimitBandRuleNotFoundError, match='coverage begins'):
            service.get_rule(symbol, previous_day)

    def test_tick_is_resolved_from_instrument_rule(self):
        service = LimitBandService()
        stock = service.get_rule('000001.SZ', '20260721')
        fund = service.get_rule('510300.SH', '20260721')

        assert (stock.tick_size, stock.price_decimals) == (0.01, 2)
        assert (fund.tick_size, fund.price_decimals) == (0.001, 3)

    def test_rounding_policy_is_effective_dated_and_json_serializable(self):
        service = LimitBandService()

        with pytest.raises(LimitBandRuleNotFoundError, match='BSE snapshot'):
            service.get_rule('430047.BJ', '20211112')
        bse_after = service.get_rule('430047.BJ', '20211115')
        ordinary = service.get_rule('000001.SZ', '20260721')

        assert (
            bse_after.rounding_policy
            is LimitBandRoundingPolicy.TOWARD_REFERENCE
        )
        assert (
            ordinary.rounding_policy
            is LimitBandRoundingPolicy.NEAREST_HALF_UP
        )
        payload = bse_after.to_dict()
        assert payload['rounding_policy'] == 'toward_reference'
        assert json.loads(json.dumps(payload, sort_keys=True)) == payload

    def test_post_freeze_ordinary_fund_remains_10pct_with_disclosed_risk(self):
        """Do not reject known 10% funds when no reliable list_date exists."""

        rule = LimitBandService().get_rule('510300.SH', '20260808')

        assert rule.limit_pct == pytest.approx(0.10)
        assert rule.rule_id == 'ordinary-exchange-fund-10pct'
        assert (
            rule.to_dict()['fund_snapshot_freeze_date']
            == LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE
        )
        assert 'unknown-funds-use-ordinary-10pct' in LIMIT_BAND_SNAPSHOT_COVERAGE

    def test_rounding_policy_requires_the_explicit_enum(self):
        rule = LimitBandService().get_rule('430047.BJ', '20211115')

        with pytest.raises(TypeError, match='rounding_policy'):
            replace(rule, rounding_policy='toward_reference')

    @pytest.mark.parametrize(
        'symbol',
        ['508000.SH', '113016.SH', '204001.SH', 'NOT-A-CODE', 'IF2409.CFFEX'],
    )
    def test_cash_limit_service_rejects_unsupported_instruments(self, symbol):
        with pytest.raises(UnsupportedInstrumentError):
            LimitBandService().get_rule(symbol, '20260721')


class TestLimitPctOverrides:
    """用户覆盖表优先于内置规则"""

    def test_override_by_code(self):
        b = _broker(limit_pct_overrides={'510300': 0.20})
        assert b._get_limit_pct('510300.SH', '20260721') == pytest.approx(0.20)

    def test_override_by_full_symbol(self):
        b = _broker(limit_pct_overrides={'159781.SZ': 0.20})
        assert b._get_limit_pct('159781.SZ', '20260721') == pytest.approx(0.20)

    def test_override_wins_over_builtin(self):
        # 显式压回 10%（如需模拟旧口径）
        b = _broker(limit_pct_overrides={'588000': 0.10})
        assert b._get_limit_pct('588000.SH', '20260721') == pytest.approx(0.10)

    def test_no_override_falls_through(self):
        b = _broker(limit_pct_overrides={'588000': 0.10})
        assert b._get_limit_pct('588080.SH', '20260721') == pytest.approx(0.20)

    @pytest.mark.parametrize(
        'symbol', ['123456.XX', '113016.SH', '204001.SH', '508000.SH']
    )
    def test_override_cannot_make_unsupported_symbol_tradeable(self, symbol):
        b = _broker(limit_pct_overrides={symbol[:6]: 0.10})
        before = (b.account.cash, b.account.frozen_cash, len(b.account.orders))

        with pytest.raises(UnsupportedInstrumentError):
            b.buy_at_market(symbol, shares=100, current_price=10.0)

        assert (b.account.cash, b.account.frozen_cash, len(b.account.orders)) == before


def test_integer_tick_rounding_at_half_tick_boundaries():
    b = _broker()
    b.set_date('20240103')

    # Decimal ROUND_HALF_UP: 10.05 * 1.10 = 11.055 -> 11.06.
    assert b._limit_up_price('000001.SZ', 10.05) == pytest.approx(11.06)
    # Fund tick is 0.001: 1.005 * 1.10 = 1.1055 -> 1.106.
    assert b._limit_up_price('510300.SH', 1.005) == pytest.approx(1.106)


def test_bse_band_rounds_each_edge_toward_pre_close():
    b = _broker()
    b.set_date('20250102')

    # 3.35 * 1.30 = 4.355: BSE uses the inward 4.35 tick, not 4.36.
    assert b._limit_up_price('920002.BJ', 3.35) == pytest.approx(4.35)
    # The lower edge is rounded upward toward the same reference price.
    assert b._limit_down_price('920002.BJ', 3.35) == pytest.approx(2.35)


def test_bse_override_keeps_venue_rounding_policy():
    b = _broker(limit_pct_overrides={'920002.BJ': 0.10})
    b.set_date('20250102')

    # 3.35 * 1.10 = 3.685 is still rounded inward under the BSE policy.
    assert b._limit_up_price('920002.BJ', 3.35) == pytest.approx(3.68)


class TestEnginePlumbing:
    """引擎层参数透传"""

    def test_backtest_engine_passes_overrides(self):
        from diepi.backtest.engine.backtest_engine import BacktestEngine
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240102',
            freq='daily',
            limit_pct_overrides={'000001': 0.05},
        )
        engine._init_engine()
        assert engine._context._broker._get_limit_pct('000001.SZ', '20240102') == pytest.approx(0.05)
        assert engine._context._broker.rulebook is engine.rulebook

    @pytest.mark.parametrize(
        'symbol',
        ['508000.SH', '113016.SH', '204001.SH', 'NOT-A-CODE', 'IF2409.CFFEX'],
    )
    def test_single_cash_engine_rejects_unsupported_symbol_at_init(self, symbol):
        from diepi.backtest.engine.backtest_engine import BacktestEngine

        engine = BacktestEngine(
            symbol=symbol,
            start_date='20240102',
            end_date='20240102',
        )
        with pytest.raises(UnsupportedInstrumentError):
            engine._init_engine()

    def test_portfolio_engine_passes_overrides(self):
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        engine = PortfolioEngine(
            start_date='20240102',
            end_date='20240102',
            limit_pct_overrides={'000001': 0.05},
        )
        assert engine.limit_pct_overrides == {'000001': 0.05}

    def test_custom_rulebook_is_shared_by_broker_and_session_calendar(self):
        from diepi.backtest.engine.backtest_engine import BacktestEngine

        book = RuleBook(metadata={
            '000300': {'kind': 'etf', 'venue': 'SZ'},
        })
        engine = BacktestEngine(
            symbol='000300',
            start_date='20240102',
            end_date='20240102',
            rulebook=book,
        )
        engine._init_engine()

        assert engine._context._broker.rulebook is book
        assert engine.session_calendar.get_rule(
            '000300', '20240102').instrument_kind.value == 'etf'
        assert engine._context._broker._price_decimals('000300') == 3
