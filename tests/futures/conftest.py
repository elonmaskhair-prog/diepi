# -*- coding: utf-8 -*-
"""futures 数据集成测试的本地数据守卫。"""

import os

import pytest


def _has_futures_data() -> bool:
    try:
        from diepi.backtest.config import PARQUET_ROOT
        return os.path.isdir(os.path.join(PARQUET_ROOT, 'futures_daily'))
    except Exception:
        return False


_HAS_DATA = _has_futures_data()


@pytest.fixture(autouse=True)
def _skip_integration_without_futures_data(request):
    """Skip explicit futures integrations without touching other suites."""
    if request.node.get_closest_marker('integration') and not _HAS_DATA:
        pytest.skip("需要本地期货数据 (futures_daily 缺失)，数据准备见 README")
