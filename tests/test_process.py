from datetime import date, timedelta

from conftest import FakeClient, amazon_json, ebay_json, google_json, idealo_json

from map_violation_monitor.collect import fetch
from map_violation_monitor.process import BACK, LOWER, NEW, check, compare

TODAY = date(2026, 11, 25)


def checked_all(config, client=None):
    client = client or FakeClient()
    out = []
    for p in config.products:
        for t in p.targets:
            f = fetch(
                client,
                p,
                t,
                floor=config.rules.floor(p.map),
                limit=config.rules.limit(p.map),
                sleep=lambda s: None,
            )
            out.append(check(f, config.rules, config.sellers))
    return out


def by_source(checked):
    return {c.target.source: c for c in checked}


def test_check_keeps_only_this_product_below_map(config):
    c = by_source(checked_all(config))
    amazon = c["amazon"]
    assert [(f.seller, f.price, f.listings) for f in amazon.findings] == [
        ("Gadget Outlet", 179.0, 2),
        ("Skate World", 189.99, 1),
    ]
    assert amazon.at_map == {"A3FULLPRICE"} and amazon.complete
    skate_world = amazon.findings[1]
    assert skate_world.authorized and skate_world.gap == 10.01 and skate_world.gap_percent == 5.0

    google = c["google_shopping"]
    assert [f.seller for f in google.findings] == ["Roller Depot"]
    assert google.skipped == {"other product": 1, "excluded words": 1, "ignored seller": 1}
    assert not google.complete  # a sample

    ebay = c["ebay"]
    assert [(f.seller, f.price, f.listings) for f in ebay.findings] == [("brightdeals", 149.99, 2)]
    assert ebay.skipped == {"excluded words": 1}  # "used once"
    assert ebay.findings[0].url == "https://www.ebay.com/itm/100000"

    idealo = c["idealo"]
    assert [f.seller for f in idealo.findings] == ["schnellshop.de", "rollerhaus.de"]
    assert idealo.findings[0].variant == "Acme RS-200 Red"
    assert idealo.complete


def test_check_currency_floor_and_shipping(config):
    from dataclasses import replace

    client = FakeClient(
        {"ebay": ebay_json([("Acme RS-200", "cheapo", 60.0), ("Acme RS-200", "fair", 190.0)])}
    )
    c = by_source(checked_all(config, client))
    assert c["ebay"].skipped == {"below the floor": 1}
    assert [f.seller for f in c["ebay"].findings] == ["fair"]

    shipping = replace(config.rules, include_shipping=True)
    product = config.products[0]
    target = product.targets[2]
    f = fetch(client, product, target, floor=100, limit=199, sleep=lambda s: None)
    result = check(f, shipping, config.sellers)
    assert result.findings[0].price == 195.0  # 190 + 5 shipping

    euro = FakeClient({"amazon": amazon_json()})
    euro.answers["amazon"]["offers"][0]["currency"] = "EUR"
    c = by_source(checked_all(config, euro))
    assert c["amazon"].skipped == {"other currency": 1}


def test_answer_without_this_product_proves_nothing(config):
    client = FakeClient({"google_shopping": google_json([("Other Brand Skates", "Shop", 90.0)])})
    c = by_source(checked_all(config, client))
    assert c["google_shopping"].offers == 0 and not c["google_shopping"].answered


def test_first_run_opens_everything(config):
    events, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    assert {e.kind for e in events} == {NEW}
    assert len(events) == len(opened) == 6
    assert all(o.first_seen == "2026-11-25" for o in opened.values())


def test_second_run_with_no_change_is_quiet(config):
    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    events, again = compare(checked_all(config), opened, config.rules, TODAY, {})
    assert events == []
    assert again.keys() == opened.keys()


def test_deeper_undercut_alerts_again(config):
    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    cheaper = amazon_json(
        [("Skate World", "A1SKATEWORLD", 189.00), ("Gadget Outlet", "A2GADGETOUT", 170.00)]
    )
    events, after = compare(
        checked_all(config, FakeClient({"amazon": cheaper})), opened, config.rules, TODAY, {}
    )
    lower = [e for e in events if e.kind == LOWER]
    assert [(e.finding.seller, e.previous) for e in lower] == [("Gadget Outlet", 179.0)]
    # Skate World went from 189.99 to 189.00: less than realert_drop_percent, no alert
    key = lower[0].finding.key
    assert after[key].alerted_price == 170.0 and after[key].first_seen == "2026-11-25"


def test_complete_sources_close_findings_at_once(config):
    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    fixed = amazon_json([("Gadget Outlet", "A2GADGETOUT", 199.99)])
    events, after = compare(
        checked_all(config, FakeClient({"amazon": fixed})), opened, config.rules, TODAY, {}
    )
    back = sorted(e.finding.seller for e in events if e.kind == BACK)
    assert back == ["Gadget Outlet", "Skate World"]
    assert len(after) == 4


def test_sampled_sources_wait_resolve_after_days(config):
    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    empty_ebay = FakeClient({"ebay": ebay_json([("Acme RS-200 skates", "someone_else", 250.0)])})
    for days, expect_back in ((1, False), (6, False), (7, True)):
        events, _ = compare(
            checked_all(config, empty_ebay),
            opened,
            config.rules,
            TODAY + timedelta(days=days),
            {},
        )
        sellers = [e.finding.seller for e in events if e.kind == BACK]
        assert (sellers == ["brightdeals"]) is expect_back, days


def test_no_answer_keeps_findings_open(config):
    from scrapeunblocker import ServerError

    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    broken = FakeClient(
        {
            "amazon": ServerError("busy", status_code=502),
            "idealo": idealo_json([]),
        }
    )
    events, after = compare(
        checked_all(config, broken), opened, config.rules, TODAY + timedelta(days=30), {}
    )
    kept = {k for k in after if "|amazon|" in k or "|idealo|" in k}
    assert len(kept) == 4  # Amazon failed, idealo had no offers: nothing is known
    assert not [e for e in events if e.finding.source in ("amazon", "idealo")]


def test_episodes_count_repeat_offenders(config):
    _, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    key = next(k for k in opened if k.endswith("|A2GADGETOUT"))
    events, _ = compare(checked_all(config), {}, config.rules, TODAY, {key: 2})
    event = next(e for e in events if e.finding.key == key)
    assert event.kind == NEW and event.episode == 3
