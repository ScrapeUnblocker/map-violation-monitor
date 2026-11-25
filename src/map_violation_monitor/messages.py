"""The two messages: the alert after a run, and the weekly MAP report.

Both are built as a small :class:`Doc` (a title, intro lines and sections of items, each
with an optional link) and rendered from it: Slack mrkdwn and Discord markdown (split into
posts that fit), Teams Adaptive Card text, plain text and HTML for email, and JSON for the
webhook.
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import SOURCES, Config, Product
from .process import BACK, LOWER, NEW, Event, Finding, Opened

SOURCE = "map-violation-monitor"
_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "CAD": "CA$", "AUD": "A$"}


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def money(amount: float, currency: str) -> str:
    decimals = 0 if currency == "JPY" else 2
    text = f"{amount:,.{decimals}f}"
    symbol = _SYMBOL.get(currency)
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def day_text(when: datetime | date) -> str:
    return f"{when:%a} {when.day} {when:%b %Y}"


def short_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b}"


def span_text(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start.day} {start:%b %Y} - {end.day} {end:%b %Y}"
    if start.month != end.month:
        return f"{start.day} {start:%b} - {end.day} {end:%b %Y}"
    return f"{start.day}-{end.day} {end:%b %Y}"


def where(source: str, market: str) -> str:
    return f"{SOURCES.get(source, source)} ({market})"


def product_heading(product: Product | None, fallback: str, map_price: float, cur: str) -> str:
    name = product.name if product else fallback
    if product and product.sku and product.sku.casefold() not in name.casefold():
        name += f" ({product.sku})"
    return f"{name} · MAP {money(map_price, cur)}"


# -- the document model -------------------------------------------------------------------


@dataclass
class Item:
    text: str
    note: str = ""  # shown under the item, e.g. the listing title
    link: str = ""


@dataclass
class Section:
    heading: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Doc:
    title: str
    subtitle: str
    intro: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    outro: str = ""


def finding_text(f: Finding) -> str:
    """``eBay (ebay.com) · pd34pd: $154.99, 44.6% below MAP (3 listings)``"""
    text = f"{where(f.source, f.market)} · {f.seller}: {money(f.price, f.currency)}"
    text += f", {f.gap_percent:g}% below MAP"
    extra = []
    if f.listings > 1:
        extra.append(plural(f.listings, "listing"))
    if f.authorized:
        extra.append("authorized reseller")
    return text + (f" ({', '.join(extra)})" if extra else "")


def source_order(f: Finding) -> tuple[int, str, float, str]:
    """Amazon, Google Shopping, eBay, idealo; then the deepest undercut first."""
    rank = list(SOURCES).index(f.source) if f.source in SOURCES else len(SOURCES)
    return (rank, f.market, -f.gap_percent, f.seller.casefold())


def finding_note(f: Finding, episode: int = 1) -> str:
    parts = []
    if f.variant:
        parts.append(f"variant: {f.variant}")
    elif f.title and f.source != "amazon":
        parts.append(f"“{short(f.title, 120)}”")
    if f.shipping and f.price == f.item_price:
        parts.append(f"+ {money(f.shipping, f.currency)} shipping")
    if episode > 1:
        parts.append(f"{ordinal(episode)} time below MAP")
    return " · ".join(parts)


def finding_data(f: Finding) -> dict[str, Any]:
    return {
        "product_id": f.product,
        "source": f.source,
        "market": f.market,
        "seller": f.seller,
        "seller_id": f.seller_id or None,
        "price": f.price,
        "item_price": f.item_price,
        "shipping": f.shipping,
        "currency": f.currency,
        "map": f.map,
        "below_map_by": f.gap,
        "below_map_percent": f.gap_percent,
        "listings": f.listings,
        "authorized": f.authorized,
        "title": f.title or None,
        "variant": f.variant or None,
        "url": f.url or None,
        "seller_url": f.seller_url or None,
        "listing_id": f.listing or None,
    }


# -- the alert after a run ----------------------------------------------------------------


def event_item(e: Event) -> Item:
    f = e.finding
    if e.kind == NEW:
        text = f"🆕 {finding_text(f)}"
    else:
        was = money(e.previous or 0, f.currency)
        text = (
            f"🔻 {where(f.source, f.market)} · {f.seller}: {money(f.price, f.currency)}, "
            f"was {was} ({f.gap_percent:g}% below MAP)"
        )
    return Item(text, finding_note(f, e.episode), f.url)


@dataclass
class Alert:
    events: list[Event]
    products: dict[str, Product]
    now: datetime
    first_run: bool = False
    max_per_source: int = 8

    kind = "alert"

    @property
    def counts(self) -> Counter[str]:
        return Counter(e.kind for e in self.events)

    @property
    def empty(self) -> bool:
        return not self.events

    def _summary(self) -> str:
        c = self.counts
        parts = []
        if c[NEW]:
            parts.append(plural(c[NEW], "new seller") if not self.first_run else "")
        if c[LOWER]:
            parts.append(f"{c[LOWER]} further down")
        if c[BACK]:
            parts.append(f"{c[BACK]} back at MAP")
        return " · ".join(p for p in parts if p)

    @property
    def subject(self) -> str:
        c = self.counts
        n = len({e.finding.product for e in self.events})
        if self.first_run:
            return f"Below MAP: {plural(c[NEW], 'seller')} on {plural(n, 'product')} (first check)"
        return f"Below MAP: {self._summary()} on {plural(n, 'product')}"

    def doc(self) -> Doc:
        sub = day_text(self.now)
        sub += f" · first check, {plural(self.counts[NEW], 'seller')}" if self.first_run else ""
        summary = self._summary()
        if summary:
            sub += f" · {summary}"
        doc = Doc(title="Below MAP", subtitle=sub)
        by_product: dict[str, list[Event]] = defaultdict(list)
        for e in self.events:
            by_product[e.finding.product].append(e)
        order = {pid: i for i, pid in enumerate(self.products)}
        for pid in sorted(by_product, key=lambda p: order.get(p, len(order))):
            events = by_product[pid]
            f0 = events[0].finding
            section = Section(product_heading(self.products.get(pid), pid, f0.map, f0.currency))
            down = [e for e in events if e.kind in (NEW, LOWER)]
            down.sort(key=lambda e: source_order(e.finding))
            groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
            for e in down:
                groups[(e.finding.source, e.finding.market)].append(e)
            for (source, market), group in groups.items():
                for e in group[: self.max_per_source]:
                    section.items.append(event_item(e))
                rest = len(group) - self.max_per_source
                if rest > 0:
                    section.items.append(
                        Item(
                            f"+ {plural(rest, 'more seller')} below MAP on "
                            f"{where(source, market)}, listed in the Google Sheet and the "
                            "weekly report"
                        )
                    )
            for e in [e for e in events if e.kind == BACK]:
                f = e.finding
                section.items.append(
                    Item(
                        f"✅ {where(f.source, f.market)} · {f.seller}: back at MAP after "
                        f"{plural(max(e.days, 1), 'day')} (was {money(f.price, f.currency)})",
                        link=f.url,
                    )
                )
            doc.sections.append(section)
        if self.first_run:
            doc.outro = (
                "From now on a message arrives only when a seller newly goes below MAP or "
                "drops further."
            )
        return doc

    def data(self) -> dict[str, Any]:
        events = []
        for e in self.events:
            product = self.products.get(e.finding.product)
            events.append(
                {
                    "event": e.kind,
                    "product": product.name if product else e.finding.product,
                    "sku": (product.sku or None) if product else None,
                    **finding_data(e.finding),
                    "previous_price": e.previous,
                    "first_seen": e.first_seen,
                    "episode": e.episode,
                }
            )
        return {
            "generated_at": self.now.isoformat(),
            "first_run": self.first_run,
            "counts": {
                "new": self.counts[NEW],
                "lower": self.counts[LOWER],
                "back": self.counts[BACK],
            },
            "events": events,
        }


def build_alert(
    config: Config, events: Sequence[Event], now: datetime, *, first_run: bool, back_at_map: bool
) -> Alert:
    shown = [e for e in events if back_at_map or e.kind != BACK]
    return Alert(
        shown,
        {p.id: p for p in config.products},
        now,
        first_run=first_run,
        max_per_source=config.alerts.max_per_source,
    )


# -- the weekly report --------------------------------------------------------------------


@dataclass
class Offender:
    seller: str
    source: str
    market: str
    episodes: int
    products: int
    open_now: int


@dataclass
class ReportMessage:
    products: dict[str, Product]
    opened: list[Opened]
    back: list[dict[str, Any]]
    offenders: list[Offender]
    now: datetime
    since: date

    kind = "report"

    @property
    def week(self) -> str:
        year, week, _ = self.now.date().isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def new(self) -> list[Opened]:
        return [o for o in self.opened if o.first_seen >= self.since.isoformat()]

    @property
    def subject(self) -> str:
        n = len({o.finding.product for o in self.opened})
        head = f"MAP report · week {self.week.split('-W')[1]}"
        if not self.opened:
            return f"{head}: no seller below MAP"
        text = f"{head}: {plural(len(self.opened), 'seller')} below MAP on {plural(n, 'product')}"
        return text + (f", {len(self.new)} new" if self.new else "")

    def doc(self) -> Doc:
        end = self.now.date()
        doc = Doc(
            title=f"MAP report · week {self.week.split('-W')[1]}",
            subtitle=f"{span_text(self.since, end)} · "
            + (
                f"{plural(len(self.opened), 'seller')} below MAP now"
                if self.opened
                else "no seller below MAP now"
            ),
        )
        doc.intro.append(
            f"New this week: {len(self.new)} · back at MAP: {len(self.back)} · "
            f"repeat offenders: {len(self.offenders)}"
        )
        per_source = Counter(o.finding.source for o in self.opened)
        if per_source:
            doc.intro.append(
                " · ".join(f"{SOURCES[s]} {per_source[s]}" for s in SOURCES if per_source[s])
            )
        by_product: dict[str, list[Opened]] = defaultdict(list)
        for o in self.opened:
            by_product[o.finding.product].append(o)
        order = {pid: i for i, pid in enumerate(self.products)}
        for pid in sorted(by_product, key=lambda p: order.get(p, len(order))):
            items = sorted(by_product[pid], key=lambda o: source_order(o.finding))
            f0 = items[0].finding
            heading = product_heading(self.products.get(pid), pid, f0.map, f0.currency)
            section = Section(f"{heading} · {plural(len(items), 'seller')} below")
            for o in items:
                f = o.finding
                mark = "🆕 " if o.first_seen >= self.since.isoformat() else ""
                lowest = (
                    f", lowest {money(o.lowest, f.currency)}" if o.lowest < f.price - 0.005 else ""
                )
                section.items.append(
                    Item(
                        f"{mark}{finding_text(f)} · since {short_day(o.first_seen)}{lowest}",
                        f"{ordinal(o.episode)} time below MAP" if o.episode > 1 else "",
                        f.url,
                    )
                )
            doc.sections.append(section)
        if self.back:
            section = Section("Back at MAP this week")
            for b in sorted(self.back, key=lambda b: (b.get("on", ""), b.get("seller", ""))):
                product = self.products.get(str(b.get("product")))
                name = product.name if product else str(b.get("product"))
                days = plural(max(int(b.get("days") or 0), 1), "day")
                last = money(float(b.get("last_price") or 0), str(b.get("currency") or ""))
                section.items.append(
                    Item(
                        f"{where(str(b['source']), str(b['market']))} · {b.get('seller')}: "
                        f"{name}, back on {short_day(str(b['on']))} after {days} (was {last})"
                    )
                )
            doc.sections.append(section)
        if self.offenders:
            section = Section("Repeat offenders (12 months)")
            for o in self.offenders:
                now = f", below MAP now on {o.open_now}" if o.open_now else ""
                section.items.append(
                    Item(
                        f"{o.seller} · {where(o.source, o.market)}: "
                        f"{plural(o.episodes, 'time')} below MAP on "
                        f"{plural(o.products, 'product')}{now}"
                    )
                )
            doc.sections.append(section)
        doc.outro = "Every finding, with its link and dates, is logged in the Google Sheet."
        return doc

    def data(self) -> dict[str, Any]:
        def opened(o: Opened) -> dict[str, Any]:
            product = self.products.get(o.finding.product)
            return {
                "product": product.name if product else o.finding.product,
                "sku": (product.sku or None) if product else None,
                **finding_data(o.finding),
                "first_seen": o.first_seen,
                "last_seen": o.last_seen,
                "lowest": o.lowest,
                "episode": o.episode,
            }

        return {
            "generated_at": self.now.isoformat(),
            "week": self.week,
            "since": self.since.isoformat(),
            "below_map": [opened(o) for o in self.opened],
            "back_at_map": self.back,
            "repeat_offenders": [o.__dict__ for o in self.offenders],
        }

    @property
    def empty(self) -> bool:
        return False


def offenders(
    history: dict[str, dict[str, Any]], opened: dict[str, Opened], minimum: int = 2
) -> list[Offender]:
    """Sellers that went below MAP ``minimum`` or more times (on any of your products)."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, h in history.items():
        product, source, market, seller_key = key.split("|", 3)
        g = groups.setdefault(
            (source, market, seller_key),
            {"seller": h.get("seller") or seller_key, "episodes": 0, "products": set()},
        )
        g["episodes"] += int(h.get("episodes") or 0)
        g["products"].add(product)
    out = []
    for (source, market, seller_key), g in groups.items():
        if g["episodes"] < minimum:
            continue
        now = sum(
            1
            for o in opened.values()
            if (o.finding.source, o.finding.market, o.finding.seller_key)
            == (source, market, seller_key)
        )
        out.append(Offender(g["seller"], source, market, g["episodes"], len(g["products"]), now))
    out.sort(key=lambda o: (-o.episodes, -o.open_now, o.seller.casefold()))
    return out


def build_report(
    config: Config,
    opened: dict[str, Opened],
    history: dict[str, dict[str, Any]],
    back: Sequence[dict[str, Any]],
    now: datetime,
) -> ReportMessage:
    since = now.date() - timedelta(days=6)
    ids = {p.id for p in config.products}
    return ReportMessage(
        products={p.id: p for p in config.products},
        opened=[o for o in opened.values() if o.finding.product in ids],
        back=[b for b in back if str(b.get("on") or "") >= since.isoformat()],
        offenders=offenders(history, opened),
        now=now,
        since=since,
    )


# -- rendering ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    name: str
    bold: Callable[[str], str]
    note: Callable[[str], str]
    escape: Callable[[str], str]
    link: Callable[[str, str], str]  # (url, label) -> markup
    limit: int


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_DISCORD_SPECIAL = "\\*_~`|[]"


def _discord_escape(text: str) -> str:
    return "".join("\\" + c if c in _DISCORD_SPECIAL else c for c in text)


_TEAMS_SPECIAL = "\\*_[]`"


def _teams_escape(text: str) -> str:
    return "".join("\\" + c if c in _TEAMS_SPECIAL else c for c in text)


SLACK = Style(
    name="slack",
    bold=lambda t: f"*{t}*",
    note=lambda t: f"      _{t}_",
    escape=_slack_escape,
    link=lambda url, label: f"<{url.replace('|', '%7C').replace('>', '%3E')}|{label}>",
    limit=3900,
)
DISCORD = Style(
    name="discord",
    bold=lambda t: f"**{t}**",
    note=lambda t: f"-# {t}",
    escape=_discord_escape,
    link=lambda url, label: f"[{label}](<{url}>)",
    limit=1990,
)
TEAMS = Style(
    name="teams",
    bold=lambda t: f"**{t}**",
    note=lambda t: f"    _{t}_",
    escape=_teams_escape,
    link=lambda url, label: f"[{label}]({url.replace(')', '%29').replace(' ', '%20')})",
    limit=20_000,
)
PLAIN = Style(
    name="plain",
    bold=lambda t: t,
    note=lambda t: f"  {t}",
    escape=lambda t: t,
    link=lambda url, label: f"\n  {url}",
    limit=100_000,
)


def blocks(doc: Doc, style: Style) -> list[str]:
    e = style.escape
    head = [f"🏷️ {style.bold(e(doc.title))} · {e(doc.subtitle)}"]
    if doc.intro:
        head.append("")
        head += [e(line) for line in doc.intro]
    out = ["\n".join(head)]
    for section in doc.sections:
        out.append("\n" + style.bold(e(section.heading)))
        for item in section.items:
            text = f"• {e(item.text)}"
            if item.link:
                sep = "" if style is PLAIN else " · "
                text += sep + style.link(item.link, "view")
            if item.note:
                text += "\n" + style.note(e(item.note))
            out.append(text)
    if doc.outro:
        out.append("\n" + e(doc.outro))
    return out


def pack(parts: Sequence[str], limit: int) -> list[str]:
    """Join blocks into as few posts as fit ``limit`` characters each."""
    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(part) > limit:
            part = part[: limit - 1] + "…"
        candidate = f"{current}\n{part}" if current else part.lstrip("\n")
        if len(candidate) > limit:
            chunks.append(current)
            current = part.lstrip("\n")
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def render(doc: Doc, style: Style) -> list[str]:
    """The message as one or more posts for a chat destination."""
    return pack(blocks(doc, style), style.limit)


def render_plain(doc: Doc) -> str:
    return "\n".join(blocks(doc, PLAIN)).strip() + "\n"


TEAMS_CARD_TEXT = 18_000  # Teams webhooks take about 28 KB per card; leave room for JSON


def teams_cards(doc: Doc) -> list[dict[str, Any]]:
    """Adaptive Cards for a Teams Workflows webhook: one TextBlock per block, split into
    more cards when the message is long."""
    cards: list[list[dict[str, Any]]] = [[]]
    size = 0
    for i, block in enumerate(blocks(doc, TEAMS)):
        text = block.strip("\n").replace("\n", "\n\n")
        if cards[-1] and size + len(text) > TEAMS_CARD_TEXT:
            cards.append([])
            size = 0
        tb: dict[str, Any] = {"type": "TextBlock", "text": text[:TEAMS_CARD_TEXT], "wrap": True}
        if i == 0:
            tb["size"] = "Medium"
        elif block.startswith("\n**"):
            tb["spacing"] = "Medium"
        cards[-1].append(tb)
        size += len(text)
    return [
        {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "msteams": {"width": "Full"},
                        "body": body,
                    },
                }
            ],
        }
        for body in cards
    ]


_MUTED = "color:#59636e"


def render_html(doc: Doc) -> str:
    e = html.escape
    out = [
        "<div style=\"font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        'max-width:720px;color:#1f2328;line-height:1.45">',
        f'<h2 style="margin:0 0 2px">{e(doc.title)}</h2>',
        f'<p style="margin:0 0 14px;{_MUTED}">{e(doc.subtitle)}</p>',
    ]
    for line in doc.intro:
        out.append(f'<p style="margin:0 0 6px">{e(line)}</p>')
    for section in doc.sections:
        out.append(
            '<h3 style="margin:20px 0 8px;padding-bottom:4px;border-bottom:1px solid #d1d9e0;'
            f'font-size:15px">{e(section.heading)}</h3>'
        )
        out.append('<ul style="margin:0;padding-left:18px">')
        for item in section.items:
            link = (
                f' · <a href="{e(item.link, quote=True)}" style="color:#0969da">view</a>'
                if item.link
                else ""
            )
            note = (
                f'<br><span style="font-size:13px;{_MUTED};font-style:italic">{e(item.note)}</span>'
                if item.note
                else ""
            )
            out.append(f'<li style="margin:0 0 8px">{e(item.text)}{link}{note}</li>')
        out.append("</ul>")
    if doc.outro:
        out.append(f'<p style="margin:18px 0 0;font-size:13px;{_MUTED}">{e(doc.outro)}</p>')
    out.append("</div>")
    return "\n".join(out) + "\n"
