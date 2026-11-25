"""Destinations behind one small interface.

A message sink has a ``name`` and three methods: ``send(message)`` delivers the alert or
the weekly report, ``preview(message)`` returns what ``send`` would deliver (for
``--dry-run``) and ``test()`` sends a short hello (for ``test-destinations``). ``send`` and
``test`` raise :class:`DeliveryError` on failure.

The Google Sheet is a sink of its own kind: ``append(rows)`` adds one row per finding
event to a tab, which becomes the evidence log to filter, share and chart.

Secrets come from environment variables, never from ``config.yml``. To add a destination,
write a class like the ones below and register it in ``REGISTRY``.
"""

from __future__ import annotations

import json
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from . import messages
from .config import SOURCES, Config
from .messages import SOURCE, Doc, Style, money
from .process import Event

USER_AGENT = "map-violation-monitor (+https://github.com/ScrapeUnblocker/map-violation-monitor)"
CONNECTED = "map-violation-monitor is connected. Sellers below your MAP will be reported here."


class DeliveryError(RuntimeError):
    """A destination did not accept the message."""


class Message(Protocol):
    kind: str

    @property
    def subject(self) -> str: ...

    def doc(self) -> Doc: ...

    def data(self) -> dict[str, Any]: ...


class Sink(Protocol):
    name: str

    def send(self, message: Message) -> None: ...

    def preview(self, message: Message) -> str: ...

    def test(self) -> str: ...


class Http(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def call(
    http: Http,
    method: str,
    url: str,
    *,
    sink: str,
    sleep: Callable[[float], None] | None = None,
    **kwargs: Any,
) -> Any:
    """One HTTP request, retried once on 429/500/502/503. Errors never include the URL,
    because webhook URLs are secrets."""
    for attempt in (1, 2):
        try:
            resp = http.request(method, url, timeout=30.0, **kwargs)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{sink}: request failed ({type(exc).__name__})") from None
        if resp.status_code in (429, 500, 502, 503) and attempt == 1:
            (sleep or time.sleep)(_retry_after(resp))
            continue
        if resp.status_code >= 300:
            body = (resp.text or "")[:200].replace("\n", " ")
            raise DeliveryError(f"{sink}: HTTP {resp.status_code} {body}".rstrip())
        return resp
    raise AssertionError("unreachable")


def _retry_after(resp: Any) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "2")), 10.0)
    except (TypeError, ValueError, AttributeError):
        return 2.0


class ChatSink:
    """A chat webhook: the message as one or more formatted posts."""

    name = "chat"
    style: Style = messages.PLAIN

    def __init__(self, webhook_url: str, http: Http) -> None:
        self.webhook_url = webhook_url
        self.http = http

    def posts(self, message: Message) -> list[str]:
        return messages.render(message.doc(), self.style)

    def preview(self, message: Message) -> str:
        posts = self.posts(message)
        if len(posts) == 1:
            return posts[0]
        return "\n".join(f"[post {i} of {len(posts)}]\n{p}" for i, p in enumerate(posts, 1))

    def send(self, message: Message) -> None:
        for text in self.posts(message):
            call(self.http, "POST", self.webhook_url, json=self.wrap(text), sink=self.name)

    def test(self) -> str:
        call(self.http, "POST", self.webhook_url, json=self.wrap(CONNECTED), sink=self.name)
        return "test message sent"

    def wrap(self, text: str) -> dict[str, Any]:
        raise NotImplementedError


class SlackSink(ChatSink):
    """Slack incoming webhook (mrkdwn)."""

    name = "slack"
    style = messages.SLACK

    def wrap(self, text: str) -> dict[str, Any]:
        return {"text": text, "unfurl_links": False, "unfurl_media": False}


class DiscordSink(ChatSink):
    """Discord channel webhook. Embeds are suppressed and nobody is pinged."""

    name = "discord"
    style = messages.DISCORD

    def wrap(self, text: str) -> dict[str, Any]:
        return {"content": text, "allowed_mentions": {"parse": []}, "flags": 4}


class TeamsSink:
    """Microsoft Teams channel, through a Workflows webhook (the "Send webhook alerts to a
    channel" template). Posts Adaptive Cards: one per message, more for a long one."""

    name = "teams"

    def __init__(self, webhook_url: str, http: Http) -> None:
        self.webhook_url = webhook_url
        self.http = http

    def preview(self, message: Message) -> str:
        texts = [
            "\n".join(block["text"] for block in card["attachments"][0]["content"]["body"])
            for card in messages.teams_cards(message.doc())
        ]
        if len(texts) == 1:
            return texts[0]
        return "\n".join(f"[card {i} of {len(texts)}]\n{t}" for i, t in enumerate(texts, 1))

    def send(self, message: Message) -> None:
        for card in messages.teams_cards(message.doc()):
            call(self.http, "POST", self.webhook_url, json=card, sink=self.name)

    def test(self) -> str:
        card = messages.teams_cards(Doc(title="map-violation-monitor", subtitle=CONNECTED))[0]
        call(self.http, "POST", self.webhook_url, json=card, sink=self.name)
        return "test card sent"


class WebhookSink:
    """Any URL that accepts a JSON POST: Zapier, Make, n8n, your own endpoint.

    One POST per message: ``source``, ``kind`` (``alert`` or ``report``), the email-style
    ``subject``, the plain-text message as ``text`` and every field as JSON.
    """

    name = "webhook"

    def __init__(self, url: str, http: Http) -> None:
        self.webhook_url = url
        self.http = http

    def payload(self, message: Message) -> dict[str, Any]:
        return {
            "source": SOURCE,
            "kind": message.kind,
            "subject": message.subject,
            "text": messages.render_plain(message.doc()),
            **message.data(),
        }

    def preview(self, message: Message) -> str:
        return self.payload(message)["text"]

    def send(self, message: Message) -> None:
        call(self.http, "POST", self.webhook_url, json=self.payload(message), sink=self.name)

    def test(self) -> str:
        payload = {"source": SOURCE, "kind": "test", "text": CONNECTED}
        call(self.http, "POST", self.webhook_url, json=payload, sink=self.name)
        return "test message sent"


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    to: tuple[str, ...]
    security: str  # "starttls", "ssl" or "none"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SmtpSettings:
        def get(name: str) -> str:
            return (env.get(name) or "").strip()

        security = get("SMTP_SECURITY").lower()
        port_text = get("SMTP_PORT")
        try:
            port = int(port_text) if port_text else (465 if security == "ssl" else 587)
        except ValueError:
            raise DeliveryError(f"email: SMTP_PORT must be a number, got {port_text!r}") from None
        if not security:
            security = "ssl" if port == 465 else "starttls"
        if security not in ("starttls", "ssl", "none"):
            raise DeliveryError("email: SMTP_SECURITY must be starttls, ssl or none")
        to = tuple(a.strip() for a in get("EMAIL_TO").replace(";", ",").split(",") if a.strip())
        sender = get("EMAIL_FROM") or get("SMTP_USERNAME")
        if not sender:
            raise DeliveryError("email: set EMAIL_FROM (or SMTP_USERNAME) as the sender address")
        return cls(
            host=get("SMTP_HOST"),
            port=port,
            username=get("SMTP_USERNAME"),
            password=env.get("SMTP_PASSWORD") or "",
            sender=sender,
            to=to,
            security=security,
        )


class EmailSink:
    """Email over SMTP: HTML with a plain-text part."""

    name = "email"

    def __init__(
        self, settings: SmtpSettings, smtp_factory: Callable[[SmtpSettings], Any] | None = None
    ) -> None:
        self.settings = settings
        self.smtp_factory = smtp_factory or _connect

    def build(self, subject: str, text: str, html: str | None = None) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = " ".join(subject.split())[:200]
        mail["From"] = self.settings.sender
        mail["To"] = ", ".join(self.settings.to)
        mail["Date"] = formatdate(localtime=False)
        mail["Message-ID"] = make_msgid(domain="map-violation-monitor.local")
        mail.set_content(text)
        if html:
            mail.add_alternative(html, subtype="html")
        return mail

    def preview(self, message: Message) -> str:
        return f"Subject: {message.subject}\n\n{messages.render_plain(message.doc())}"

    def send(self, message: Message) -> None:
        doc = message.doc()
        mail = self.build(message.subject, messages.render_plain(doc), messages.render_html(doc))
        self.deliver(mail)

    def test(self) -> str:
        self.deliver(self.build("map-violation-monitor is connected", CONNECTED))
        return f"test email sent to {', '.join(self.settings.to)}"

    def deliver(self, mail: EmailMessage) -> None:
        try:
            with self.smtp_factory(self.settings) as smtp:
                smtp.send_message(mail)
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"email: {type(exc).__name__}: {exc}") from None


def _connect(settings: SmtpSettings) -> smtplib.SMTP:
    if settings.security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=30, context=ssl.create_default_context()
        )
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=30)
    try:
        if settings.security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if settings.username:
            smtp.login(settings.username, settings.password)
    except BaseException:
        smtp.close()
        raise
    return smtp


# -- Google Sheets ------------------------------------------------------------------------

SHEETS_API = "https://sheets.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
HEADER = [
    "Logged at (UTC)",
    "Event",
    "Product",
    "SKU",
    "Source",
    "Market",
    "Seller",
    "Seller id",
    "Authorized",
    "Price",
    "MAP",
    "Currency",
    "Below MAP by",
    "Below MAP %",
    "Shipping",
    "Listings",
    "Listing title",
    "Link",
    "Seller page",
    "First seen",
    "Time below MAP",
]
_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_EVENT_NAMES = {"new": "below MAP", "lower": "further below MAP", "back at MAP": "back at MAP"}


def sheet_id(value: str) -> str:
    """A spreadsheet id, or the id inside a docs.google.com/spreadsheets/d/<id>/ URL."""
    value = value.strip()
    m = _SHEET_ID.search(value)
    return m.group(1) if m else value


def load_service_account(value: str) -> dict[str, Any]:
    """The service account key: the JSON itself, or the path of the .json file."""
    text = value.strip()
    if not text.startswith("{"):
        try:
            text = Path(text).read_text(encoding="utf-8")
        except OSError as exc:
            raise DeliveryError(f"sheet: cannot read the service account key file: {exc}") from None
    try:
        info = json.loads(text)
    except ValueError:
        raise DeliveryError("sheet: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from None
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise DeliveryError(
            "sheet: GOOGLE_SERVICE_ACCOUNT_JSON needs client_email and private_key "
            "(paste the whole key file of the service account)"
        )
    return info


def _cell(value: Any) -> Any:
    """Text that starts like a formula is kept as text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def sheet_row(event: Event, product_name: str, sku: str, logged_at: str) -> list[Any]:
    f = event.finding
    row = [
        logged_at.replace("T", " ").removesuffix("+00:00"),
        _EVENT_NAMES.get(event.kind, event.kind),
        product_name,
        sku,
        SOURCES.get(f.source, f.source),
        f.market,
        f.seller,
        f.seller_id,
        "yes" if f.authorized else "",
        f.price,
        f.map,
        f.currency,
        f.gap if event.kind != "back at MAP" else "",
        f.gap_percent if event.kind != "back at MAP" else "",
        f.shipping if f.shipping is not None else "",
        f.listings,
        f.title if f.source != "amazon" else "",
        f.url,
        f.seller_url,
        event.first_seen,
        f"{event.days} days" if event.kind == "back at MAP" else "",
    ]
    return [_cell(v) for v in row]


class SheetSink:
    """Appends one row per finding event to a tab of a Google Sheet, as a service account.

    Share the sheet with the service account's email (Editor). The tab and its header row
    are created on the first write.
    """

    name = "sheet"

    def __init__(
        self,
        spreadsheet: str,
        account: dict[str, Any],
        http: Http,
        *,
        tab: str = "Findings",
        api_url: str = SHEETS_API,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.spreadsheet = spreadsheet
        self.account = account
        self.http = http
        self.tab = tab
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self._token = ""
        self._expires = 0.0
        self._ready = False

    @property
    def email(self) -> str:
        return str(self.account.get("client_email"))

    # auth: a JWT signed with the service account key, exchanged for an access token
    def assertion(self) -> str:
        from google.auth import crypt, jwt

        try:
            signer = crypt.RSASigner.from_service_account_info(self.account)
        except (ValueError, KeyError) as exc:
            raise DeliveryError(
                f"sheet: the service account private key is unusable: {exc}"
            ) from None
        now = int(self.clock())
        claims = {
            "iss": self.email,
            "scope": SCOPE,
            "aud": self.token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(signer, claims).decode("ascii")

    @property
    def token_uri(self) -> str:
        return str(self.account.get("token_uri") or "https://oauth2.googleapis.com/token")

    def token(self) -> str:
        if self._token and self.clock() < self._expires - 60:
            return self._token
        resp = call(
            self.http,
            "POST",
            self.token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.assertion(),
            },
            sink="sheet (sign-in)",
        )
        body = resp.json()
        self._token = str(body.get("access_token") or "")
        if not self._token:
            raise DeliveryError("sheet: Google returned no access token")
        self._expires = self.clock() + float(body.get("expires_in") or 3600)
        return self._token

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.api_url}/v4/spreadsheets/{quote(self.spreadsheet, safe='')}{path}"
        headers = {"Authorization": f"Bearer {self.token()}"}
        resp = call(self.http, method, url, headers=headers, sink="sheet", **kwargs)
        try:
            return resp.json()
        except ValueError:
            return {}

    def range(self, cells: str) -> str:
        name = "'" + self.tab.replace("'", "''") + "'"
        return quote(f"{name}!{cells}", safe="")

    def ensure_tab(self) -> bool:
        """Create the tab and its header row when missing. Returns True when created."""
        if self._ready:
            return False
        meta = self.api("GET", "", params={"fields": "sheets.properties.title"})
        titles = [s.get("properties", {}).get("title") for s in meta.get("sheets") or []]
        created = False
        if self.tab not in titles:
            body = {"requests": [{"addSheet": {"properties": {"title": self.tab}}}]}
            self.api("POST", ":batchUpdate", json=body)
            created = True
        first = self.api("GET", f"/values/{self.range('A1:U1')}")
        if not first.get("values"):
            self.api(
                "PUT",
                f"/values/{self.range('A1')}",
                params={"valueInputOption": "RAW"},
                json={"values": [HEADER]},
            )
            created = True
        self._ready = True
        return created

    def append(self, rows: Sequence[list[Any]]) -> None:
        if not rows:
            return
        self.ensure_tab()
        self.api(
            "POST",
            f"/values/{self.range('A1')}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [list(r) for r in rows]},
        )

    def preview(self, rows: Sequence[list[Any]]) -> str:
        lines = [f"{plural_rows(len(rows))} for tab {self.tab!r}:"]
        for row in rows[:12]:
            price = money(float(row[9]), str(row[11]))
            lines.append(f"  {row[1]:<17} {row[4]:<15} {str(row[6])[:32]:<32} {price}")
        if len(rows) > 12:
            lines.append(f"  … {len(rows) - 12} more")
        return "\n".join(lines)

    def test(self) -> str:
        created = self.ensure_tab()
        state = "created with its header row" if created else "ready"
        return f"tab {self.tab!r} {state} (shared with {self.email})"


def plural_rows(n: int) -> str:
    return f"{n} row" + ("" if n == 1 else "s")


# -- registry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SinkSpec:
    env: tuple[str, ...]  # every one must be set for the destination to be used
    build: Callable[[Mapping[str, str], Http], Sink]


REGISTRY: dict[str, SinkSpec] = {
    "slack": SinkSpec(
        ("SLACK_WEBHOOK_URL",), lambda env, http: SlackSink(env["SLACK_WEBHOOK_URL"].strip(), http)
    ),
    "teams": SinkSpec(
        ("TEAMS_WEBHOOK_URL",), lambda env, http: TeamsSink(env["TEAMS_WEBHOOK_URL"].strip(), http)
    ),
    "discord": SinkSpec(
        ("DISCORD_WEBHOOK_URL",),
        lambda env, http: DiscordSink(env["DISCORD_WEBHOOK_URL"].strip(), http),
    ),
    "webhook": SinkSpec(
        ("ALERT_WEBHOOK_URL",),
        lambda env, http: WebhookSink(env["ALERT_WEBHOOK_URL"].strip(), http),
    ),
    "email": SinkSpec(
        ("SMTP_HOST", "EMAIL_TO"), lambda env, http: EmailSink(SmtpSettings.from_env(env))
    ),
}
SHEET_ENV = ("GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")


@dataclass
class Destinations:
    alerts: list[Sink]
    report: list[Sink]
    sheet: SheetSink | None
    skipped: list[str]  # destinations listed in config.yml whose secrets are missing

    @property
    def all(self) -> list[Any]:
        out: list[Any] = [self.sheet] if self.sheet else []
        for sink in [*self.alerts, *self.report]:
            if sink not in out:
                out.append(sink)
        return out


def build_destinations(config: Config, env: Mapping[str, str], http: Http) -> Destinations:
    """Every destination listed in config.yml whose secrets are set. A destination listed for
    both alerts and the report is one sink."""

    def ready(names: Sequence[str]) -> bool:
        return all((env.get(n) or "").strip() for n in names)

    built: dict[str, Sink] = {}
    skipped: list[str] = []
    for name in dict.fromkeys([*config.alerts.to, *config.report.to]):
        spec = REGISTRY[name]
        if ready(spec.env):
            built[name] = spec.build(env, http)
        else:
            skipped.append(name)
    sheet = None
    if config.sheet.enabled:
        if ready(SHEET_ENV):
            sheet = SheetSink(
                sheet_id(env["GOOGLE_SHEET_ID"]),
                load_service_account(env["GOOGLE_SERVICE_ACCOUNT_JSON"]),
                http,
                tab=config.sheet.tab,
                api_url=(env.get("GOOGLE_SHEETS_API_URL") or "").strip() or SHEETS_API,
            )
        else:
            skipped.insert(0, "sheet")
    return Destinations(
        alerts=[built[n] for n in config.alerts.to if n in built],
        report=[built[n] for n in config.report.to if n in built],
        sheet=sheet,
        skipped=skipped,
    )


def needs(name: str) -> str:
    env = SHEET_ENV if name == "sheet" else REGISTRY[name].env
    return " and ".join(env)


def new_http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False)
