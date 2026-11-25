"""Shared fakes: an API client with canned answers, an HTTP client for the sinks (webhooks,
the Google token endpoint and the Sheets API), an SMTP server, and config files.

No test calls the real API, posts anywhere or sends email. The JSON below has the shape of
real answers from the Amazon offers, Google Shopping, eBay search and idealo offers
endpoints, with a made-up product and made-up sellers.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from map_violation_monitor.config import Config, load_config

NOW = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)  # a Wednesday

CONFIG = """
products:
  - name: Rocket Skates RS-200 (US)
    sku: RS-200
    map: 200.00
    currency: USD
    match: [RS-200]
    exclude: [RS-300, wheels]
    amazon:
      - asin: B0TESTRS20
        marketplace: amazon.com
    google_shopping:
      - query: Acme RS-200 rocket skates
        country: us
    ebay:
      - keyword: Acme RS-200
        marketplace: ebay.com
  - name: Rocket Skates RS-200 (DE)
    sku: RS-200
    map: 189.00
    currency: EUR
    idealo:
      - product_id: 123456789
        country: de
rules:
  tolerance_percent: 0.5
  floor_percent: 50
  include_shipping: false
  realert_drop_percent: 3
  resolve_after_days: 7
sellers:
  ignore: [Second Hand Market]
  authorized: [Skate World]
alerts:
  to: [slack, teams, discord, webhook]
  max_per_source: 8
report:
  day: monday
  to: [email, slack]
sheet:
  enabled: true
  tab: Findings
state: state/findings.json
"""


def amazon_json(offers: list[tuple[str, str, float]] | None = None, **extra: Any) -> dict:
    """Amazon offers answer. ``offers`` = (seller name, seller id, price)."""
    if offers is None:
        offers = [
            ("Skate World", "A1SKATEWORLD", 189.99),
            ("Gadget Outlet", "A2GADGETOUT", 179.00),
            ("Gadget Outlet", "A2GADGETOUT", 185.00),
            ("Full Price Store", "A3FULLPRICE", 199.99),
        ]
    return {
        "asin": "B0TESTRS20",
        "marketplace": "amazon.com",
        "title": "Acme Rocket Skates RS-200, Red",
        "hasMore": False,
        "totalOffers": len(offers),
        "offers": [
            {
                "sellerName": name,
                "sellerId": sid,
                "sellerUrl": f"https://www.amazon.com/sp?seller={sid}",
                "price": price,
                "currency": "USD",
                "priceRaw": f"${price:.2f}",
                "shippingPrice": 0.0,
                "conditionType": "new",
                "soldByAmazon": False,
            }
            for name, sid, price in offers
        ],
        **extra,
    }


def google_json(cards: list[tuple[str, str, float]] | None = None) -> dict:
    """Google Shopping answer. ``cards`` = (title, merchant, price)."""
    if cards is None:
        cards = [
            ("Acme Rocket Skates RS-200", "Roller Depot", 159.00),
            ("Acme RS-200 Rocket Skates, Blue", "Wheel Barn", 210.00),
            ("Acme RS-300 Rocket Skates", "Wheel Barn", 120.00),
            ("Acme RS-200 replacement wheels", "Parts Place", 150.00),
            ("Acme Rocket Skates RS-200", "Second Hand Market", 110.00),
            ("Acme Rocket Skates RS-200", "And more prices", 150.00),
        ]
    return {
        "query": "Acme RS-200 rocket skates",
        "gl": "us",
        "products": [
            {
                "title": title,
                "price": price,
                "priceRaw": f"${price:.2f}",
                "currency": "$",
                "currencyCode": "USD",
                "merchant": merchant,
                "productId": f"9{i:05d}",
            }
            for i, (title, merchant, price) in enumerate(cards)
        ],
    }


def ebay_json(items: list[tuple[str, str, Any]] | None = None, **extra: Any) -> dict:
    """eBay search answer. ``items`` = (title, seller, price or None for priceRaw only)."""
    if items is None:
        items = [
            ("NEW Acme RS-200 Rocket Skates sealed", "brightdeals", 149.99),
            ("Acme RS-200 rocket skates", "brightdeals", 155.00),
            ("Acme RS 200 Rocket Skates - used once", "skater_sam", 120.00),
            ("Acme RS-200 Rocket Skates", "fair_price_shop", None),
        ]
    results = []
    for i, (title, seller, price) in enumerate(items):
        results.append(
            {
                "listingId": f"10000{i}",
                "title": title,
                "url": f"https://www.ebay.com/itm/10000{i}",
                "conditionCode": "new",
                "seller": {"username": seller, "feedbackPercent": 99.5},
                "price": price,
                "currency": "USD" if price is not None else None,
                "priceRaw": f"US ${price:.2f}" if price is not None else "US $1,199.00",
                "shippingCost": 5.0,
            }
        )
    return {"keyword": "Acme RS-200", "exactMatches": True, "results": results, **extra}


def idealo_json(offers: list[tuple[str, str, float]] | None = None) -> dict:
    """idealo offers answer, cheapest first. ``offers`` = (shop, shop id, price)."""
    if offers is None:
        offers = [
            ("schnellshop.de", "111", 169.00),
            ("rollerhaus.de", "222", 179.50),
            ("fairpreis.de", "333", 189.00),
        ]
    return {
        "productId": "123456789",
        "title": "Acme Rocket Skates RS-200",
        "currency": "EUR",
        "url": "https://www.idealo.de/preisvergleich/OffersOfProduct/123456789.html",
        "offers": [
            {
                "offerKey": f"key{i}",
                "title": f"Acme RS-200 Raketenrollschuhe ({shop})",
                "price": price,
                "shippingCost": 4.99,
                "shop": {"id": sid, "name": shop},
                "variantName": "Acme RS-200 Red",
                "url": f"https://www.idealo.de/relocator/relocate?offerKey=key{i}",
            }
            for i, (shop, sid, price) in enumerate(offers)
        ],
    }


class FakeClient:
    """Stands in for ``scrapeunblocker.Client``: canned answers, every call recorded.

    An answer can be a dict, an exception instance to raise, or a list of those to hand
    out one per call.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = {
            "amazon": amazon_json(),
            "google_shopping": google_json(),
            "ebay": ebay_json(),
            "idealo": idealo_json(),
            **(answers or {}),
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _answer(self, source: str, params: dict[str, Any]) -> Any:
        self.calls.append((source, params))
        answer = self.answers[source]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)

    def amazon_offers(self, **params: Any) -> Any:
        return self._answer("amazon", params)

    def google_shopping(self, q: str, **params: Any) -> Any:
        return self._answer("google_shopping", {"q": q, **params})

    def ebay_search(self, keyword: str, **params: Any) -> Any:
        return self._answer("ebay", {"keyword": keyword, **params})

    def idealo_offers(self, product_id: str | None = None, **params: Any) -> Any:
        return self._answer("idealo", {"product_id": product_id, **params})


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class FakeHttp:
    """Records every request. Plays the Google token endpoint and a small Sheets API;
    everything else answers 200 (or what ``fail`` says for that URL fragment)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.tabs: dict[str, list[list[Any]]] = {"Sheet1": []}
        self.fail: dict[str, int] = {}

    def posts(self, fragment: str) -> list[Any]:
        return [r["json"] for r in self.requests if fragment in r["url"] and r["method"] == "POST"]

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        for fragment, code in self.fail.items():
            if fragment in url:
                return FakeResponse(code, "nope")
        if url.endswith("/token"):
            return FakeResponse(200, {"access_token": "t0k", "expires_in": 3600})
        if "/v4/spreadsheets/" in url:
            return self._sheets(method, url, kwargs)
        return FakeResponse(200, "ok")

    def _sheets(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        tail = url.split("/v4/spreadsheets/", 1)[1]
        sheet, _, rest = tail.partition("/")
        if sheet.endswith(":batchUpdate"):
            for r in kwargs["json"]["requests"]:
                self.tabs.setdefault(r["addSheet"]["properties"]["title"], [])
            return FakeResponse(200, {})
        if not rest:
            return FakeResponse(200, {"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        rng = unquote(rest.removeprefix("values/")).removesuffix(":append")
        tab = rng.split("!")[0].strip("'")
        rows = self.tabs[tab]
        if method == "GET":
            return FakeResponse(200, {"values": rows[:1]} if rows else {})
        if method == "PUT":
            rows[0:1] = kwargs["json"]["values"]
        else:
            rows.extend(kwargs["json"]["values"])
        return FakeResponse(200, {})


class FakeSmtp:
    sent: list[Any] = []

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def send_message(self, mail: Any) -> None:
        FakeSmtp.sent.append(mail)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def config(config_path: Path) -> Config:
    return load_config(config_path)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def write(text: str) -> Path:
        path = tmp_path / "config.yml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def service_account() -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "client_email": "monitor@test-project.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.test/token",
    }


@pytest.fixture
def env(service_account: dict[str, Any]) -> dict[str, str]:
    FakeSmtp.sent = []
    return {
        "SCRAPEUNBLOCKER_KEY": "test-key",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/T/B/x",
        "TEAMS_WEBHOOK_URL": "https://teams.test/workflows/x",
        "DISCORD_WEBHOOK_URL": "https://discord.test/api/webhooks/1/x",
        "ALERT_WEBHOOK_URL": "https://hooks.test/map",
        "GOOGLE_SHEET_ID": "https://docs.google.com/spreadsheets/d/sheet123/edit",
        "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(service_account),
        "GOOGLE_SHEETS_API_URL": "https://sheets.test",
        "SMTP_HOST": "smtp.test",
        "SMTP_SECURITY": "none",
        "EMAIL_FROM": "monitor@acme.test",
        "EMAIL_TO": "brand@acme.test",
    }
