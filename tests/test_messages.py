import json
from datetime import timedelta

from conftest import NOW, FakeClient, amazon_json, ebay_json
from test_process import TODAY, checked_all

from map_violation_monitor.messages import (
    DISCORD,
    SLACK,
    TEAMS_CARD_TEXT,
    build_alert,
    build_report,
    money,
    render,
    render_html,
    render_plain,
    teams_cards,
)
from map_violation_monitor.process import compare
from map_violation_monitor.state import State


def first_alert(config):
    events, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    return build_alert(config, events, NOW, first_run=True, back_at_map=False), opened


def test_money():
    assert money(1234.5, "USD") == "$1,234.50"
    assert money(199, "EUR") == "€199.00"
    assert money(1500, "JPY") == "¥1,500"
    assert money(99.9, "PLN") == "99.90 PLN"


def test_first_alert(config):
    alert, _ = first_alert(config)
    assert alert.subject == "Below MAP: 6 sellers on 2 products (first check)"
    text = render_plain(alert.doc())
    assert text.startswith("🏷️ Below MAP · Wed 25 Nov 2026 · first check, 6 sellers\n")
    assert "Rocket Skates RS-200 (US) · MAP $200.00" in text
    # Amazon first, then Google Shopping, eBay; deepest undercut first within a source
    amazon = text.index(
        "🆕 Amazon (amazon.com) · Gadget Outlet: $179.00, 10.5% below MAP (2 listings)"
    )
    skate = text.index(
        "🆕 Amazon (amazon.com) · Skate World: $189.99, 5% below MAP (authorized reseller)"
    )
    google = text.index("🆕 Google Shopping (us) · Roller Depot: $159.00, 20.5% below MAP")
    ebay = text.index("🆕 eBay (ebay.com) · brightdeals: $149.99, 25% below MAP (2 listings)")
    assert amazon < skate < google < ebay
    assert "“NEW Acme RS-200 Rocket Skates sealed” · + $5.00 shipping" in text
    assert "🆕 idealo (de) · schnellshop.de: €169.00, 10.6% below MAP" in text
    assert "variant: Acme RS-200 Red" in text
    assert "https://www.ebay.com/itm/100000" in text
    assert text.rstrip().endswith("drops further.")


def test_alert_caps_each_source(config):
    from dataclasses import replace

    many = ebay_json([(f"Acme RS-200 skates #{i}", f"seller{i:02d}", 150.0 + i) for i in range(11)])
    events, _ = compare(
        checked_all(config, FakeClient({"ebay": many})), {}, config.rules, TODAY, {}
    )
    small = replace(config, alerts=replace(config.alerts, max_per_source=3))
    text = render_plain(build_alert(small, events, NOW, first_run=True, back_at_map=False).doc())
    assert text.count("🆕 eBay") == 3
    assert "+ 8 more sellers below MAP on eBay (ebay.com), listed in the Google Sheet" in text


def test_lower_and_back_alert(config):
    _, opened = first_alert(config)
    cheaper = amazon_json([("Gadget Outlet", "A2GADGETOUT", 170.00)])
    events, _ = compare(
        checked_all(config, FakeClient({"amazon": cheaper})), opened, config.rules, TODAY, {}
    )
    alert = build_alert(config, events, NOW, first_run=False, back_at_map=True)
    assert alert.subject == "Below MAP: 1 further down · 1 back at MAP on 1 product"
    text = render_plain(alert.doc())
    assert "🔻 Amazon (amazon.com) · Gadget Outlet: $170.00, was $179.00 (15% below MAP)" in text
    assert "✅ Amazon (amazon.com) · Skate World: back at MAP after 1 day (was $189.99)" in text
    hidden = build_alert(config, events, NOW, first_run=False, back_at_map=False)
    assert "back at MAP" not in render_plain(hidden.doc())


def test_alert_data_for_the_webhook(config):
    alert, _ = first_alert(config)
    data = alert.data()
    assert data["counts"] == {"new": 6, "lower": 0, "back": 0} and data["first_run"]
    event = next(e for e in data["events"] if e["seller"] == "Skate World")
    assert event["product"] == "Rocket Skates RS-200 (US)" and event["sku"] == "RS-200"
    assert event["below_map_by"] == 10.01 and event["authorized"] is True
    assert event["seller_id"] == "A1SKATEWORLD" and event["event"] == "new"
    json.dumps(data)


def test_slack_and_discord_markup(config):
    alert, _ = first_alert(config)
    slack = "\n".join(render(alert.doc(), SLACK))
    assert "*Rocket Skates RS-200 (US) · MAP $200.00*" in slack
    assert "<https://www.ebay.com/itm/100000|view>" in slack
    discord = render(alert.doc(), DISCORD)
    assert all(len(post) <= DISCORD.limit for post in discord)
    assert "[view](<https://www.ebay.com/itm/100000>)" in "\n".join(discord)


def test_teams_cards_split_long_messages(config):
    many = ebay_json([(f"Acme RS-200 skates #{i}", f"seller{i:03d}", 150.0) for i in range(400)])
    events, _ = compare(
        checked_all(config, FakeClient({"ebay": many})), {}, config.rules, TODAY, {}
    )
    from dataclasses import replace

    big = replace(config, alerts=replace(config.alerts, max_per_source=400))
    cards = teams_cards(build_alert(big, events, NOW, first_run=True, back_at_map=False).doc())
    assert len(cards) > 1
    for card in cards:
        body = card["attachments"][0]["content"]["body"]
        assert sum(len(b["text"]) for b in body) <= TEAMS_CARD_TEXT + 500
    assert cards[0]["attachments"][0]["content"]["body"][0]["size"] == "Medium"


def test_html_escapes(config):
    alert, _ = first_alert(config)
    doc = alert.doc()
    doc.sections[0].items[0].text = "<b>Gadget & Co</b>"
    html = render_html(doc)
    assert "&lt;b&gt;Gadget &amp; Co&lt;/b&gt;" in html
    assert 'href="https://www.amazon.com/dp/B0TESTRS20?aod=1"' in html


def test_weekly_report(config):
    from conftest import FakeClient as Client

    events, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    state = State()
    state.record(events, opened, TODAY)
    later = TODAY + timedelta(days=5)
    fixed = Client({"amazon": amazon_json([("Gadget Outlet", "A2GADGETOUT", 199.50)])})
    ev2, op2 = compare(checked_all(config, fixed), state.open, config.rules, later, {})
    state.record(ev2, op2, later)
    state.history[next(k for k in state.open if k.endswith("|brightdeals"))]["episodes"] = 2
    now = NOW + timedelta(days=5)
    report = build_report(config, state.open, state.history, state.back, now)
    assert report.subject == "MAP report · week 49: 4 sellers below MAP on 2 products, 4 new"
    text = render_plain(report.doc())
    assert "New this week: 4 · back at MAP: 2 · repeat offenders: 1" in text
    assert "Google Shopping 1 · eBay 1 · idealo 2" in text
    assert "Rocket Skates RS-200 (US) · MAP $200.00 · 2 sellers below" in text
    assert (
        "🆕 eBay (ebay.com) · brightdeals: $149.99, 25% below MAP (2 listings) · since 25 Nov"
        in text
    )
    assert "Back at MAP this week" in text
    assert (
        "Gadget Outlet: Rocket Skates RS-200 (US), back on 30 Nov after 5 days (was $179.00)"
        in text
    )
    assert (
        "brightdeals · eBay (ebay.com): 2 times below MAP on 1 product, below MAP now on 1" in text
    )
    data = report.data()
    assert data["week"] == "2026-W49" and data["since"] == "2026-11-24"
    assert len(data["below_map"]) == 4 and len(data["back_at_map"]) == 2
