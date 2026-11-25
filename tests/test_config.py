from pathlib import Path

import pytest

from map_violation_monitor.config import (
    DEFAULT_EXCLUDE,
    ConfigError,
    Term,
    compact,
    load_config,
    parse_config,
)

ROOT = Path(__file__).resolve().parents[1]


def minimal(**product):
    base = {
        "name": "Rocket Skates",
        "map": 100,
        "currency": "USD",
        "amazon": [{"asin": "B0TESTRS20"}],
    }
    base.update(product)
    return {"products": [base]}


def test_shipped_config_loads():
    config = load_config(ROOT / "config.yml")
    assert len(config.products) == 2
    us, de = config.products
    assert us.currency == "USD" and de.currency == "EUR"
    assert [t.source for t in us.targets] == ["amazon", "google_shopping", "ebay"]
    assert [t.source for t in de.targets] == ["idealo"]
    assert config.calls == 4  # the cost stated in the README


def test_example_config_values(config):
    us = config.products[0]
    assert us.map == 200.0 and us.sku == "RS-200"
    amazon, google, ebay = us.targets
    assert (amazon.market, amazon.ref, amazon.pages) == ("amazon.com", "B0TESTRS20", 1)
    assert (google.market, google.ref) == ("us", "Acme RS-200 rocket skates")
    assert (ebay.market, ebay.max_offers, ebay.listing_type) == ("ebay.com", 240, "buy_it_now")
    idealo = config.products[1].targets[0]
    assert (idealo.ref, idealo.market, idealo.max_offers) == ("123456789", "de", 20)
    assert config.rules.limit(200) == pytest.approx(199.0)
    assert config.rules.floor(200) == pytest.approx(100.0)
    assert config.sellers.is_authorized("Skate World")
    assert config.sellers.ignored("second hand market")
    assert config.report.weekday == 0
    assert config.state.name == "findings.json" and config.state.is_absolute()


def test_defaults():
    config = parse_config(minimal())
    assert config.alerts.to == ("slack", "teams", "discord", "webhook")
    assert config.report.to == ("email",)
    assert config.rules.resolve_after_days == 7
    assert len(config.rules.exclude) == len(DEFAULT_EXCLUDE)
    assert config.sheet.tab == "Findings"


def test_model_terms_ignore_spaces_and_dashes():
    term = Term("WH-1000XM5")
    assert term.model
    assert term.found_in("SONY WH 1000 XM 5 B, Over-ear")
    assert term.found_in("Sony WH‑1000XM5SA")  # a non-breaking hyphen
    assert not term.found_in("Sony WH-1000XM4")
    assert compact("Wh-1000xm5") == "wh1000xm5"


def test_word_terms_match_whole_words():
    used = Term("used")
    assert not used.model
    assert used.found_in("Rocket skates, used twice")
    assert not used.found_in("Unused rocket skates")
    assert Term("open box").found_in("Acme RS-200 (Open-Box)")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"map": None}, "Minimum Advertised Price is required"),
        ({"map": "cheap"}, "expected a number"),
        ({"currency": "dollars"}, "currency code"),
        ({"amazon": [{"asin": "123"}]}, "10-character ASIN"),
        ({"amazon": [{"asin": "B0TESTRS20", "marketplace": "amazon.moon"}]}, "marketplace"),
        ({"amazon": None}, "at least one place it is sold"),
        ({"amazon": None, "ebay": [{"keyword": "skates"}]}, "match"),
        ({"ebay": [{"keyword": "x", "page_size": 100}]}, "60, 120 or 240"),
        ({"idealo": [{"product_id": "abc"}]}, "price comparison link"),
        ({"google_shopping": [{"query": "x"}]}, "country code"),
        ({"colour": "red"}, "unknown setting"),
    ],
)
def test_product_errors(change, message):
    product = {"name": "Skates", "map": 100, "currency": "USD", "amazon": [{"asin": "B0TESTRS20"}]}
    for key, value in change.items():
        if value is None:
            product.pop(key, None)
        else:
            product[key] = value
    with pytest.raises(ConfigError, match=message):
        parse_config({"products": [product]})


def test_duplicate_products_and_targets():
    raw = minimal()
    raw["products"].append(dict(raw["products"][0]))
    with pytest.raises(ConfigError, match="another product"):
        parse_config(raw)
    with pytest.raises(ConfigError, match="listed twice"):
        parse_config(minimal(amazon=["B0TESTRS20", "b0testrs20"]))


def test_short_forms():
    config = parse_config(
        minimal(
            amazon="B0TESTRS20",
            idealo=123456789,
            ebay="Acme RS-200",
            google_shopping={"query": "skates", "country": "GB"},
            match="RS-200",
        )
    )
    sources = {t.source: t for t in config.products[0].targets}
    assert sources["idealo"].ref == "123456789"
    assert sources["ebay"].ref == "Acme RS-200"
    assert sources["google_shopping"].market == "gb"


def test_rules_and_destinations():
    raw = minimal()
    raw["rules"] = {"exclude": ["demo unit"], "keep_default_exclude": False}
    raw["alerts"] = {"to": ["slack"], "back_at_map": True}
    raw["report"] = {"day": False}
    config = parse_config(raw)
    assert [t.text for t in config.rules.exclude] == ["demo unit"]
    assert config.alerts.back_at_map and config.report.weekday is None
    raw["alerts"] = {"to": ["pager"]}
    with pytest.raises(ConfigError, match="unknown destination"):
        parse_config(raw)
    with pytest.raises(ConfigError, match="tolerance_percent"):
        parse_config({**minimal(), "rules": {"tolerance_percent": 80}})


def test_find_products(config):
    assert [p.name for p in config.find_products("(de)")] == ["Rocket Skates RS-200 (DE)"]
    assert len(config.find_products("rs-200")) == 2


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("products: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
