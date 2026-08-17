"""Pure unit coverage for the instrument rule book."""

from dataclasses import replace

import pytest

from diepi.backtest.rulebook import (
    ExecutionEngine,
    InstrumentKind,
    RuleBook,
    UnsupportedInstrumentError,
    Venue,
)


@pytest.mark.parametrize(
    "symbol, expected_kind, expected_venue",
    [
        ("600000.SH", InstrumentKind.A_SHARE, Venue.SSE),
        ("000001.SZ", InstrumentKind.A_SHARE, Venue.SZSE),
        ("688001.SH", InstrumentKind.A_SHARE, Venue.SSE),
        ("830001.BJ", InstrumentKind.A_SHARE, Venue.BSE),
        ("510300.SH", InstrumentKind.ETF, Venue.SSE),
        ("159915.SZ", InstrumentKind.ETF, Venue.SZSE),
        ("501018.SH", InstrumentKind.LOF, Venue.SSE),
        ("160119.SZ", InstrumentKind.LOF, Venue.SZSE),
        ("508000.SH", InstrumentKind.REIT, Venue.SSE),
        ("180101.SZ", InstrumentKind.REIT, Venue.SZSE),
        ("000300.SH", InstrumentKind.INDEX, Venue.SSE),
        ("399001.SZ", InstrumentKind.INDEX, Venue.SZSE),
        ("IF2409.CFX", InstrumentKind.FUTURE, Venue.CFFEX),
    ],
)
def test_code_range_fallbacks(symbol, expected_kind, expected_venue):
    rule = RuleBook().get_rule(symbol)
    assert rule.kind is expected_kind
    assert rule.venue is expected_venue


def test_classification_priority_is_metadata_then_override_then_fallback():
    book = RuleBook(
        metadata={"510300.SH": {"kind": "a_share"}},
        kind_overrides={
            "510300.SH": InstrumentKind.LOF,
            "159915.SZ": InstrumentKind.LOF,
        },
    )

    assert book.classify("510300.SH") is InstrumentKind.A_SHARE
    assert book.classify("159915.SZ") is InstrumentKind.LOF
    assert book.classify("512000.SH") is InstrumentKind.ETF
    # One-shot metadata is also authoritative over configured overrides.
    assert (
        book.classify("159915.SZ", metadata={"kind": InstrumentKind.REIT})
        is InstrumentKind.REIT
    )


def test_cash_rules_expose_logical_routes_and_execution_fields():
    book = RuleBook()

    stock = book.require_supported("600000.SH", ExecutionEngine.CASH)
    assert (stock.data_route, stock.price_decimals) == ("equity", 2)
    assert (stock.buy_min_qty, stock.buy_step) == (100, 100)
    assert stock.settlement == "T+1"
    assert stock.stamp_exempt is False

    star = book.require_supported("688001.SH", "cash")
    assert (star.buy_min_qty, star.buy_step) == (200, 1)

    etf = book.require_supported("510300.SH", ExecutionEngine.CASH)
    lof = book.require_supported("160119.SZ", ExecutionEngine.CASH)
    for rule in (etf, lof):
        assert rule.supported is True
        assert rule.engine is ExecutionEngine.CASH
        assert rule.data_route == "exchange_fund"
        assert rule.price_decimals == 3
        assert (rule.buy_min_qty, rule.buy_step) == (100, 100)
        assert rule.stamp_exempt is True


@pytest.mark.parametrize("symbol", ["511010.SH", "513100.SH", "518880.SH"])
def test_only_unambiguous_etf_code_ranges_default_to_t_plus_zero(symbol):
    rule = RuleBook().require_supported(symbol, ExecutionEngine.CASH)
    assert rule.kind is InstrumentKind.ETF
    assert rule.settlement == "T+0"


def test_ambiguous_shenzhen_etf_defaults_to_t_plus_one_but_metadata_can_override():
    book = RuleBook()
    assert book.get_rule("159915.SZ").settlement == "T+1"
    assert (
        book.get_rule("159915.SZ", metadata={"settlement": "T+0"}).settlement
        == "T+0"
    )


@pytest.mark.parametrize("symbol", ["508000.SH", "180101.SZ", "000300.SH", "NOT-A-CODE"])
def test_non_tradeable_cash_instruments_fail_fast(symbol):
    with pytest.raises(UnsupportedInstrumentError) as caught:
        RuleBook().require_supported(symbol, ExecutionEngine.CASH)

    assert caught.value.rule.supported is False
    assert caught.value.symbol == symbol


@pytest.mark.parametrize(
    "symbol, multiplier, margin_rate",
    [
        ("IF", 300, 0.12),
        ("IH2409", 300, 0.12),
        ("IC2412.CFX", 200, 0.14),
        ("IM2503.CFFEX", 200, 0.14),
    ],
)
def test_supported_index_futures_use_product_specs(symbol, multiplier, margin_rate):
    rule = RuleBook().require_supported(symbol, ExecutionEngine.FUTURES)

    assert rule.kind is InstrumentKind.FUTURE
    assert rule.venue is Venue.CFFEX
    assert rule.engine is ExecutionEngine.FUTURES
    assert rule.data_route == "cffex_futures"
    assert rule.multiplier == multiplier
    assert rule.margin_rate == pytest.approx(margin_rate)
    assert (rule.buy_min_qty, rule.buy_step) == (1, 1)


def test_rule_generation_is_immutable_and_deterministic():
    metadata_a = {
        "600000.SH": {
            "kind": "a_share",
            "effective_from": "20240101",
            "effective_to": "20241231",
            "source_version": "security-master-20240101",
        },
        "510300.SH": {"kind": "etf"},
    }
    metadata_b = dict(reversed(list(metadata_a.items())))

    retained_book = RuleBook(metadata=metadata_a)
    first = retained_book.get_rule("600000.SH")
    second = RuleBook(metadata=metadata_b).get_rule("600000.SH")
    repeated = RuleBook(metadata=metadata_a).get_rule("600000.SH")

    assert first == second == repeated
    assert hash(first) == hash(second)
    assert first.source_version == "security-master-20240101"
    assert first.effective_from == "20240101"
    assert first.effective_to == "20241231"

    # Constructor copies metadata rather than retaining caller-owned dictionaries.
    metadata_a["600000.SH"]["kind"] = "reit"
    assert retained_book.classify("600000.SH") is InstrumentKind.A_SHARE


def test_metadata_can_resolve_an_ambiguous_symbol_and_override_fields():
    rule = RuleBook().get_rule(
        "000300",
        metadata={
            "kind": "etf",
            "venue": "SZ",
            "settlement": "T+0",
            "buy_step": 1,
        },
    )

    assert rule.kind is InstrumentKind.ETF
    assert rule.venue is Venue.SZSE
    assert rule.settlement == "T+0"
    assert rule.buy_step == 1


@pytest.mark.parametrize("symbol", ["", " ", "\t\r\n"])
def test_empty_symbols_are_input_errors_not_unknown_instruments(symbol):
    book = RuleBook()
    with pytest.raises(ValueError, match="non-empty"):
        book.classify(symbol)
    with pytest.raises(ValueError, match="non-empty"):
        book.get_rule(symbol)


@pytest.mark.parametrize("field", ["data_route", "settlement", "source_version"])
def test_required_text_rule_fields_reject_empty_values(field):
    with pytest.raises(ValueError, match=field):
        RuleBook().get_rule("600000.SH", metadata={field: "   "})


@pytest.mark.parametrize(
    ("field", "value", "expected_name"),
    [
        ("kind", "a_share", "InstrumentKind"),
        ("venue", "SSE", "Venue"),
        ("engine", "cash", "ExecutionEngine"),
    ],
)
def test_instrument_rule_enum_fields_require_enum_members(
    field, value, expected_name
):
    rule = RuleBook().get_rule("600000.SH")

    with pytest.raises(TypeError, match=expected_name):
        replace(rule, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("supported", 1),
        ("supported", "false"),
        ("stamp_exempt", 0),
        ("stamp_exempt", "true"),
    ],
)
def test_boolean_rule_fields_require_exact_bool(field, value):
    rule = RuleBook().get_rule("600000.SH")

    with pytest.raises(TypeError, match=field):
        replace(rule, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_decimals", 2.0),
        ("price_decimals", True),
        ("buy_min_qty", 100.0),
        ("buy_min_qty", False),
        ("buy_step", 100.0),
        ("buy_step", True),
    ],
)
def test_integer_rule_fields_reject_bool_and_float(field, value):
    rule = RuleBook().get_rule("600000.SH")

    with pytest.raises(TypeError, match=field):
        replace(rule, **{field: value})


@pytest.mark.parametrize("value", [300.0, True, "300"])
def test_futures_multiplier_requires_a_positive_integer(value):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(TypeError, match="multiplier"):
        replace(rule, multiplier=value)


@pytest.mark.parametrize("value", [0, -1])
def test_futures_multiplier_rejects_nonpositive_integers(value):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(ValueError, match="positive"):
        replace(rule, multiplier=value)


@pytest.mark.parametrize("value", [True, "0.12", 0.12 + 0j])
def test_margin_rate_requires_a_real_number(value):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(TypeError, match="margin_rate"):
        replace(rule, margin_rate=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_margin_rate_must_be_finite(value):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(ValueError, match="finite"):
        replace(rule, margin_rate=value)


@pytest.mark.parametrize("value", [0.0, -0.1, 1.01])
def test_margin_rate_must_stay_in_unit_interval(value):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        replace(rule, margin_rate=value)


@pytest.mark.parametrize("field", ["buy_min_qty", "buy_step"])
def test_supported_rules_require_positive_order_quantities(field):
    rule = RuleBook().get_rule("600000.SH")

    with pytest.raises(ValueError, match="positive"):
        replace(rule, **{field: 0})


@pytest.mark.parametrize(
    "updates",
    [
        {"engine": ExecutionEngine.FUTURES},
        {"venue": Venue.CFFEX},
        {"data_route": "cffex_futures"},
        {"settlement": "T+0"},
        {"multiplier": 300},
        {"margin_rate": 0.12},
    ],
)
def test_supported_a_share_rejects_cross_family_fields(updates):
    rule = RuleBook().get_rule("600000.SH")

    with pytest.raises(ValueError):
        replace(rule, **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"engine": ExecutionEngine.NONE},
        {"venue": Venue.BSE},
        {"data_route": "equity"},
        {"settlement": "N/A"},
        {"multiplier": 200},
    ],
)
def test_supported_exchange_fund_rejects_cross_family_fields(updates):
    rule = RuleBook().get_rule("159915.SZ")

    with pytest.raises(ValueError):
        replace(rule, **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"engine": ExecutionEngine.CASH},
        {"venue": Venue.SSE},
        {"data_route": "equity"},
        {"settlement": "T+1"},
        {"multiplier": None},
        {"margin_rate": None},
    ],
)
def test_supported_future_requires_complete_cffex_rule(updates):
    rule = RuleBook().get_rule("IF2409")

    with pytest.raises(ValueError):
        replace(rule, **updates)


@pytest.mark.parametrize("symbol", ["000300.SH", "508000.SH", "NOT-A-CODE"])
def test_v01_nontradeable_kinds_cannot_be_enabled_by_metadata(symbol):
    with pytest.raises(ValueError):
        RuleBook().get_rule(symbol, metadata={"supported": True})


@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": "a_share", "buy_stepp": 1},
        {"supported": True, "stamp_duty": False},
    ],
)
def test_direct_metadata_rejects_unknown_fields(metadata):
    with pytest.raises(ValueError, match="unknown rule field"):
        RuleBook().get_rule("600000.SH", metadata=metadata)


def test_configured_metadata_rejects_unknown_nested_fields():
    with pytest.raises(ValueError, match="unknown rule field"):
        RuleBook(metadata={"600000.SH": {"kind": "a_share", "typo": 1}})


def test_conflicting_kind_aliases_are_rejected():
    with pytest.raises(ValueError, match="conflicting kind fields"):
        RuleBook().get_rule(
            "600000.SH",
            metadata={"kind": "a_share", "asset_type": "etf"},
        )


def test_one_shot_metadata_rejects_duplicate_normalised_symbol_keys():
    metadata = {
        "600000.sh": {"buy_step": 100},
        " 600000.SH ": {"buy_step": 1},
    }

    with pytest.raises(ValueError, match="duplicate normalised symbol"):
        RuleBook().get_rule("600000.SH", metadata=metadata)


def test_previously_accepted_inconsistent_metadata_now_fails_closed():
    book = RuleBook()

    with pytest.raises(ValueError, match="requires engine=cash"):
        book.get_rule(
            "600000.SH", metadata={"engine": "futures", "supported": True}
        )
    with pytest.raises(TypeError, match="supported"):
        book.get_rule("600000.SH", metadata={"supported": "false"})
    with pytest.raises(ValueError, match="allowed venues"):
        book.get_rule("NOT-A-CODE", metadata={"kind": "a_share"})


@pytest.mark.parametrize("symbol", ["000016", "000300", "000852", "000905"])
def test_bare_core_index_codes_fail_closed_instead_of_routing_to_szse(symbol):
    rule = RuleBook().get_rule(symbol)

    assert rule.kind is InstrumentKind.UNKNOWN
    assert rule.venue is Venue.UNKNOWN
    assert rule.supported is False
    with pytest.raises(UnsupportedInstrumentError):
        RuleBook().require_supported(symbol, ExecutionEngine.CASH)


def test_suffix_disambiguates_core_index_code_from_shenzhen_security():
    index_rule = RuleBook().get_rule("000300.SH")
    stock_rule = RuleBook().get_rule("000300.SZ")

    assert (index_rule.kind, index_rule.venue) == (
        InstrumentKind.INDEX,
        Venue.SSE,
    )
    assert (stock_rule.kind, stock_rule.venue) == (
        InstrumentKind.A_SHARE,
        Venue.SZSE,
    )


@pytest.mark.parametrize("symbol", ["580001.SH", "150001.SZ", "151001.SZ"])
def test_historical_ambiguous_ranges_are_unknown_without_metadata(symbol):
    rule = RuleBook().get_rule(symbol)

    assert rule.kind is InstrumentKind.UNKNOWN
    assert rule.supported is False


def test_explicit_metadata_can_resolve_historical_ambiguous_range():
    rule = RuleBook().get_rule("150001.SZ", metadata={"kind": "lof"})

    assert rule.kind is InstrumentKind.LOF
    assert rule.venue is Venue.SZSE
    assert rule.data_route == "exchange_fund"
    assert rule.supported is True


def test_three_digit_cffex_contract_code_is_not_silently_supported():
    rule = RuleBook().get_rule("IF409")

    assert rule.kind is InstrumentKind.UNKNOWN
    assert rule.supported is False
