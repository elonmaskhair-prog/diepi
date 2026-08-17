# -*- coding: utf-8 -*-
"""新股上市初期交易规则

A股新股上市初期无涨跌幅限制：
- 科创板(688/689)、创业板注册制后(20200824起)、主板全面注册制后(20230217起)：前5个交易日
- 北交所：仅上市首日
- 注册制前的主板/创业板：首日有 44%/64% 特殊带宽，此处近似为首日免校验
  （成交价来自真实bar，失真仅剩封板判定）
- 场内基金（ETF/LOF/REITs）上市首日即有常规涨跌幅，不豁免

引擎在初始化时用本模块计算每标的的"涨跌停校验豁免日历"，注入 Broker。
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, Set

from ..data.data_provider import ParameterValidator

logger = logging.getLogger(__name__)


def _exempt_days_for(code: str, list_date: str) -> int:
    """按板块与上市日期返回免涨跌停校验的交易日数"""
    if code.startswith(('688', '689')):
        return 5
    if code.startswith(('300', '301', '302')):
        return 5 if list_date >= '20200824' else 1
    if code.startswith(('92', '43', '83', '87')):
        return 1  # 北交所仅首日无限制
    return 5 if list_date >= '20230217' else 1  # 主板全面注册制


def compute_limit_exempt_dates(data_provider, symbols: Iterable[str],
                               backtest_start: str = None) -> Dict[str, Set[str]]:
    """计算 {symbol: {豁免日YYYYMMDD, ...}}。

    只对回测窗口内上市的股票有意义：list_date 早于 backtest_start 的标的
    其上市初期不在回测窗口内，直接跳过（也让全市场池的计算量可控）。
    """
    result: Dict[str, Set[str]] = {}
    if backtest_start is not None:
        backtest_start = ParameterValidator.normalize_date(backtest_start)
    for symbol in symbols:
        code = symbol[:6]
        # 场内基金不豁免
        if code.startswith('5') or code.startswith(('15', '16', '18')):
            continue
        try:
            info = data_provider.get_stock_info(symbol)
        except Exception:
            continue
        list_date = info.get('list_date') if hasattr(info, 'get') else None
        list_date = (
            ParameterValidator.normalize_date(list_date)
            if list_date is not None else ''
        )
        if len(list_date) != 8 or not list_date.isdigit():
            continue
        n_days = _exempt_days_for(code, list_date)
        try:
            window_end = (datetime.strptime(list_date, '%Y%m%d')
                          + timedelta(days=40)).strftime('%Y%m%d')
            # 只有整个候选窗口都早于回测起点时才可跳过。旧判断只要
            # list_date < backtest_start 就跳过，导致从上市第2--5日开始的
            # 回测错误启用常规涨跌停。
            if backtest_start and window_end < backtest_start:
                continue
            df = data_provider.get_daily(
                symbol, start=list_date, end=window_end, price_mode='execution')
        except Exception as e:
            logger.debug(f"limit exempt calc failed for {symbol}: {e}")
            continue
        if df is None or df.empty:
            continue
        # get_daily 返回的 DataFrame 以 trade_date 为索引（非列）。
        # 历史P1：此处曾检查 'trade_date' in df.columns → 恒 False →
        # 整个新股豁免特性对真实数据静默失效（零测试覆盖没拦住）
        if 'trade_date' in df.columns:
            date_series = df['trade_date'].astype(str)
        else:
            date_series = df.index.astype(str)
        dates = [str(d)[:8] for d in list(date_series)[:n_days]]
        if backtest_start:
            dates = [date for date in dates if date >= backtest_start]
        if dates:
            result[symbol] = set(dates)
    return result
