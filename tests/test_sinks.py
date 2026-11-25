import json

import pytest
from conftest import NOW, FakeHttp, FakeResponse, FakeSmtp
from test_messages import first_alert

from map_violation_monitor.sinks import (
    HEADER,
    DeliveryError,
    DiscordSink,
    EmailSink,
    SheetSink,
    SlackSink,
    SmtpSettings,
    TeamsSink,
    WebhookSink,
    build_destinations,
    call,
    load_service_account,
    sheet_id,
    sheet_row,
)


def test_slack_post(config):
    alert, _ = first_alert(config)
    http = FakeHttp()
    SlackSink("https://hooks.slack.test/x", http).send(alert)
    body = http.posts("hooks.slack.test")[0]
    assert body["unfurl_links"] is False and body["text"].startswith("🏷️ *Below MAP*")


def test_discord_post_pings_nobody(config):
    alert, _ = first_alert(config)
    http = FakeHttp()
    DiscordSink("https://discord.test/x", http).send(alert)
    body = http.posts("discord.test")[0]
    assert body["allowed_mentions"] == {"parse": []} and body["flags"] == 4


def test_teams_card(config):
    alert, _ = first_alert(config)
    http = FakeHttp()
    TeamsSink("https://teams.test/x", http).send(alert)
    card = http.posts("teams.test")[0]
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard" and content["version"] == "1.4"
    assert "Gadget Outlet" in json.dumps(content)


def test_webhook_payload(config):
    alert, _ = first_alert(config)
    http = FakeHttp()
    WebhookSink("https://hooks.test/map", http).send(alert)
    body = http.posts("hooks.test/map")[0]
    assert body["source"] == "map-violation-monitor" and body["kind"] == "alert"
    assert body["subject"].startswith("Below MAP: 6 sellers")
    assert len(body["events"]) == 6 and "Gadget Outlet" in body["text"]


def test_errors_never_show_the_url():
    http = FakeHttp()
    http.fail["secret-token"] = 404
    with pytest.raises(DeliveryError) as exc:
        call(http, "POST", "https://hooks.test/secret-token", sink="slack", sleep=lambda s: None)
    assert "secret-token" not in str(exc.value) and "HTTP 404" in str(exc.value)


def test_retry_once_on_429():
    class Busy(FakeHttp):
        def request(self, method, url, **kwargs):
            self.requests.append(url)
            if len(self.requests) == 1:
                return FakeResponse(429, "slow down", {"Retry-After": "1"})
            return FakeResponse(200, "ok")

    http = Busy()
    pauses = []
    call(http, "POST", "https://hooks.test/x", sink="slack", sleep=pauses.append)
    assert len(http.requests) == 2 and pauses == [1.0]


def test_email(config):
    alert, _ = first_alert(config)
    settings = SmtpSettings.from_env(
        {"SMTP_HOST": "smtp.test", "EMAIL_TO": "a@x.test; b@x.test", "EMAIL_FROM": "m@x.test"}
    )
    assert settings.port == 587 and settings.security == "starttls"
    assert settings.to == ("a@x.test", "b@x.test")
    FakeSmtp.sent = []
    EmailSink(settings, smtp_factory=FakeSmtp).send(alert)
    mail = FakeSmtp.sent[0]
    assert mail["Subject"] == "Below MAP: 6 sellers on 2 products (first check)"
    assert mail["To"] == "a@x.test, b@x.test"
    html = mail.get_body(("html",)).get_content()
    assert "<h2" in html and "Gadget Outlet" in html
    with pytest.raises(DeliveryError, match="SMTP_SECURITY"):
        SmtpSettings.from_env({"SMTP_HOST": "h", "EMAIL_FROM": "x", "SMTP_SECURITY": "tls"})


def test_sheet_id_and_service_account(service_account, tmp_path):
    assert sheet_id("https://docs.google.com/spreadsheets/d/abc_123/edit#gid=0") == "abc_123"
    assert sheet_id(" abc_123 ") == "abc_123"
    key = tmp_path / "key.json"
    key.write_text(json.dumps(service_account), encoding="utf-8")
    assert load_service_account(str(key))["client_email"].startswith("monitor@")
    with pytest.raises(DeliveryError, match="client_email"):
        load_service_account('{"type": "service_account"}')


def test_sheet_rows_and_append(config, service_account):
    alert, _ = first_alert(config)
    rows = [sheet_row(e, "Rocket Skates", "RS-200", NOW.isoformat()) for e in alert.events]
    assert len(rows[0]) == len(HEADER)
    gadget = next(r for r in rows if r[6] == "Gadget Outlet")
    assert gadget[:6] == [
        "2026-11-25 07:00:00",
        "below MAP",
        "Rocket Skates",
        "RS-200",
        "Amazon",
        "amazon.com",
    ]
    assert gadget[9:14] == [179.0, 200.0, "USD", 21.0, 10.5]
    assert gadget[17] == "https://www.amazon.com/dp/B0TESTRS20?aod=1"

    http = FakeHttp()
    sink = SheetSink("sheet123", service_account, http, api_url="https://sheets.test")
    sink.append(rows)
    sink.append(rows[:1])
    tab = http.tabs["Findings"]
    assert tab[0] == HEADER and len(tab) == 1 + len(rows) + 1
    token_calls = [r for r in http.requests if r["url"].endswith("/token")]
    assert len(token_calls) == 1  # the access token is reused
    assertion = token_calls[0]["data"]["assertion"]
    assert assertion.count(".") == 2


def test_formula_like_cells_stay_text(config):
    alert, _ = first_alert(config)
    event = alert.events[0]
    event.finding.seller = "=HYPERLINK(1)"
    assert sheet_row(event, "x", "", NOW.isoformat())[6] == "'=HYPERLINK(1)"


def test_build_destinations(config, env):
    http = FakeHttp()
    dests = build_destinations(config, env, http)
    assert [s.name for s in dests.alerts] == ["slack", "teams", "discord", "webhook"]
    assert [s.name for s in dests.report] == ["email", "slack"]
    assert dests.sheet is not None and dests.skipped == []
    assert len(dests.all) == 6  # slack is one sink for both

    bare = build_destinations(config, {"SLACK_WEBHOOK_URL": "https://x.test"}, http)
    assert [s.name for s in bare.alerts] == ["slack"] and bare.sheet is None
    assert bare.skipped == ["sheet", "teams", "discord", "webhook", "email"]
