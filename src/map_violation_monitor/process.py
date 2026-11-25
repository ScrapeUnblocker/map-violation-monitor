"""Compare every offer with your MAP and work out what is new since the last run.

1. :func:`check` keeps the offers that are this product (new condition, title terms,
   your currency, above the parts-and-accessories floor), drops ignored sellers, and
   groups what is left by seller: one :class:`Finding` per seller below MAP, at its
   lowest price.
2. :func:`compare` sets those findings against the open ones in the state file:
   a seller newly below MAP, or one that went further down, is an :class:`Event` to
   deliver. A seller no longer below MAP in an answer that lists every seller (Amazon,
   idealo), or not seen below MAP for ``resolve_after_days`` (eBay and Google Shopping,
   which show each visitor a different selection), is back at MAP.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .collect import Fetch, Offer
from .config import Product, Rules, Sellers, Target

# Sources whose answers list offers strictly cheapest first. (eBay's price sort is loose,
# so eBay is asked for the price band below MAP instead, see collect.request.)
PRICE_SORTED = ("idealo",)

NEW = "new"
LOWER = "lower"
BACK = "back at MAP"


@dataclass
class Finding:
    """A seller advertising a product below MAP at one source and market."""

    product: str  # Product.id
    source: str
    market: str
    seller: str
    seller_id: str
    seller_key: str
    price: float  # the compared price: the item, or the item plus shipping
    item_price: float
    shipping: float | None
    currency: str
    map: float
    title: str = ""
    url: str = ""
    seller_url: str = ""
    listing: str = ""
    listings: int = 1  # this seller's listings below MAP in this answer
    authorized: bool = False
    variant: str = ""

    @property
    def key(self) -> str:
        return finding_key(self.product, self.source, self.market, self.seller_key)

    @property
    def gap(self) -> float:
        return round(self.map - self.price, 2)

    @property
    def gap_percent(self) -> float:
        return round(100 * (self.map - self.price) / self.map, 1) if self.map else 0.0


def finding_key(product: str, source: str, market: str, seller_key: str) -> str:
    return f"{product}|{source}|{market}|{seller_key}"


@dataclass
class Checked:
    """The result for one target: who is below MAP, and who was seen at or above it."""

    product: Product
    target: Target
    findings: list[Finding] = field(default_factory=list)
    at_map: set[str] = field(default_factory=set)  # seller keys seen only at/above MAP
    offers: int = 0  # offers that are this product
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    answered: bool = False  # the source answered with offers
    whole_list: bool = False  # the answer holds every offer the source has
    reached_map: bool = False  # an offer of this product at or above MAP was read

    @property
    def complete(self) -> bool:
        """Every seller below MAP is in this answer, so a seller missing from it is no
        longer below MAP: the source listed all its offers (Amazon with no further page,
        idealo with fewer than max_offers), or it lists cheapest first (idealo) and the
        answer reached MAP. eBay and Google Shopping answers are samples, never complete."""
        if not self.answered:
            return False
        return self.whole_list or (self.target.source in PRICE_SORTED and self.reached_map)

    @property
    def all_below(self) -> bool:
        """Every offer read is below MAP and there are more: the next page may hold more
        sellers below MAP."""
        return bool(self.findings) and not self.whole_list and not self.reached_map

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def is_product(offer: Offer, product: Product, rules: Rules) -> str:
    """``""`` when the offer is a new unit of this product, else the reason it is not."""
    if offer.condition and offer.condition != "new":
        return "not new"
    if offer.source == "amazon":
        return ""  # Amazon offers belong to the exact ASIN; the title is the product's
    title = f"{offer.title} {offer.variant}"
    if any(not t.found_in(title) for t in product.match):
        return "other product"
    if any(t.found_in(title) for t in (*product.exclude, *rules.exclude)):
        return "excluded words"
    return ""


def compared_price(offer: Offer, rules: Rules) -> float:
    if rules.include_shipping and offer.shipping:
        return round(offer.price + offer.shipping, 2)
    return offer.price


def check(fetch: Fetch, rules: Rules, sellers: Sellers) -> Checked:
    product, target = fetch.product, fetch.target
    out = Checked(product, target, answered=fetch.ok, whole_list=fetch.whole_list)
    limit = rules.limit(product.map)
    floor = rules.floor(product.map)
    best: dict[str, Finding] = {}
    for offer in fetch.offers:
        if offer.currency and offer.currency != product.currency:
            out.skip("other currency")
            continue
        reason = is_product(offer, product, rules)
        if reason:
            out.skip(reason)
            continue
        if sellers.ignored(offer.seller, offer.seller_id):
            out.skip("ignored seller")
            continue
        price = compared_price(offer, rules)
        if price < floor:
            out.skip("below the floor")
            continue
        out.offers += 1
        key = offer.seller_key
        if price >= limit:
            out.at_map.add(key)
            out.reached_map = True
            continue
        found = best.get(key)
        if found is not None:
            found.listings += 1
            if price >= found.price:
                continue
            listings = found.listings
        else:
            listings = 1
        best[key] = Finding(
            product=product.id,
            source=offer.source,
            market=offer.market,
            seller=offer.seller,
            seller_id=offer.seller_id,
            seller_key=key,
            price=price,
            item_price=offer.price,
            shipping=offer.shipping,
            currency=product.currency,
            map=product.map,
            title=offer.title,
            url=offer.url,
            seller_url=offer.seller_url,
            listing=offer.listing,
            listings=listings,
            authorized=sellers.is_authorized(offer.seller, offer.seller_id),
            variant=offer.variant,
        )
    out.at_map -= set(best)
    # An answer with nothing of this product in it (another country's page, suggestions)
    # proves nothing, so it never closes a finding.
    out.answered = out.answered and out.offers > 0
    out.findings = sorted(best.values(), key=lambda f: (f.price, f.seller.casefold()))
    return out


# -- against the last run -------------------------------------------------------------------


@dataclass
class Event:
    """Something to deliver: a seller newly below MAP, one that went further below it, or
    one back at MAP."""

    kind: str  # NEW, LOWER or BACK
    finding: Finding
    first_seen: str  # ISO date
    previous: float | None = None  # LOWER: the price last alerted; BACK: the last price seen
    episode: int = 1  # how many times this seller has gone below MAP on this product
    days: int = 0  # BACK: days it was below MAP


@dataclass
class Opened:
    """An open finding, as kept in the state file."""

    finding: Finding
    first_seen: str
    last_seen: str
    alerted_price: float
    lowest: float
    episode: int = 1

    def to_json(self) -> dict[str, Any]:
        f = self.finding
        return {
            "product": f.product,
            "source": f.source,
            "market": f.market,
            "seller": f.seller,
            "seller_id": f.seller_id,
            "seller_key": f.seller_key,
            "price": f.price,
            "item_price": f.item_price,
            "shipping": f.shipping,
            "currency": f.currency,
            "map": f.map,
            "title": f.title,
            "url": f.url,
            "seller_url": f.seller_url,
            "listing": f.listing,
            "listings": f.listings,
            "authorized": f.authorized,
            "variant": f.variant,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "alerted_price": self.alerted_price,
            "lowest": self.lowest,
            "episode": self.episode,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Opened:
        price = float(raw["price"])
        finding = Finding(
            product=str(raw["product"]),
            source=str(raw["source"]),
            market=str(raw["market"]),
            seller=str(raw.get("seller") or ""),
            seller_id=str(raw.get("seller_id") or ""),
            seller_key=str(raw["seller_key"]),
            price=price,
            item_price=float(raw.get("item_price", price)),
            shipping=raw.get("shipping"),
            currency=str(raw.get("currency") or ""),
            map=float(raw.get("map") or 0),
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or ""),
            seller_url=str(raw.get("seller_url") or ""),
            listing=str(raw.get("listing") or ""),
            listings=int(raw.get("listings") or 1),
            authorized=bool(raw.get("authorized")),
            variant=str(raw.get("variant") or ""),
        )
        return cls(
            finding=finding,
            first_seen=str(raw["first_seen"]),
            last_seen=str(raw.get("last_seen") or raw["first_seen"]),
            alerted_price=float(raw.get("alerted_price", price)),
            lowest=float(raw.get("lowest", price)),
            episode=int(raw.get("episode") or 1),
        )


def target_of(finding: Finding, target: Target) -> bool:
    return finding.source == target.source and finding.market == target.market


def compare(
    checked: Sequence[Checked],
    opened: dict[str, Opened],
    rules: Rules,
    today: date,
    episodes: dict[str, int],
) -> tuple[list[Event], dict[str, Opened]]:
    """The events of this run and the open findings after it.

    ``episodes`` maps a finding key to how many times that seller went below MAP on that
    product before; it is only read here.
    """
    day = today.isoformat()
    after = dict(opened)
    events: list[Event] = []
    found: dict[str, Finding] = {}
    for c in checked:
        for f in c.findings:
            # One seller can turn up from two targets of the same source (two ASINs of a
            # product); keep its lowest price.
            if f.key not in found or f.price < found[f.key].price:
                found[f.key] = f
    for key, f in found.items():
        old = after.get(key)
        if old is None:
            episode = episodes.get(key, 0) + 1
            after[key] = Opened(f, day, day, f.price, f.price, episode)
            events.append(Event(NEW, f, day, episode=episode))
            continue
        drop = rules.realert_drop_percent / 100
        lower = f.price < old.alerted_price * (1 - drop) - 1e-9 and f.price < old.alerted_price
        if lower:
            events.append(Event(LOWER, f, old.first_seen, old.alerted_price, old.episode))
        after[key] = Opened(
            f,
            old.first_seen,
            day,
            f.price if lower else old.alerted_price,
            min(old.lowest, f.price),
            old.episode,
        )

    # Back at MAP: not below MAP in an answer that lists every seller below MAP (Amazon,
    # idealo), or not seen below MAP for resolve_after_days. eBay and Google Shopping show
    # each visitor a different selection, so a seller missing there, or seen there with
    # only a listing at MAP, may still have another listing below it.
    stale = (today - timedelta(days=rules.resolve_after_days)).isoformat()
    for key, old in list(after.items()):
        if key in found:
            continue
        f = old.finding
        answered = [c for c in checked if c.product.id == f.product and target_of(f, c.target)]
        if not any(c.answered for c in answered):
            continue  # no fresh answer from this source: nothing is known
        if any(c.complete for c in answered) or old.last_seen <= stale:
            del after[key]
            days = (today - date.fromisoformat(old.first_seen)).days
            events.append(Event(BACK, f, old.first_seen, f.price, old.episode, days))
    return events, after


def drop_unknown(opened: dict[str, Opened], products: Iterable[Product]) -> dict[str, Opened]:
    """Forget the open findings of products no longer in config.yml."""
    ids = {p.id for p in products}
    return {k: v for k, v in opened.items() if v.finding.product in ids}
