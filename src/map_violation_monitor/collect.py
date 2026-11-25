"""Read every seller's advertised price through the ScrapeUnblocker API.

One SDK call per target (see ``Target.calls`` for the paged ones):

- ``client.amazon_offers(asin=..., marketplace=..., condition="new")``
  (``POST /marketplace/amazon-offers``): every seller under "All buying options"
- ``client.google_shopping(q, gl=...)`` (``POST /goods/google-shopping``): the product
  cards of a Google Shopping search, one merchant each. Google shows a different
  selection of merchants on each search, so this source is a sample, never a full list.
- ``client.ebay_search(keyword, marketplace=..., condition="new", sort="price_asc", ...)``
  (``POST /marketplace/ebay-search``): the cheapest new fixed-price listings
- ``client.idealo_offers(product_id, country=..., max_results=...)``
  (``POST /marketplace/idealo-offers``): every shop in an idealo price comparison

Each answer is turned into the same :class:`Offer` shape, so the rest of the code never
needs to know where a price came from.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote_plus

from scrapeunblocker import (
    AuthenticationError,
    BlockedError,
    BrowserTimeoutError,
    InvalidRequestError,
    PaymentRequiredError,
    RateLimitError,
    ScrapeTimeoutError,
    ScrapeUnblockerError,
    ServerError,
    TargetNotFoundError,
    UpstreamOutageError,
    ValidationError,
)
from scrapeunblocker import ConnectionError as SUConnectionError

from .config import Product, Target

log = logging.getLogger(__name__)

# Errors that end the whole run: the next target cannot do better.
FATAL = (AuthenticationError, PaymentRequiredError)
# Errors worth one more try after a pause: a busy site, a dropped connection.
RETRYABLE = (
    ServerError,
    RateLimitError,
    UpstreamOutageError,
    ScrapeTimeoutError,
    SUConnectionError,
    BlockedError,
    BrowserTimeoutError,
)
# Errors that mean config.yml points at something that does not exist.
PERMANENT = (InvalidRequestError, ValidationError, TargetNotFoundError)
RETRY_PAUSE = 20.0

# Where a shopper of each eBay site is. Prices, currency and the results themselves follow
# the visitor's country, so every call is made from the market's own country unless
# config.yml says otherwise.
EBAY_COUNTRY = {
    "ebay.com": "US",
    "ebay.co.uk": "GB",
    "ebay.de": "DE",
    "ebay.fr": "FR",
    "ebay.it": "IT",
    "ebay.es": "ES",
    "ebay.nl": "NL",
    "ebay.pl": "PL",
    "ebay.at": "AT",
    "ebay.ch": "CH",
    "ebay.be": "BE",
    "ebay.ie": "IE",
    "ebay.ca": "CA",
    "ebay.com.au": "AU",
    "ebay.in": "IN",
    "ebay.ph": "PH",
    "ebay.com.sg": "SG",
    "ebay.com.my": "MY",
    "ebay.com.hk": "HK",
}


class ShopClient(Protocol):
    def amazon_offers(self, **params: Any) -> Any: ...

    def google_shopping(self, q: str, **params: Any) -> Any: ...

    def ebay_search(self, keyword: str, **params: Any) -> Any: ...

    def idealo_offers(self, product_id: str | None = None, **params: Any) -> Any: ...


@dataclass(frozen=True)
class Offer:
    """One seller's advertised price for one listing."""

    source: str
    market: str
    seller: str  # the name shoppers see
    seller_id: str  # Amazon seller id, eBay user name, idealo shop id ("" when none)
    price: float
    currency: str
    shipping: float | None = None
    title: str = ""
    url: str = ""  # where to see it: the listing, or the page that lists it
    listing: str = ""  # the listing's own id at the source
    condition: str = ""  # new, used, ... ("" when the source does not say)
    seller_url: str = ""
    variant: str = ""  # idealo: the variant of the price comparison, e.g. "Black"

    @property
    def seller_key(self) -> str:
        """One seller at one source and market, whatever its listings."""
        key = self.seller_id or " ".join(self.seller.casefold().split())
        return key or "unknown"


@dataclass
class Fetch:
    """What one target returned this run."""

    product: Product
    target: Target
    offers: list[Offer] = field(default_factory=list)
    calls: int = 0
    error: str = ""
    permanent: bool = False  # config.yml needs a fix
    note: str = ""  # e.g. "no exact match"
    title: str = ""  # the product title the source shows, when it has one
    whole_list: bool = False  # every offer there is was read (no further page)

    @property
    def ok(self) -> bool:
        """The source answered with offers. An empty answer proves nothing: a finding is
        only closed by an answer that has offers in it."""
        return not self.error and bool(self.offers)


# -- prices ---------------------------------------------------------------------------------

_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "zł": "PLN"}
_NUMBER = re.compile(r"\d[\d.,\s']*")


def parse_price(raw: Any) -> tuple[float | None, str]:
    """``"US $29.99"`` -> ``(29.99, "USD")``, ``"EUR 1.234,50"`` -> ``(1234.5, "EUR")``."""
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return (float(raw), "") if math.isfinite(raw) else (None, "")
    if not isinstance(raw, str):
        return None, ""
    text = raw.strip()
    code = ""
    m = _CODE.search(text)
    if m:
        code = m.group(1)
    else:
        for symbol, iso in _SYMBOLS.items():
            if symbol in text:
                code = iso
                break
    n = _NUMBER.search(text)
    if not n:
        return None, code
    digits = re.sub(r"[\s']", "", n.group(0)).rstrip(".,")
    if "," in digits and "." in digits:
        # the last separator is the decimal one
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits:
        head, _, tail = digits.rpartition(",")
        digits = (
            f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else digits.replace(",", "")
        )
    elif digits.count(".") > 1 or (digits.count(".") == 1 and len(digits.rpartition(".")[2]) == 3):
        digits = digits.replace(".", "")
    try:
        return float(digits), code
    except ValueError:
        return None, code


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return parse_price(value)[0]
    return None


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


# -- one normalizer per source --------------------------------------------------------------


def amazon_offers(data: dict[str, Any], target: Target) -> tuple[list[Offer], str]:
    dp = f"https://www.{target.market}/dp/{target.ref}"
    offers = []
    for o in data.get("offers") or []:
        if not isinstance(o, dict):
            continue
        price = _num(o.get("price"))
        if price is None:
            continue
        sold_by_amazon = bool(o.get("soldByAmazon"))
        name = _text(o.get("sellerName")) or ("Amazon" if sold_by_amazon else "")
        seller_id = _text(o.get("sellerId")) or ("amazon" if sold_by_amazon else "")
        offers.append(
            Offer(
                source="amazon",
                market=target.market,
                seller=name or "unknown seller",
                seller_id=seller_id,
                price=price,
                currency=_text(o.get("currency")).upper(),
                shipping=_num(o.get("shippingPrice")),
                title=_text(data.get("title")),
                url=f"{dp}?aod=1",
                listing=target.ref,
                condition=_text(o.get("conditionType")).lower(),
                seller_url=_text(o.get("sellerUrl")),
            )
        )
    return offers, _text(data.get("title"))


def _merchant(raw: Any) -> str:
    """Google's merchant line, or "" when it is not one shop ("And more prices")."""
    name = _text(raw)
    if not name or "more prices" in name.casefold():
        return ""
    return name


def google_shopping_offers(data: dict[str, Any], target: Target) -> tuple[list[Offer], str]:
    # Google Shopping exposes no per-product link, so the evidence link is the search.
    url = f"https://www.google.com/search?udm=28&q={quote_plus(target.ref)}&gl={target.market}"
    offers = []
    for p in data.get("products") or []:
        if not isinstance(p, dict):
            continue
        merchant = _merchant(p.get("merchant"))
        price = _num(p.get("price"))
        title = _text(p.get("title"))
        if not merchant or price is None or not title:
            continue
        currency = _text(p.get("currencyCode")).upper() or parse_price(p.get("priceRaw"))[1]
        offers.append(
            Offer(
                source="google_shopping",
                market=target.market,
                seller=merchant,
                seller_id="",
                price=price,
                currency=currency,
                title=title,
                url=url,
                listing=_text(p.get("productId")),
            )
        )
    return offers, ""


def ebay_offers(data: dict[str, Any], target: Target) -> tuple[list[Offer], str]:
    if data.get("exactMatches") is False:
        return [], "eBay found no exact match and showed suggestions instead"
    offers = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        price = _num(item.get("price"))
        raw_price, raw_code = parse_price(item.get("priceRaw"))
        if price is None:
            price = raw_price
        if price is None:
            continue
        seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
        user = _text(seller.get("username"))
        offers.append(
            Offer(
                source="ebay",
                market=target.market,
                seller=user or "unknown seller",
                seller_id=user.casefold(),
                price=price,
                currency=_text(item.get("currency")).upper() or raw_code,
                shipping=_num(item.get("shippingCost")),
                title=_text(item.get("title")),
                url=_text(item.get("url")),
                listing=_text(item.get("listingId")),
                condition=_text(item.get("conditionCode")).lower(),
                seller_url=f"https://www.{target.market}/usr/{quote_plus(user)}" if user else "",
            )
        )
    return offers, ""


def idealo_offers(data: dict[str, Any], target: Target) -> tuple[list[Offer], str]:
    currency = _text(data.get("currency")).upper()
    offers = []
    for o in data.get("offers") or []:
        if not isinstance(o, dict):
            continue
        price = _num(o.get("price"))
        shop = o.get("shop") if isinstance(o.get("shop"), dict) else {}
        name = _text(shop.get("name"))
        if price is None or not name:
            continue
        offers.append(
            Offer(
                source="idealo",
                market=target.market,
                seller=name,
                seller_id=_text(shop.get("id")),
                price=price,
                currency=currency,
                shipping=_num(o.get("shippingCost")),
                title=_text(o.get("title")),
                variant=_text(o.get("variantName")),
                url=_text(o.get("url")) or _text(data.get("url")),
                listing=_text(o.get("offerKey")),
                condition="new",
            )
        )
    return offers, _text(data.get("title"))


NORMALIZE: dict[str, Callable[[dict[str, Any], Target], tuple[list[Offer], str]]] = {
    "amazon": amazon_offers,
    "google_shopping": google_shopping_offers,
    "ebay": ebay_offers,
    "idealo": idealo_offers,
}


def whole_list(data: dict[str, Any], target: Target) -> bool:
    """The answer holds every offer the source has, so a seller missing from it is gone."""
    if target.source == "amazon":
        return data.get("hasMore") is False
    if target.source == "idealo":
        return len(data.get("offers") or []) < target.max_offers
    # eBay and Google Shopping show each visitor a different selection (eBay's result count
    # for the same search swings with the visitor's location), so they are samples.
    return False


# -- calling the API ------------------------------------------------------------------------


def request(client: ShopClient, target: Target, floor: float, limit: float) -> Any:
    """The one SDK call for a target.

    eBay is asked only for listings priced between ``floor`` and ``limit`` (MAP less the
    tolerance): eBay's own price filter is exact, while its price sort is loose, so a
    page of the band holds far more listings that matter than a page sorted by price.
    """
    proxy = target.proxy_country.upper() or None
    if target.source == "amazon":
        # The API sends Amazon calls from the marketplace's home country by default.
        return client.amazon_offers(
            asin=target.ref,
            marketplace=target.market,
            condition="new",
            pages=target.pages,
            proxy_country=proxy,
        )
    if target.source == "google_shopping":
        return client.google_shopping(
            target.ref,
            gl=target.market,
            max_results=60,
            proxy_country=proxy or target.market.upper(),
        )
    if target.source == "ebay":
        return client.ebay_search(
            target.ref,
            marketplace=target.market,
            condition="new",
            listing_type=target.listing_type,
            sort="price_asc",
            min_price=math.floor(floor) or None,
            max_price=math.ceil(limit),
            page_size=target.max_offers,
            proxy_country=proxy or EBAY_COUNTRY.get(target.market),
        )
    return client.idealo_offers(target.ref, country=target.market, max_results=target.max_offers)


def fetch(
    client: ShopClient,
    product: Product,
    target: Target,
    *,
    floor: float,
    limit: float,
    sleep: Callable[[float], None] | None = None,
) -> Fetch:
    """Call the API for one target, retrying once after a pause on a busy site."""
    sleep = sleep or time.sleep
    result = Fetch(product, target)
    for attempt in (1, 2):
        try:
            data = request(client, target, floor, limit)
        except FATAL:
            raise
        except PERMANENT as exc:
            result.calls += 1
            result.error = f"{type(exc).__name__}: {exc}"
            result.permanent = True
            return result
        except RETRYABLE as exc:
            result.calls += 1
            log.info("%s %s: %s (attempt %d)", target.label, target.ref, exc, attempt)
            if attempt == 1:
                sleep(RETRY_PAUSE)
                continue
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        except ScrapeUnblockerError as exc:
            result.calls += 1
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        result.calls += target.calls
        if not isinstance(data, dict):
            result.error = "unexpected answer (not a JSON object)"
            return result
        result.offers, extra = NORMALIZE[target.source](data, target)
        result.whole_list = whole_list(data, target)
        if target.source in ("amazon", "idealo"):
            result.title = extra
        elif extra:
            result.note = extra
        if not result.offers and not result.note:
            result.note = "no offers in the answer"
        return result
    raise AssertionError("unreachable")
