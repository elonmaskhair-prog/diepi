"""Product specifications for CFFEX stock index futures."""
from __future__ import annotations

PRODUCT_SPECS: dict[str, dict] = {
    "IC": {"multiplier": 200, "exchange": "CFX", "index": "000905.SH", "margin_rate": 0.14, "name": "ZZ500"},
    "IM": {"multiplier": 200, "exchange": "CFX", "index": "000852.SH", "margin_rate": 0.14, "name": "ZZ1000"},
    "IF": {"multiplier": 300, "exchange": "CFX", "index": "000300.SH", "margin_rate": 0.12, "name": "HS300"},
    "IH": {"multiplier": 300, "exchange": "CFX", "index": "000016.SH", "margin_rate": 0.12, "name": "SZ50"},
}


def get_spec(product: str) -> dict:
    if product not in PRODUCT_SPECS:
        raise KeyError(f"Unknown product: {product}. Must be one of {list(PRODUCT_SPECS)}")
    return PRODUCT_SPECS[product]


def get_multiplier(product: str) -> int:
    return get_spec(product)["multiplier"]


def get_margin_rate(product: str) -> float:
    return get_spec(product)["margin_rate"]
