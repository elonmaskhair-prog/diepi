from pathlib import Path

import pandas as pd

from diepi.artifacts import SourceFingerprint
from diepi.backtest.data import builtin_calendar_identity
from diepi.backtest.data import source_evidence as source_evidence_module
from diepi.backtest.data.source_evidence import (
    DynamicMarketDataFingerprintTracker,
    RUNTIME_METADATA_SOURCE_KIND,
    collect_market_data_fingerprints,
    require_complete_direct_sources,
    trade_calendar_fingerprint,
)
from diepi.demo.generator import generate_synthetic_demo


def _source(path: str) -> SourceFingerprint:
    return SourceFingerprint.from_bytes(
        kind="market_data_file",
        logical_path=path,
        payload=path.encode("utf-8"),
    )


def test_dual_rankable_source_evidence_requires_both_lanes_and_factor():
    sources = (
        _source("parquet/timeseries/etf_daily/510300.SH.parquet"),
        _source("parquet/timeseries/etf_daily_raw/510300.SH.parquet"),
        _source("parquet/timeseries/etf_adj_factor/510300.SH.parquet"),
    )

    assert require_complete_direct_sources("510300.SH", "dual", sources) == ()

    try:
        require_complete_direct_sources("510300.SH", "dual", sources[:1] + sources[2:])
    except ValueError as exc:
        assert "daily:raw" in str(exc)
        assert "fallback routes cannot be ranked" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("dual evidence without a raw lane must fail closed")


def test_dual_rankable_source_evidence_requires_adjustment_factor():
    sources = (
        _source("parquet/timeseries/etf_daily/510300.SH.parquet"),
        _source("parquet/timeseries/etf_daily_raw/510300.SH.parquet"),
    )

    try:
        require_complete_direct_sources("510300.SH", "dual", sources)
    except ValueError as exc:
        assert "adj_factor" in str(exc)
        assert "fallback routes cannot be ranked" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("dual evidence without an adjustment factor must fail closed")


def test_minute_rankable_source_evidence_requires_a_direct_minute_partition():
    daily = _source("parquet/timeseries/etf_daily_raw/510300.SH.parquet")

    try:
        require_complete_direct_sources(
            "510300.SH", "raw", (daily,), frequency="1min"
        )
    except ValueError as exc:
        assert "minute:raw" in str(exc)
        assert "fallback routes cannot be ranked" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("minute evidence without a direct partition must fail closed")

    minute = _source(
        "parquet/timeseries/etf_minute_raw/510300.SH/2026.parquet"
    )
    assert (
        require_complete_direct_sources(
            "510300.SH", "raw", (daily, minute), frequency="1min"
        )
        == ()
    )


def test_source_evidence_rejects_a_wrong_provenance_kind():
    source = SourceFingerprint.from_bytes(
        kind="combo_input",
        logical_path="parquet/timeseries/daily_raw/600000.SH.parquet",
        payload=b"not market evidence",
    )

    try:
        require_complete_direct_sources("600000.SH", "raw", (source,))
    except ValueError as exc:
        assert "only market_data_file" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("a non-market provenance kind must fail closed")


def test_source_evidence_rejects_an_unknown_frequency():
    source = _source("parquet/timeseries/daily_raw/600000.SH.parquet")

    try:
        require_complete_direct_sources(
            "600000.SH", "raw", (source,), frequency="daily-ish"
        )
    except ValueError as exc:
        assert "supported minute frequency" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("an unknown evidence frequency must fail closed")


def test_minute_evidence_requires_a_year_parquet_member():
    daily = _source("parquet/timeseries/daily_raw/600000.SH.parquet")
    impostor = _source(
        "parquet/timeseries/minute_raw/600000.SH/not-parquet.txt"
    )

    try:
        require_complete_direct_sources(
            "600000.SH", "raw", (daily, impostor), frequency="1min"
        )
    except ValueError as exc:
        assert "minute:raw" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("a non-year, non-Parquet minute member must fail closed")


def test_shared_stock_basic_is_fingerprinted_once_for_a_wide_stock_scope(
    tmp_path, monkeypatch,
):
    demo = generate_synthetic_demo(tmp_path / "demo")
    real_fingerprint = (
        source_evidence_module._fingerprint_readable_nonempty_parquet
    )
    stock_basic_calls = []

    def counted_fingerprint(path, *, root, kind="market_data_file"):
        if Path(path) == (
            demo.data_root / "parquet" / "metadata" / "stock" / "basic.parquet"
        ):
            stock_basic_calls.append(Path(path))
        return real_fingerprint(path, root=root, kind=kind)

    monkeypatch.setattr(
        source_evidence_module,
        "_fingerprint_readable_nonempty_parquet",
        counted_fingerprint,
    )

    sources = collect_market_data_fingerprints(
        demo.data_root,
        symbols=("000001.SZ", "000002.SZ", "000003.SZ"),
        price_mode="raw",
    )

    assert len(stock_basic_calls) == 1
    assert sum(
        source.kind == RUNTIME_METADATA_SOURCE_KIND
        and source.logical_path == "parquet/metadata/stock/basic.parquet"
        for source in sources
    ) == 1


def test_all_market_scope_freezes_shared_security_master(tmp_path):
    demo = generate_synthetic_demo(tmp_path / "demo")

    before = collect_market_data_fingerprints(
        demo.data_root,
        symbols=None,
        price_mode="raw",
    )
    stock_basic = (
        demo.data_root / "parquet" / "metadata" / "stock" / "basic.parquet"
    )
    changed = pd.read_parquet(stock_basic)
    changed.loc[:, "list_date"] = "20200101"
    changed.to_parquet(stock_basic, index=False)
    after = collect_market_data_fingerprints(
        demo.data_root,
        symbols=None,
        price_mode="raw",
    )

    assert any(
        source.kind == RUNTIME_METADATA_SOURCE_KIND
        and source.logical_path == "parquet/metadata/stock/basic.parquet"
        for source in before
    )
    assert before != after


def test_etf_scope_binds_generic_stock_info_metadata(tmp_path):
    root = tmp_path / "market-data"
    stock_basic = root / "parquet" / "metadata" / "stock" / "basic.parquet"
    stock_basic.parent.mkdir(parents=True)
    pd.DataFrame(
        {"ts_code": ["000001.SZ"], "list_date": ["19910403"]}
    ).to_parquet(stock_basic, index=False)
    etf_raw = (
        root
        / "parquet"
        / "timeseries"
        / "etf_daily_raw"
        / "510300.SH.parquet"
    )
    etf_raw.parent.mkdir(parents=True)
    pd.DataFrame(
        {"trade_date": ["20260105"], "close": [4.0]}
    ).to_parquet(etf_raw, index=False)

    before = collect_market_data_fingerprints(
        root, symbols=("510300.SH",), price_mode="raw"
    )
    pd.DataFrame(
        {"ts_code": ["000001.SZ"], "list_date": ["20200101"]}
    ).to_parquet(stock_basic, index=False)
    after = collect_market_data_fingerprints(
        root, symbols=("510300.SH",), price_mode="raw"
    )

    assert any(
        source.kind == RUNTIME_METADATA_SOURCE_KIND
        and source.logical_path == "parquet/metadata/stock/basic.parquet"
        for source in before
    )
    assert before != after


def test_dynamic_tracker_hashes_shared_metadata_once_per_generation(
    tmp_path, monkeypatch,
):
    demo = generate_synthetic_demo(tmp_path / "demo")
    real_fingerprint = (
        source_evidence_module._fingerprint_readable_nonempty_parquet
    )
    stock_basic_calls = []

    def counted_fingerprint(path, *, root, kind="market_data_file"):
        if Path(path) == (
            demo.data_root / "parquet" / "metadata" / "stock" / "basic.parquet"
        ):
            stock_basic_calls.append(Path(path))
        return real_fingerprint(path, root=root, kind=kind)

    monkeypatch.setattr(
        source_evidence_module,
        "_fingerprint_readable_nonempty_parquet",
        counted_fingerprint,
    )
    tracker = DynamicMarketDataFingerprintTracker(
        demo.data_root,
        configured_symbols=(),
        price_mode="raw",
        allow_incomplete_sources=True,
    )

    tracker.observe(("000001.SZ", "000002.SZ", "000003.SZ"))
    assert len(stock_basic_calls) == 1

    tracker.verify_stable()
    assert len(stock_basic_calls) == 2


def test_auxiliary_only_snapshot_does_not_cover_a_later_price_read(tmp_path):
    demo = generate_synthetic_demo(tmp_path / "aux-only")
    outsider = "600000.SH"
    auxiliary = (
        demo.data_root
        / "parquet"
        / "timeseries"
        / "daily_basic"
        / f"{outsider}.parquet"
    )
    auxiliary.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_code": [outsider],
            "trade_date": ["20240102"],
            "turnover_rate": [1.0],
        }
    ).to_parquet(auxiliary, index=False)
    tracker = DynamicMarketDataFingerprintTracker(
        demo.data_root,
        configured_symbols=demo.manifest.symbols,
        price_mode="raw",
    )

    tracker.observe(
        (outsider,),
        start_date="20240102",
        end_date="20240102",
        require_direct_prices=False,
    )
    assert any(
        source.logical_path
        == f"parquet/timeseries/daily_basic/{outsider}.parquet"
        for source in tracker.fingerprints
    )

    try:
        tracker.observe(
            (outsider,),
            start_date="20240102",
            end_date="20240102",
            require_direct_prices=True,
        )
    except ValueError as exc:
        assert "DYNAMIC_MARKET_DATA_SOURCE_UNVERIFIED" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("an auxiliary snapshot must not cover missing prices")

    changed = pd.read_parquet(auxiliary)
    changed.loc[:, "turnover_rate"] = 2.0
    changed.to_parquet(auxiliary, index=False)
    try:
        tracker.verify_stable()
    except OSError as exc:
        assert "MARKET_DATA_CHANGED_DURING_RUN" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("changed auxiliary evidence must fail closed")


def test_bundled_trade_calendar_has_stable_synthetic_provenance():
    source = trade_calendar_fingerprint(builtin_calendar_identity())

    assert source.kind == "trade_calendar"
    assert source.logical_path == "calendar/bundled.json"
    assert source.byte_length > 0
