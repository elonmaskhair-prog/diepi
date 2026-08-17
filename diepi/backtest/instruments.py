# -*- coding: utf-8 -*-
"""证券品种判定（全仓库唯一真源）

历史P0教训：仓库曾同时存在三份互不一致的基金前缀名单
（数据路由 9 前缀 / 涨跌停 tick 8 前缀 / 印花税 auto 宽口径），
数据路由名单过时导致本地 51.8% 的基金文件（560-563/589/517/501/508/16x
等新段）被引擎判为"无数据"。本模块统一为按码段的宽判定：

- 沪市场内基金：5xxxxx 全段（510-518 老段、517 互联互通、520/526/530/551
  持有期类、560-563 新发主流段、588/589 科创板 ETF、501/502 LOF、
  506 科创主题、508 REITs 等——上交所基金代码段即 5 开头）
- 深市场内基金：15xxxx（ETF/分级）、16xxxx（LOF）、18xxxx（REITs/封基）

新段基金上市不再需要改代码。股票/基金二义性风险：沪市股票 6/68 开头、
深市股票 00/30 开头、北交所 43/83/87/92 开头，与上述码段无交集。
"""


def fund_code(symbol: str) -> str:
    """提取 6 位代码（容忍带交易所后缀）"""
    return symbol.split('.')[0][:6]


def is_exchange_fund(symbol: str) -> bool:
    """是否场内基金（ETF/LOF/REITs/封基）——见模块 docstring 的码段依据"""
    code = fund_code(symbol)
    if not code or not code[0].isdigit():
        return False
    if code.startswith('5'):
        return True
    return code.startswith(('15', '16', '18'))
