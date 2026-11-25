"""Load and check ``config.yml``, the one file a user edits.

Secrets never live here: the API key, webhook URLs, SMTP login and the Google service
account come from environment variables.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Source id -> the name people know it by.
SOURCES = {
    "amazon": "Amazon",
    "google_shopping": "Google Shopping",
    "ebay": "eBay",
    "idealo": "idealo",
}
ALERT_DESTINATIONS = ("slack", "teams", "discord", "webhook", "email")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

AMAZON_MARKETS = (
    "amazon.com",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.nl",
    "amazon.se",
    "amazon.pl",
    "amazon.com.be",
    "amazon.com.tr",
    "amazon.ca",
    "amazon.com.mx",
    "amazon.com.br",
    "amazon.co.jp",
    "amazon.in",
    "amazon.com.au",
    "amazon.sg",
    "amazon.ae",
    "amazon.sa",
)
EBAY_MARKETS = (
    "ebay.com",
    "ebay.co.uk",
    "ebay.de",
    "ebay.fr",
    "ebay.it",
    "ebay.es",
    "ebay.nl",
    "ebay.pl",
    "ebay.at",
    "ebay.ch",
    "ebay.be",
    "ebay.ie",
    "ebay.ca",
    "ebay.com.au",
    "ebay.in",
    "ebay.ph",
    "ebay.com.sg",
    "ebay.com.my",
    "ebay.com.hk",
)
IDEALO_COUNTRIES = ("de", "at", "uk", "fr", "it", "es")
EBAY_LISTING_TYPES = ("buy_it_now", "all", "auction")

_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_TAB = re.compile(r"^[^\[\]*?/\\:]{1,100}$")
_DIGIT = re.compile(r"\d")

# Words in a listing title that mean it is not a new unit of the product. MAP covers new
# goods; these are never checked. Add your own under rules.exclude.
DEFAULT_EXCLUDE = (
    "refurbished",
    "renewed",
    "remanufactured",
    "used",
    "pre-owned",
    "open box",
    "for parts",
    "not working",
    "replacement",
    "compatible with",
    "grade a",
    "grade b",
    "grade c",
    "gebraucht",
    "generalüberholt",
    "b-ware",
    "reconditionné",
    "reacondicionado",
    "ricondizionato",
)


class ConfigError(ValueError):
    """config.yml is missing, unreadable or has a value that cannot work."""


# -- title matching -----------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case, accents kept, every kind of dash and space made plain."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def compact(text: str) -> str:
    """``"WH-1000XM5"``, ``"wh 1000 xm5"`` and ``"WH‑1000XM5"`` all become ``"wh1000xm5"``."""
    return fold(text).replace(" ", "")


@dataclass(frozen=True)
class Term:
    """A word or model number to find in a listing title.

    A term with a digit in it (a model number) matches anywhere in the title, ignoring
    case, spaces and dashes, so ``WH-1000XM5`` matches ``SONY WH 1000 XM 5 B``. A term
    without digits matches whole words only, so ``used`` does not match ``unused``.
    """

    text: str

    @property
    def model(self) -> bool:
        return bool(_DIGIT.search(self.text))

    def found_in(self, title: str) -> bool:
        if self.model:
            needle = compact(self.text)
            return bool(needle) and needle in compact(title)
        needle = fold(self.text)
        return bool(needle) and f" {needle} " in f" {fold(title)} "


# -- the settings -------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One place a product is sold: an Amazon listing, a Google Shopping search, an eBay
    search or an idealo price comparison."""

    source: str
    market: str  # amazon.com, us, ebay.com, de
    ref: str  # the ASIN, search words or idealo product id
    proxy_country: str = ""
    pages: int = 1  # Amazon offer pages (10 offers each)
    max_offers: int = 20  # idealo offers, or eBay listings per search (60, 120, 240)
    listing_type: str = "buy_it_now"

    @property
    def label(self) -> str:
        return f"{SOURCES[self.source]} ({self.market})"

    @property
    def id(self) -> str:
        return f"{self.source}|{self.market}|{self.ref.casefold()}"

    @property
    def calls(self) -> int:
        """API calls this target costs per run."""
        if self.source == "amazon":
            return self.pages
        if self.source == "idealo":
            return 1 + (self.max_offers - 1) // 20
        return 1


@dataclass(frozen=True)
class Product:
    name: str
    map: float
    currency: str
    targets: tuple[Target, ...]
    sku: str = ""
    match: tuple[Term, ...] = ()
    exclude: tuple[Term, ...] = ()

    @property
    def id(self) -> str:
        """Stable id in the state file: the name, folded. Renaming makes a new product."""
        return compact(self.name)[:60] or "product"

    @property
    def calls(self) -> int:
        return sum(t.calls for t in self.targets)


@dataclass(frozen=True)
class Rules:
    tolerance_percent: float = 0.5
    floor_percent: float = 50.0
    include_shipping: bool = False
    realert_drop_percent: float = 3.0
    resolve_after_days: int = 7
    exclude: tuple[Term, ...] = tuple(Term(w) for w in DEFAULT_EXCLUDE)

    def limit(self, map_price: float) -> float:
        """Prices below this are below MAP."""
        return map_price * (1 - self.tolerance_percent / 100)

    def floor(self, map_price: float) -> float:
        """Prices below this are not the product (parts, accessories, a wrong match)."""
        return map_price * self.floor_percent / 100


@dataclass(frozen=True)
class Sellers:
    ignore: tuple[str, ...] = ()  # names or seller ids; never reported
    authorized: tuple[str, ...] = ()  # names or seller ids; reported, marked authorized

    @staticmethod
    def _hit(names: tuple[str, ...], name: str, seller_id: str) -> bool:
        folded = fold(name)
        for entry in names:
            want = fold(entry)
            if not want:
                continue
            if seller_id and entry.strip().casefold() == seller_id.casefold():
                return True
            if f" {want} " in f" {folded} ":
                return True
        return False

    def ignored(self, name: str, seller_id: str = "") -> bool:
        return self._hit(self.ignore, name, seller_id)

    def is_authorized(self, name: str, seller_id: str = "") -> bool:
        return self._hit(self.authorized, name, seller_id)


@dataclass(frozen=True)
class Alerts:
    to: tuple[str, ...] = ("slack", "teams", "discord", "webhook")
    max_per_source: int = 8  # sellers listed per product and source in one alert
    back_at_map: bool = False  # also alert when a seller is back at MAP (always in the report)


@dataclass(frozen=True)
class Report:
    day: str = "monday"  # or "off"
    to: tuple[str, ...] = ("email",)

    @property
    def weekday(self) -> int | None:
        """0 = Monday, None = no weekly report."""
        return None if self.day == "off" else DAYS.index(self.day)


@dataclass(frozen=True)
class Sheet:
    enabled: bool = True
    tab: str = "Findings"


@dataclass(frozen=True)
class Config:
    products: tuple[Product, ...]
    rules: Rules = field(default_factory=Rules)
    sellers: Sellers = field(default_factory=Sellers)
    alerts: Alerts = field(default_factory=Alerts)
    report: Report = field(default_factory=Report)
    sheet: Sheet = field(default_factory=Sheet)
    state: Path = Path("state/findings.json")

    @property
    def calls(self) -> int:
        return sum(p.calls for p in self.products)

    def find_products(self, text: str) -> list[Product]:
        wanted = fold(text)
        return [p for p in self.products if wanted in fold(p.name) or wanted == fold(p.sku)]


# -- parsing ------------------------------------------------------------------------------


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected a whole number, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{where}: must be {low}-{high}, got {value}")
    return value


def _number(value: Any, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{where}: must be {low:g}-{high:g}, got {value}")
    return float(value)


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected text, got {value!r}")
    return " ".join(value.split())


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str | dict | int) and not isinstance(value, bool):
        return [value]  # one entry written without the list brackets
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list")
    return value


def _mapping(raw: dict[str, Any], key: str, where: str = "") -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where or key}: expected a section of settings")
    return value


def _unknown(section: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(str(k) for k in set(section) - allowed)
    if extra:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(extra)}")


def _terms(value: Any, where: str) -> tuple[Term, ...]:
    out: list[Term] = []
    for i, item in enumerate(_list(value, where)):
        text = _text(item, f"{where}[{i}]")
        if not fold(text):
            raise ConfigError(f"{where}[{i}]: empty entry")
        out.append(Term(text))
    return tuple(out)


def _names(value: Any, where: str) -> tuple[str, ...]:
    out: list[str] = []
    for i, item in enumerate(_list(value, where)):
        text = _text(item, f"{where}[{i}]")
        if not fold(text):
            raise ConfigError(f"{where}[{i}]: empty entry")
        out.append(text)
    return tuple(dict.fromkeys(out))


def _choice(value: Any, where: str, choices: tuple[str, ...], default: str) -> str:
    text = _text(value, where).lower() if value is not None else default
    if text not in choices:
        raise ConfigError(f"{where}: expected one of {', '.join(choices)}, got {text!r}")
    return text


def _country(value: Any, where: str, *, required: bool = False) -> str:
    text = _text(value, where)
    if not text:
        if required:
            raise ConfigError(f"{where}: a 2-letter country code such as us is required")
        return ""
    if not _COUNTRY.match(text):
        raise ConfigError(f"{where}: expected a 2-letter country code such as us, got {text!r}")
    return text.lower()


def parse_target(source: str, raw: Any, where: str) -> Target:
    if source == "amazon":
        if isinstance(raw, str):
            raw = {"asin": raw}
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected {{asin: ..., marketplace: amazon.com}}")
        _unknown(raw, {"asin", "marketplace", "pages", "proxy_country"}, where)
        asin = _text(raw.get("asin"), f"{where}.asin").upper()
        if not _ASIN.match(asin):
            raise ConfigError(f"{where}.asin: expected a 10-character ASIN, got {asin!r}")
        market = _choice(
            raw.get("marketplace"), f"{where}.marketplace", AMAZON_MARKETS, "amazon.com"
        )
        return Target(
            "amazon",
            market,
            asin,
            proxy_country=_country(raw.get("proxy_country"), f"{where}.proxy_country"),
            pages=_int(raw.get("pages", 1), f"{where}.pages", 1, 5),
        )
    if source == "google_shopping":
        if isinstance(raw, str):
            raw = {"query": raw}
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected {{query: ..., country: us}}")
        _unknown(raw, {"query", "country", "proxy_country"}, where)
        query = _text(raw.get("query"), f"{where}.query")
        if not query:
            raise ConfigError(f"{where}.query: the search words are required")
        return Target(
            "google_shopping",
            _country(raw.get("country"), f"{where}.country", required=True),
            query,
            proxy_country=_country(raw.get("proxy_country"), f"{where}.proxy_country"),
        )
    if source == "ebay":
        if isinstance(raw, str):
            raw = {"keyword": raw}
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected {{keyword: ..., marketplace: ebay.com}}")
        _unknown(
            raw, {"keyword", "marketplace", "listing_type", "page_size", "proxy_country"}, where
        )
        keyword = _text(raw.get("keyword"), f"{where}.keyword")
        if not keyword:
            raise ConfigError(f"{where}.keyword: the search words are required")
        size = raw.get("page_size", 240)
        if size not in (60, 120, 240):
            raise ConfigError(f"{where}.page_size: eBay pages hold 60, 120 or 240, got {size!r}")
        return Target(
            "ebay",
            _choice(raw.get("marketplace"), f"{where}.marketplace", EBAY_MARKETS, "ebay.com"),
            keyword,
            proxy_country=_country(raw.get("proxy_country"), f"{where}.proxy_country"),
            max_offers=size,
            listing_type=_choice(
                raw.get("listing_type"), f"{where}.listing_type", EBAY_LISTING_TYPES, "buy_it_now"
            ),
        )
    # idealo
    if isinstance(raw, str | int) and not isinstance(raw, bool):
        raw = {"product_id": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected {{product_id: ..., country: de}}")
    _unknown(raw, {"product_id", "country", "max_offers"}, where)
    product_id = _text(raw.get("product_id"), f"{where}.product_id")
    if not product_id.isdigit():
        raise ConfigError(
            f"{where}.product_id: expected the number in an idealo price comparison link, "
            f"got {product_id!r}"
        )
    return Target(
        "idealo",
        _choice(raw.get("country"), f"{where}.country", IDEALO_COUNTRIES, "de"),
        product_id,
        max_offers=_int(raw.get("max_offers", 20), f"{where}.max_offers", 1, 200),
    )


PRODUCT_KEYS = {"name", "sku", "map", "currency", "match", "exclude", *SOURCES}


def parse_product(raw: Any, where: str) -> Product:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping with name, map, currency and sources")
    _unknown(raw, PRODUCT_KEYS, where)
    name = _text(raw.get("name"), f"{where}.name")
    if not name:
        raise ConfigError(f"{where}.name: the product name is required")
    if raw.get("map") is None:
        raise ConfigError(f"{where}.map: your Minimum Advertised Price is required")
    map_price = _number(raw.get("map"), f"{where}.map", 0.01, 10_000_000)
    currency = _text(raw.get("currency"), f"{where}.currency").upper()
    if not _CURRENCY.match(currency):
        raise ConfigError(f"{where}.currency: expected a currency code such as USD or EUR")
    targets: list[Target] = []
    for source in SOURCES:
        for i, item in enumerate(_list(raw.get(source), f"{where}.{source}")):
            target = parse_target(source, item, f"{where}.{source}[{i}]")
            if any(t.id == target.id for t in targets):
                raise ConfigError(f"{where}.{source}[{i}]: listed twice")
            targets.append(target)
    if not targets:
        raise ConfigError(
            f"{where}: add at least one place it is sold: {', '.join(SOURCES)} (see README)"
        )
    match = _terms(raw.get("match"), f"{where}.match")
    if not match and any(t.source in ("google_shopping", "ebay") for t in targets):
        raise ConfigError(
            f"{where}.match: searches return other products too; list the model number or "
            "words every real listing has in its title, e.g. match: [WH-1000XM5]"
        )
    return Product(
        name=name,
        map=round(map_price, 2),
        currency=currency,
        targets=tuple(targets),
        sku=_text(raw.get("sku"), f"{where}.sku"),
        match=match,
        exclude=_terms(raw.get("exclude"), f"{where}.exclude"),
    )


def _destinations(value: Any, where: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = _list(value, where)
    bad = [str(d) for d in items if d not in ALERT_DESTINATIONS]
    if bad:
        raise ConfigError(
            f"{where}: unknown destination(s) {', '.join(bad)}; use {', '.join(ALERT_DESTINATIONS)}"
        )
    return tuple(dict.fromkeys(items))


TOP_KEYS = {"products", "rules", "sellers", "alerts", "report", "sheet", "state"}
RULE_KEYS = {
    "tolerance_percent",
    "floor_percent",
    "include_shipping",
    "realert_drop_percent",
    "resolve_after_days",
    "exclude",
    "keep_default_exclude",
}


def parse_config(raw: Any, base_dir: Path | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.yml must be a mapping of settings")
    _unknown(raw, TOP_KEYS, "config.yml")

    products_raw = raw.get("products")
    if not isinstance(products_raw, list) or not products_raw:
        raise ConfigError("products: expected a list of products with their MAP (see README)")
    products: list[Product] = []
    for i, item in enumerate(products_raw):
        product = parse_product(item, f"products[{i}]")
        if any(p.id == product.id for p in products):
            raise ConfigError(f"products[{i}]: another product has the name {product.name!r}")
        products.append(product)

    rules_raw = _mapping(raw, "rules")
    _unknown(rules_raw, RULE_KEYS, "rules")
    keep = _bool(rules_raw.get("keep_default_exclude", True), "rules.keep_default_exclude")
    extra = _terms(rules_raw.get("exclude"), "rules.exclude")
    base = tuple(Term(w) for w in DEFAULT_EXCLUDE) if keep else ()
    rules = Rules(
        tolerance_percent=_number(
            rules_raw.get("tolerance_percent", 0.5), "rules.tolerance_percent", 0, 50
        ),
        floor_percent=_number(rules_raw.get("floor_percent", 50), "rules.floor_percent", 0, 99),
        include_shipping=_bool(rules_raw.get("include_shipping", False), "rules.include_shipping"),
        realert_drop_percent=_number(
            rules_raw.get("realert_drop_percent", 3), "rules.realert_drop_percent", 0, 90
        ),
        resolve_after_days=_int(
            rules_raw.get("resolve_after_days", 7), "rules.resolve_after_days", 1, 60
        ),
        exclude=base + extra,
    )

    sellers_raw = _mapping(raw, "sellers")
    _unknown(sellers_raw, {"ignore", "authorized"}, "sellers")
    sellers = Sellers(
        ignore=_names(sellers_raw.get("ignore"), "sellers.ignore"),
        authorized=_names(sellers_raw.get("authorized"), "sellers.authorized"),
    )

    alerts_raw = _mapping(raw, "alerts")
    _unknown(alerts_raw, {"to", "max_per_source", "back_at_map"}, "alerts")
    alerts = Alerts(
        to=_destinations(alerts_raw.get("to"), "alerts.to", Alerts.to),
        max_per_source=_int(alerts_raw.get("max_per_source", 8), "alerts.max_per_source", 1, 100),
        back_at_map=_bool(alerts_raw.get("back_at_map", False), "alerts.back_at_map"),
    )

    report_raw = _mapping(raw, "report")
    _unknown(report_raw, {"day", "to"}, "report")
    day_value = report_raw.get("day", "monday")
    day = "off" if day_value is False else _text(day_value, "report.day").lower()
    if day not in (*DAYS, "off"):
        raise ConfigError(f"report.day: expected a weekday such as monday, or off, got {day!r}")
    report = Report(day=day, to=_destinations(report_raw.get("to"), "report.to", Report.to))

    sheet_raw = _mapping(raw, "sheet")
    _unknown(sheet_raw, {"enabled", "tab"}, "sheet")
    tab = _text(sheet_raw.get("tab", "Findings"), "sheet.tab") or "Findings"
    if not _TAB.match(tab):
        raise ConfigError(f"sheet.tab: a sheet tab name can't contain [ ] * ? / \\ : ({tab!r})")
    sheet = Sheet(enabled=_bool(sheet_raw.get("enabled", True), "sheet.enabled"), tab=tab)

    return Config(
        products=tuple(products),
        rules=rules,
        sellers=sellers,
        alerts=alerts,
        report=report,
        sheet=sheet,
        state=_path(raw.get("state") or "state/findings.json", "state", base_dir),
    )


def _path(value: Any, where: str, base_dir: Path | None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a path")
    path = Path(value.strip())
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    return path


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path} not found (run from the repo folder or pass --config)") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    return parse_config(raw, base_dir=path.resolve().parent)
