import pytest
from conftest import FakeClient, amazon_json, ebay_json, google_json, idealo_json
from scrapeunblocker import AuthenticationError, InvalidRequestError, ServerError

from map_violation_monitor.collect import (
    amazon_offers,
    ebay_offers,
    fetch,
    google_shopping_offers,
    idealo_offers,
    parse_price,
    request,
    whole_list,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("US $29.99", (29.99, "USD")),
        ("EUR 1.234,50", (1234.5, "EUR")),
        ("KRW105,964.42", (105964.42, "KRW")),
        ("739.84 SEK", (739.84, "SEK")),
        ("1 299,00 €", (1299.0, "EUR")),
        ("£1,299", (1299.0, "GBP")),
        ("$1,234", (1234.0, "USD")),
        (19.5, (19.5, "")),
        ("free", (None, "")),
        (None, (None, "")),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def targets(config):
    us, de = config.products
    return us, {t.source: t for t in us.targets}, de, de.targets[0]


def test_amazon_offers(config):
    _, t, _, _ = targets(config)
    offers, title = amazon_offers(amazon_json(), t["amazon"])
    assert title == "Acme Rocket Skates RS-200, Red"
    assert [o.seller for o in offers][:2] == ["Skate World", "Gadget Outlet"]
    first = offers[0]
    assert first.seller_id == "A1SKATEWORLD" and first.seller_key == "A1SKATEWORLD"
    assert first.url == "https://www.amazon.com/dp/B0TESTRS20?aod=1"
    assert first.seller_url.endswith("seller=A1SKATEWORLD")
    assert (first.price, first.currency, first.condition) == (189.99, "USD", "new")


def test_amazon_sold_by_amazon_gets_a_name(config):
    _, t, _, _ = targets(config)
    data = amazon_json()
    data["offers"][0].update(sellerName=None, sellerId=None, soldByAmazon=True)
    offers, _ = amazon_offers(data, t["amazon"])
    assert (offers[0].seller, offers[0].seller_key) == ("Amazon", "amazon")


def test_google_shopping_drops_cards_without_one_merchant(config):
    _, t, _, _ = targets(config)
    offers, _ = google_shopping_offers(google_json(), t["google_shopping"])
    assert "And more prices" not in [o.seller for o in offers]
    assert len(offers) == 5
    assert offers[0].url == (
        "https://www.google.com/search?udm=28&q=Acme+RS-200+rocket+skates&gl=us"
    )
    assert offers[0].seller_key == "roller depot"


def test_ebay_offers_parse_price_raw_and_skip_suggestions(config):
    _, t, _, _ = targets(config)
    offers, note = ebay_offers(ebay_json(), t["ebay"])
    assert note == ""
    last = offers[-1]
    assert (last.price, last.currency) == (1199.0, "USD")
    assert last.seller_url == "https://www.ebay.com/usr/fair_price_shop"
    assert offers[0].shipping == 5.0
    offers, note = ebay_offers(ebay_json(exactMatches=False), t["ebay"])
    assert offers == [] and "no exact match" in note


def test_idealo_offers_keep_variant(config):
    _, _, _, target = targets(config)
    offers, title = idealo_offers(idealo_json(), target)
    assert title == "Acme Rocket Skates RS-200"
    assert [o.seller for o in offers] == ["schnellshop.de", "rollerhaus.de", "fairpreis.de"]
    assert offers[0].currency == "EUR" and offers[0].variant == "Acme RS-200 Red"
    assert offers[0].seller_key == "111"


def test_whole_list(config):
    _, t, _, idealo = targets(config)
    assert whole_list(amazon_json(), t["amazon"])
    assert not whole_list(amazon_json(hasMore=True), t["amazon"])
    assert whole_list(idealo_json(), idealo)  # 3 offers, fewer than max_offers
    assert not whole_list(ebay_json(), t["ebay"])  # eBay and Google Shopping are samples
    assert not whole_list(google_json(), t["google_shopping"])


def test_request_parameters(config):
    us, t, de, idealo = targets(config)
    client = FakeClient()
    for target in t.values():
        request(client, target, floor=100.0, limit=199.0)
    request(client, idealo, floor=94.5, limit=188.06)
    calls = dict(client.calls)
    assert calls["amazon"] == {
        "asin": "B0TESTRS20",
        "marketplace": "amazon.com",
        "condition": "new",
        "pages": 1,
        "proxy_country": None,
    }
    assert calls["google_shopping"]["gl"] == "us"
    assert calls["google_shopping"]["proxy_country"] == "US"  # pinned to the market
    ebay = calls["ebay"]
    assert (ebay["min_price"], ebay["max_price"]) == (100, 199)  # only the band below MAP
    assert (ebay["condition"], ebay["listing_type"], ebay["page_size"]) == (
        "new",
        "buy_it_now",
        240,
    )
    assert ebay["proxy_country"] == "US"
    assert calls["idealo"] == {"product_id": "123456789", "country": "de", "max_results": 20}


def test_fetch_retries_once_after_a_pause(config):
    us, t, _, _ = targets(config)
    client = FakeClient({"amazon": [ServerError("busy", status_code=502), amazon_json()]})
    pauses = []
    result = fetch(client, us, t["amazon"], floor=100, limit=199, sleep=pauses.append)
    assert result.ok and len(result.offers) == 4
    assert result.calls == 2 and pauses == [20.0]
    assert result.whole_list


def test_fetch_gives_up_after_two_tries(config):
    us, t, _, _ = targets(config)
    client = FakeClient({"ebay": ServerError("busy", status_code=502)})
    result = fetch(client, us, t["ebay"], floor=100, limit=199, sleep=lambda s: None)
    assert not result.ok and "ServerError" in result.error and not result.permanent
    assert result.calls == 2


def test_fetch_permanent_error(config):
    us, t, _, _ = targets(config)
    client = FakeClient({"amazon": InvalidRequestError("bad asin", status_code=400)})
    result = fetch(client, us, t["amazon"], floor=100, limit=199, sleep=lambda s: None)
    assert result.permanent and result.calls == 1


def test_fetch_fatal_error_raises(config):
    us, t, _, _ = targets(config)
    client = FakeClient({"amazon": AuthenticationError("bad key", status_code=401)})
    with pytest.raises(AuthenticationError):
        fetch(client, us, t["amazon"], floor=100, limit=199)


def test_fetch_empty_answer_is_not_ok(config):
    us, t, _, _ = targets(config)
    client = FakeClient({"amazon": amazon_json(offers=[])})
    result = fetch(client, us, t["amazon"], floor=100, limit=199)
    assert not result.ok and result.note == "no offers in the answer"
