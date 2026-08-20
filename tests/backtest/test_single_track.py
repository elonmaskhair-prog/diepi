# -*- coding: utf-8 -*-
"""单轨数据支持回归测试

历史坑：只放 daily/ → 0 成交 0 收益 exit 0（守卫盲区）；
daily_raw+adj_factor 无 daily/ → 限价被错除、0 成交无警告。
现在：严格双轨契约要求 daily/ 与 daily_raw/ 同时存在；任何单轨布局均
fail-fast，不能通过隐式镜像伪造另一价格空间。

DATA_ROOT 在 import 时冻结，故经子进程 + 环境变量注入合成数据根。
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd

from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import run_backtest
from diepi.backtest.data.dataset_manifest import identify_parquet_file
from diepi.backtest.ui.worker import load_gui_run
from diepi.demo.generator import (
    DEMO_END_DATE,
    DEMO_START_DATE,
    generate_synthetic_demo,
)

# 这些测试通过 tmp_path 自建完整数据根，不依赖本地真实行情仓库；它们应当
# 在无数据 CI 中真实执行，而不是被 integration 数据守卫跳过。

ROOT = Path(__file__).resolve().parents[2]

TRADE_DAYS = ['20240102', '20240103', '20240104', '20240105', '20240108']

STRAT = """
def on_before_market_open(ctx):
    pos = ctx.get_position('000001.SZ')
    if ctx.current_date == '20240102':
        ctx.buy_at_open('000001.SZ', percent=0.9)
    elif ctx.current_date == '20240108' and pos and pos.available_shares > 0:
        ctx.sell_at_open('000001.SZ', percent=1.0)
    return ['000001.SZ']
"""


def _write_parquet(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def _build_root(tmp_path: Path, tracks=('daily', 'daily_raw'), split=False,
                with_adj=False) -> Path:
    root = tmp_path / 'data_root'
    cal = []
    previous_open = '20231229'
    for value in pd.date_range('20240102', '20240108', freq='D'):
        compact = value.strftime('%Y%m%d')
        is_open = int(compact in TRADE_DAYS)
        cal.append({
            'exchange': 'SSE',
            'cal_date': compact,
            'is_open': is_open,
            'pretrade_date': previous_open,
        })
        if is_open:
            previous_open = compact
    _write_parquet(root / 'parquet/metadata/common/trade_cal.parquet', cal)

    raw_prices = [10.0, 10.2, 10.4, 10.6, 10.8]
    factors = [1.0] * 5
    if split:  # 20240105 起 2:1 拆分（raw 减半、因子翻倍）
        raw_prices = [10.0, 10.2, 10.4, 5.3, 5.4]
        factors = [1.0, 1.0, 1.0, 2.0, 2.0]

    def rows(prices):
        out = []
        for i, d in enumerate(TRADE_DAYS):
            p = prices[i]
            out.append({'ts_code': '000001.SZ', 'trade_date': d,
                        'open': p, 'high': p * 1.02, 'low': p * 0.98,
                        'close': p * 1.01,
                        'pre_close': prices[i - 1] * 1.01 if i else p,
                        'vol': 1_000_000.0, 'amount': p * 1_000_000.0})
        return out

    hfq_prices = [raw_prices[i] * factors[i] for i in range(5)]  # 锚定首日
    for track in tracks:
        prices = hfq_prices if track == 'daily' else raw_prices
        _write_parquet(root / f'parquet/timeseries/{track}/000001.SZ.parquet',
                       rows(prices))
    if with_adj:
        _write_parquet(root / 'parquet/timeseries/adj_factor/000001.SZ.parquet',
                       [{'ts_code': '000001.SZ', 'trade_date': d,
                         'adj_factor': factors[i]}
                        for i, d in enumerate(TRADE_DAYS)])
    return root


def _run(tmp_path: Path, root: Path):
    strat = tmp_path / 'strat.py'
    strat.write_text(STRAT, encoding='utf-8')
    env = {**os.environ, 'DATA_ROOT': str(root)}
    r = subprocess.run(
        [sys.executable, '-m', 'diepi', str(strat),
         '--symbols', '000001.SZ', '--start', '20240102', '--end', '20240108',
         '--cash', '100000', '--output-dir', str(tmp_path / 'out'),
         '--daily-open-cap-yuan', '1000000000000',
         '--daily-close-cap-yuan', '1000000000000',
         '--name', 'run', '-q'],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
        encoding='utf-8', errors='replace', timeout=300)
    metrics = json.loads(r.stdout)['metrics'] if r.returncode == 0 else None
    return r, metrics


class TestSingleTrackStrictContract:

    def test_only_daily_fails_strict_pair_contract(self, tmp_path):
        """只放 daily/：此前 0 成交 exit 0；现在缺撮合腿即 fail-fast。"""
        root = _build_root(tmp_path, tracks=('daily',))
        r, m = _run(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DataContractError' in r.stderr
        assert 'MISSING_DATASET' in r.stderr

    def test_only_raw_fails_strict_pair_contract(self, tmp_path):
        root = _build_root(tmp_path, tracks=('daily_raw',))
        r, m = _run(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DataContractError' in r.stderr
        assert 'MISSING_DATASET' in r.stderr

    def test_raw_plus_adj_cannot_replace_missing_strategy_lane(self, tmp_path):
        """raw+因子不能替代缺失的策略价格腿。"""
        root = _build_root(tmp_path, tracks=('daily_raw',), split=True,
                           with_adj=True)
        r, m = _run(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DataContractError' in r.stderr
        assert 'MISSING_DATASET' in r.stderr

    def test_dual_track_split_reference(self, tmp_path):
        """双轨+因子是拆分场景唯一受支持的严格参照路径。"""
        root = _build_root(tmp_path, tracks=('daily', 'daily_raw'), split=True,
                           with_adj=True)
        r, m = _run(tmp_path, root)
        assert r.returncode == 0, r.stderr[-400:]
        assert m['trade_count'] == 2
        assert m['total_return'] > 0

    def test_dual_track_diverged_without_adj_fails_fast(self, tmp_path):
        """双轨价格实质不同但缺因子：禁止 ratio=1 继续运行。"""
        root = _build_root(tmp_path, tracks=('daily', 'daily_raw'), split=True,
                           with_adj=False)
        r, m = _run(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DataContractError' in r.stderr
        assert 'MISSING_ADJ_FACTOR' in r.stderr


class TestLazyLoadSingleTrack:
    """懒加载的池外标的必须服从与预加载标的相同的严格双轨契约。"""

    STRAT_TWO = """
def on_before_market_open(ctx):
    if ctx.current_date == '20240102':
        ctx.buy_at_open('000001.SZ', percent=0.45)
        ctx.buy_at_open('000002.SZ', percent=0.45)   # 池外，懒加载
    elif ctx.current_date == '20240108':
        for s in ('000001.SZ', '000002.SZ'):
            pos = ctx.get_position(s)
            if pos and pos.available_shares > 0:
                ctx.sell_at_open(s, percent=1.0)
    # Deliberately do not return B: the engine must still freeze/read it from
    # the pending order or holding scope rather than trusting the selection.
    return ['000001.SZ']
"""

    def _build_two_symbol_root(self, tmp_path, b_tracks, *, with_b_adj=False):
        """000001.SZ 双轨；000002.SZ 按 b_tracks 指定"""
        root = _build_root(tmp_path, with_adj=True)  # 000001.SZ strict dual
        raw_prices = [10.0, 10.2, 10.4, 10.6, 10.8]

        def rows(prices):
            out = []
            for i, d in enumerate(TRADE_DAYS):
                p = prices[i]
                out.append({'ts_code': '000002.SZ', 'trade_date': d,
                            'open': p, 'high': p * 1.02, 'low': p * 0.98,
                            'close': p * 1.01,
                            'pre_close': prices[i - 1] * 1.01 if i else p,
                            'vol': 1_000_000.0, 'amount': p * 1_000_000.0})
            return out

        for track in b_tracks:
            _write_parquet(
                root / f'parquet/timeseries/{track}/000002.SZ.parquet',
                rows(raw_prices))
        if with_b_adj:
            _write_parquet(
                root / 'parquet/timeseries/adj_factor/000002.SZ.parquet',
                [
                    {
                        'ts_code': '000002.SZ',
                        'trade_date': day,
                        'adj_factor': 1.0,
                    }
                    for day in TRADE_DAYS
                ],
            )
        return root

    def _run_pool_a_only(self, tmp_path, root):
        strat = tmp_path / 'strat.py'
        strat.write_text(self.STRAT_TWO, encoding='utf-8')
        env = {**os.environ, 'DATA_ROOT': str(root)}
        r = subprocess.run(
            [sys.executable, '-m', 'diepi', str(strat),
             '--symbols', '000001.SZ',  # 池只含 A，B 走懒加载
             '--start', '20240102', '--end', '20240108',
             '--cash', '1000000', '--output-dir', str(tmp_path / 'out'),
             '--daily-open-cap-yuan', '1000000000000',
             '--daily-close-cap-yuan', '1000000000000',
             '--name', 'run', '-q'],
            capture_output=True, text=True, env=env, cwd=str(ROOT),
            encoding='utf-8', errors='replace', timeout=300)
        metrics = json.loads(r.stdout)['metrics'] if r.returncode == 0 else None
        return r, metrics

    def test_lazyload_hfq_only_symbol_fails_strict_pair_contract(self, tmp_path):
        """池外标的仅有 daily/ 时不得通过隐式镜像继续运行。"""
        root = self._build_two_symbol_root(tmp_path, b_tracks=('daily',))
        r, m = self._run_pool_a_only(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DYNAMIC_MARKET_DATA_SOURCE_UNVERIFIED' in r.stderr
        assert 'daily:raw' in r.stderr

    def test_lazyload_raw_only_symbol_fails_strict_pair_contract(self, tmp_path):
        """池外标的仅有 daily_raw/ 时不得伪造策略价格腿。"""
        root = self._build_two_symbol_root(tmp_path, b_tracks=('daily_raw',))
        r, m = self._run_pool_a_only(tmp_path, root)
        assert r.returncode == 1
        assert m is None
        assert 'DYNAMIC_MARKET_DATA_SOURCE_UNVERIFIED' in r.stderr
        assert 'daily:hfq' in r.stderr

    def test_lazyload_complete_dynamic_symbol_is_bound_to_artifact_and_gui(
        self, tmp_path
    ):
        root = self._build_two_symbol_root(
            tmp_path,
            b_tracks=('daily', 'daily_raw'),
            with_b_adj=True,
        )
        r, metrics = self._run_pool_a_only(tmp_path, root)

        assert r.returncode == 0, r.stderr[-800:]
        assert metrics['trade_count'] == 4
        loaded = ArtifactStore.load(tmp_path / 'out' / 'run')
        assert loaded.config['parameters']['pool_symbols'] == ['000001.SZ']
        assert loaded.config['realized_symbols'] == [
            '000001.SZ',
            '000002.SZ',
        ]
        market_paths = {
            source.logical_path
            for source in loaded.provenance.sources
            if source.kind == 'market_data_file'
        }
        assert {
            'parquet/timeseries/daily/000002.SZ.parquet',
            'parquet/timeseries/daily_raw/000002.SZ.parquet',
            'parquet/timeseries/adj_factor/000002.SZ.parquet',
        }.issubset(market_paths)

        gui_loaded = load_gui_run(tmp_path / 'out' / 'run')
        assert gui_loaded.artifact_verified is True
        assert gui_loaded.config['realized_symbols'] == [
            '000001.SZ',
            '000002.SZ',
        ]


def test_cli_all_market_missing_member_is_verified_partial_artifact(tmp_path):
    demo = generate_synthetic_demo(tmp_path / 'demo')
    basic_path = (
        demo.data_root / 'parquet' / 'metadata' / 'stock' / 'basic.parquet'
    )
    basic = pd.read_parquet(basic_path)
    missing = basic.iloc[[0]].copy()
    missing['ts_code'] = '000002.SZ'
    pd.concat([basic, missing], ignore_index=True).to_parquet(
        basic_path, index=False
    )
    basic_identity = identify_parquet_file(
        demo.data_root, 'parquet/metadata/stock/basic.parquet'
    )
    manifest = replace(
        demo.manifest,
        files=tuple(
            basic_identity if item.path == basic_identity.path else item
            for item in demo.manifest.files
        ),
    )
    demo.manifest_file.write_text(
        manifest.to_json(), encoding='utf-8', newline='\n'
    )

    output = run_backtest(
        str(demo.strategy_file),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
        initial_cash=1_000_000,
        output_dir=tmp_path / 'results',
        run_name='all-market-missing',
        pool_symbols=None,
        daily_open_cap_yuan=1_000_000_000,
        daily_close_cap_yuan=1_000_000_000,
        verbose=False,
        data_root=demo.data_root,
    )

    loaded = ArtifactStore.load(output['artifact_dir'])
    contract = loaded.outcome.result_contract
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is False
    assert contract.status.value == 'PARTIAL'
    assert contract.reason.code == 'UNIVERSE_MARKET_DATA_INCOMPLETE'
    assert loaded.config['realized_symbols'] == [
        '000001.SZ',
        '000002.SZ',
    ]
