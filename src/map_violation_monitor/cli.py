"""Command line: ``map-violation-monitor run | report | status | test-destinations``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

from scrapeunblocker import Client

from . import __version__
from .collect import FATAL, Fetch, ShopClient, fetch
from .config import Config, ConfigError, Product, Target, load_config
from .messages import build_alert, build_report, money, plural, render_plain, where
from .process import Checked, Event, check, compare, drop_unknown
from .sinks import (
    DeliveryError,
    Destinations,
    Message,
    Sink,
    build_destinations,
    needs,
    new_http_client,
    plural_rows,
    sheet_row,
)
from .state import State, StateError, iso_week

API_KEY_ENV = "SCRAPEUNBLOCKER_KEY"
SIGNUP_URL = (
    "https://scrapeunblocker.com/?utm_source=github&utm_medium=integration"
    "&utm_campaign=example-repos"
)
WORKERS = 4
# What to change when an answer was cut off with sellers below MAP still to come.
MORE = {
    "amazon": "more offers on the next page: raise pages (up to 5) to read them",
    "idealo": "every offer read is below MAP: raise max_offers to read more shops",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="map-violation-monitor",
        description=(
            "Finds every seller advertising your products below your Minimum Advertised "
            "Price on Amazon, Google Shopping, eBay and idealo. Alerts Slack, Teams, Discord, "
            "email or a webhook about new and deeper undercuts, logs every finding to a Google "
            "Sheet and sends a weekly report. Prices come from the ScrapeUnblocker API."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default="config.yml", help="config file (default: config.yml)"
    )
    common.add_argument(
        "--env-file",
        default=".env",
        help="read environment variables from this file when it exists (default: .env)",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="log every API call")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = sub.add_parser(
        "run",
        parents=[common],
        help="check every product, alert on new and deeper undercuts, log them to the sheet",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print exactly what would be delivered; deliver nothing and save nothing",
    )
    run.add_argument(
        "--product",
        action="append",
        metavar="NAME",
        help="only products whose name or SKU contains this text; repeat for more",
    )
    run.add_argument(
        "--no-report", action="store_true", help="do not send the weekly report even if due"
    )
    run.add_argument(
        "--save-answers",
        metavar="DIR",
        help="also write every API answer to this folder as JSON (to look into a result)",
    )
    report = sub.add_parser(
        "report", parents=[common], help="send the weekly MAP report now (no API calls)"
    )
    report.add_argument("--dry-run", action="store_true", help="print it; send and save nothing")
    report.add_argument(
        "--force", action="store_true", help="send even if this week's report went out already"
    )
    sub.add_parser(
        "status", parents=[common], help="the sellers below MAP right now, from the state file"
    )
    sub.add_parser(
        "test-destinations",
        parents=[common],
        help="send a test message to each destination and set up the sheet tab (no API calls)",
    )
    return parser


def load_env_file(path: str, environ: MutableMapping[str, str]) -> None:
    """Minimal ``.env`` reader: ``KEY=value`` lines; real environment variables win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in environ:
                environ[key] = value


def annotation(level: str, title: str, message: str) -> str:
    """A GitHub Actions workflow command (``::warning title=...::...``), safely escaped."""

    def data(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    prop = data(title).replace(":", "%3A").replace(",", "%2C")
    return f"::{level} title={prop}::{data(message)}"


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# -- checking -------------------------------------------------------------------------------


class SavingClient:
    """Wraps the API client and writes each answer to a folder (``run --save-answers``)."""

    def __init__(self, client: ShopClient, folder: Path) -> None:
        self.client = client
        self.folder = folder

    def _save(self, source: str, market: Any, ref: Any, data: Any) -> Any:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9.]+", "-", f"{source}-{market}-{ref}").strip("-")[:90]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = self.folder / f"{stamp}-{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data

    def amazon_offers(self, **params: Any) -> Any:
        data = self.client.amazon_offers(**params)
        return self._save("amazon", params.get("marketplace"), params.get("asin"), data)

    def google_shopping(self, q: str, **params: Any) -> Any:
        data = self.client.google_shopping(q, **params)
        return self._save("google-shopping", params.get("gl"), q, data)

    def ebay_search(self, keyword: str, **params: Any) -> Any:
        data = self.client.ebay_search(keyword, **params)
        return self._save("ebay", params.get("marketplace"), keyword, data)

    def idealo_offers(self, product_id: str | None = None, **params: Any) -> Any:
        data = self.client.idealo_offers(product_id, **params)
        return self._save("idealo", params.get("country"), product_id, data)


def fetch_all(
    config: Config,
    client_factory: Callable[[], ShopClient],
    jobs: Sequence[tuple[Product, Target]],
    sleep: Callable[[float], None] | None = None,
) -> list[Fetch]:
    """Every target, a few at a time. The order of the results is the order of ``jobs``."""

    def one(job: tuple[Product, Target]) -> Fetch:
        product, target = job
        rules = config.rules
        return fetch(
            client_factory(),
            product,
            target,
            floor=rules.floor(product.map),
            limit=rules.limit(product.map),
            sleep=sleep,
        )

    if len(jobs) <= 1:
        return [one(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(jobs))) as pool:
        return list(pool.map(one, jobs))


def describe(c: Checked, f: Fetch) -> str:
    if f.error:
        return f"no answer: {f.error}"
    if not f.offers:
        return f.note or "no offers"
    text = f"{plural(len(f.offers), 'offer')}, {c.offers} of this product"
    text += f", {plural(len(c.findings), 'seller')} below MAP"
    if c.findings:
        low = c.findings[0]
        text += f" (lowest {money(low.price, low.currency)} by {low.seller})"
    return text


def skipped_text(c: Checked) -> str:
    return ", ".join(f"{n} {reason}" for reason, n in sorted(c.skipped.items()))


def deliver(message: Message, sinks: Sequence[Sink], *, err: TextIO) -> tuple[bool, list[str]]:
    """Send to every sink. Returns ``(at least one took it, names of sinks that failed)``."""
    ok = False
    failed = []
    for sink in sinks:
        try:
            sink.send(message)
        except DeliveryError as exc:
            print(f"Delivery failed: {exc}", file=err)
            failed.append(sink.name)
        else:
            ok = True
    return ok, failed


def report_due(config: Config, state: State, today: date) -> bool:
    """On the first run on or after ``report.day`` each week, once a run before today has
    recorded findings."""
    weekday = config.report.weekday
    if weekday is None or state.report_week == iso_week(today) or today.weekday() < weekday:
        return False
    return bool(state.first_run) and state.first_run < today.isoformat()


def rows_for(config: Config, events: Sequence[Event], logged_at: str) -> list[list[Any]]:
    products = {p.id: p for p in config.products}
    rows = []
    for e in events:
        p = products.get(e.finding.product)
        name = p.name if p else e.finding.product
        rows.append(sheet_row(e, name, p.sku if p else "", logged_at))
    return rows


# -- run ----------------------------------------------------------------------------------


def run_checks(
    config: Config,
    client_factory: Callable[[], ShopClient],
    dests: Destinations,
    *,
    dry_run: bool,
    now: datetime,
    products: Sequence[Product] | None = None,
    send_report: bool = True,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    annotate: bool = False,
    summary_path: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """One scheduled run: read every seller's price, compare with MAP, alert, log to the
    sheet, send the report when it is due, save. Returns the exit code."""
    today = now.date()
    state = State.load(config.state)
    first_run = not state.first_run
    use = list(products or config.products)
    jobs = [(p, t) for p in use for t in p.targets]
    calls = sum(t.calls for _, t in jobs)
    print(
        f"Checking {plural(len(use), 'product')} on {plural(len(jobs), 'source')}: "
        f"{plural(calls, 'API call')}.",
        file=out,
        flush=True,
    )
    fetches = fetch_all(config, client_factory, jobs, sleep=sleep)
    checked = [check(f, config.rules, config.sellers) for f in fetches]
    exit_code = 0
    used = 0
    last = ""
    for c, f in zip(checked, fetches, strict=True):
        used += f.calls
        if f.product.name != last:
            last = f.product.name
            print(f"\n{f.product.name} · MAP {money(f.product.map, f.product.currency)}", file=out)
        print(
            f"  {short(f.target.label, 28):<28} {f.target.ref[:30]:<30} {describe(c, f)}", file=out
        )
        if c.skipped and f.offers:
            print(f"{'':<60}skipped: {skipped_text(c)}", file=out)
        if f.offers and not c.offers:
            print(
                f"{'':<60}none of them is this product, so nothing is concluded from this "
                "answer (check match, exclude and currency)",
                file=out,
            )
        if c.all_below and f.target.source in MORE:
            print(f"{'':<60}{MORE[f.target.source]}", file=out)
        if f.error and annotate:
            level = "warning" if f.permanent else "notice"
            print(
                annotation(level, f"{f.target.label} not read", f"{f.target.ref}: {f.error}"),
                file=out,
            )
        if f.permanent:
            exit_code = 1
    print(f"\nAPI calls used: {used}", file=out)
    if jobs and all(f.error for f in fetches):
        exit_code = 1

    opened = drop_unknown(state.open, config.products)
    events, after = compare(checked, opened, config.rules, today, state.episodes())
    alert = build_alert(
        config, events, now, first_run=first_run, back_at_map=config.alerts.back_at_map
    )
    rows = rows_for(config, events, now.isoformat())
    n_back = sum(e.kind == "back at MAP" for e in events)
    print(
        f"Below MAP now: {plural(len(after), 'seller')} · this run: "
        f"{len(events) - n_back} new or further down, {n_back} back at MAP.",
        file=out,
    )

    # 1. alerts
    record = True
    if alert.empty:
        print("Nothing new to alert.", file=out)
    elif dry_run:
        print(f"Dry run: would deliver {alert.subject}", file=out)
        if not dests.alerts:
            print("No alert destination is set up; the message would be:", file=out)
            print(render_plain(alert.doc()), file=out)
        for sink in dests.alerts:
            print(f"\n--- {sink.name} ---\n{sink.preview(alert)}", file=out)
    elif not dests.alerts:
        print(
            f"{alert.subject}. No alert destination is set up (alerts.to in config.yml and its "
            "secret), so here it is:\n",
            file=out,
        )
        print(render_plain(alert.doc()), file=out)
    else:
        ok, failed = deliver(alert, dests.alerts, err=err)
        took = ", ".join(s.name for s in dests.alerts if s.name not in failed)
        if ok:
            print(f"Delivered to {took}: {alert.subject}", file=out)
        else:
            record = False
            print("Not delivered anywhere. The same findings are tried again next run.", file=out)
        if failed:
            exit_code = 1
            if annotate:
                names = ", ".join(sorted(failed))
                print(
                    annotation("warning", "Delivery failed", f"Not accepted by {names}"), file=out
                )

    # 2. the sheet: one row per event, plus rows an earlier run could not write
    if record:
        exit_code = max(
            exit_code, log_rows(config, state, dests, rows, dry_run, out, err, annotate)
        )
        if not dry_run:
            state.record(events, after, today)

    # 3. the weekly report
    if send_report and record and report_due(config, state, today):
        code = send_weekly(config, state, dests, now=now, dry_run=dry_run, out=out, err=err)
        exit_code = max(exit_code, code)

    if summary_path:
        _write_summary(summary_path, config, after, used)
    if dry_run:
        print("Dry run: nothing was sent or saved.", file=out)
        return exit_code
    state.prune(today)
    if state.save(config.state):
        print(f"Saved {config.state.name}.", file=out)
    return exit_code


def log_rows(
    config: Config,
    state: State,
    dests: Destinations,
    rows: list[list[Any]],
    dry_run: bool,
    out: TextIO,
    err: TextIO,
    annotate: bool,
) -> int:
    if not config.sheet.enabled:
        return 0
    waiting = [*state.pending_rows, *rows]
    if not waiting:
        return 0
    if dests.sheet is None:
        if rows:
            print(
                f"Google Sheet not set up ({needs('sheet')}): {plural_rows(len(rows))} not logged.",
                file=out,
            )
        return 0
    if dry_run:
        print(f"Dry run: would append {dests.sheet.preview(waiting)}", file=out)
        return 0
    try:
        dests.sheet.append(waiting)
    except DeliveryError as exc:
        print(f"Delivery failed: {exc}", file=err)
        dropped = state.queue_rows(rows)
        print(
            f"The rows are written on the next run{' (oldest dropped)' if dropped else ''}.",
            file=out,
        )
        if annotate:
            print(annotation("warning", "Google Sheet", str(exc)), file=out)
        return 1
    state.pending_rows = []
    print(f"Google Sheet: appended {plural_rows(len(waiting))} to {config.sheet.tab}.", file=out)
    return 0


def send_weekly(
    config: Config,
    state: State,
    dests: Destinations,
    *,
    now: datetime,
    dry_run: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    message = build_report(config, state.open, state.history, state.back, now)
    if dry_run:
        print(f"\nDry run: would send the weekly report: {message.subject}", file=out)
        if not dests.report:
            print(render_plain(message.doc()), file=out)
        for sink in dests.report:
            print(f"\n--- {sink.name} ---\n{sink.preview(message)}", file=out)
        return 0
    if not dests.report:
        print(
            f"Weekly report: no report destination is set up (report.to). {message.subject}",
            file=out,
        )
        state.report_week = iso_week(now.date())
        return 0
    ok, failed = deliver(message, dests.report, err=err)
    if ok:
        state.report_week = iso_week(now.date())
        took = ", ".join(s.name for s in dests.report if s.name not in failed)
        print(f"Weekly report delivered to {took}: {message.subject}", file=out)
    else:
        print("Weekly report not delivered anywhere; tried again next run.", file=out)
    return 1 if failed else 0


def _write_summary(path: str, config: Config, opened: dict[str, Any], calls: int) -> None:
    lines = [
        "### Sellers below MAP",
        "",
        "| Product | MAP | Source | Seller | Price | Below MAP | Since |",
        "|---|---|---|---|---|---|---|",
    ]
    products = {p.id: p for p in config.products}
    rows = sorted(
        opened.values(),
        key=lambda o: (o.finding.product, -o.finding.gap_percent, o.finding.seller.casefold()),
    )
    for o in rows:
        f = o.finding
        p = products.get(f.product)
        name = (p.name if p else f.product).replace("|", "/")
        seller = f.seller.replace("|", "/")
        lines.append(
            f"| {name} | {money(f.map, f.currency)} | {where(f.source, f.market)} | {seller} | "
            f"{money(f.price, f.currency)} | {f.gap_percent:g}% | {o.first_seen} |"
        )
    if not rows:
        lines.append("| - | - | - | none | - | - | - |")
    lines += ["", f"API calls this run: {calls}", ""]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# -- other commands -----------------------------------------------------------------------


def show_status(config: Config, out: TextIO) -> int:
    state = State.load(config.state)
    if not state.first_run:
        print("No run yet: the state file is empty. `map-violation-monitor run` starts.", file=out)
        return 0
    week = f" · last report {state.report_week}" if state.report_week else ""
    print(f"State since {state.first_run}{week}", file=out)
    for p in config.products:
        mine = sorted(
            (o for o in state.open.values() if o.finding.product == p.id),
            key=lambda o: (-o.finding.gap_percent, o.finding.seller.casefold()),
        )
        head = f"\n{p.name} · MAP {money(p.map, p.currency)}"
        print(f"{head} · {plural(len(mine), 'seller')} below MAP", file=out)
        for o in mine:
            f = o.finding
            label = f"{where(f.source, f.market)} · {f.seller}"
            print(
                f"  {short(label, 52):<52} {money(f.price, f.currency):>12} "
                f"{f.gap_percent:>5g}%  since {o.first_seen}",
                file=out,
            )
    return 0


def test_destinations(dests: Destinations, out: TextIO) -> int:
    for name in dests.skipped:
        print(f"  {name:<8} skipped ({needs(name)} not set)", file=out)
    failed = 0
    for sink in dests.all:
        try:
            result = sink.test()
        except DeliveryError as exc:
            failed += 1
            print(f"  {sink.name:<8} FAILED {exc}", file=out)
        else:
            print(f"  {sink.name:<8} ok, {result}", file=out)
    return 1 if failed else 0


def _make_client(environ: Mapping[str, str]) -> Client:
    # The SDK's own retries are off: fetch() retries once, after a pause.
    return Client(api_key=environ[API_KEY_ENV].strip(), timeout=180.0, max_retries=0)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    client_factory: Callable[[Mapping[str, str]], ShopClient] = _make_client,
    http_factory: Callable[[], Any] = new_http_client,
    now: datetime | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    err = err or sys.stderr
    for stream in (out, err):
        if hasattr(stream, "reconfigure"):
            # Seller names and titles can hold any character; never crash on a narrow console.
            stream.reconfigure(errors="replace")
    now = now or datetime.now(UTC).replace(microsecond=0)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and webhook URLs are secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_env_file(args.env_file, environ)
    has_key = bool((environ.get(API_KEY_ENV) or "").strip())
    try:
        config = load_config(args.config)
        if args.command == "status":
            return show_status(config, out)
        if args.command == "run" and not has_key:
            print(
                f"error: set {API_KEY_ENV} to your ScrapeUnblocker API key "
                f"(get one at {SIGNUP_URL})",
                file=err,
            )
            return 2

        http = http_factory()
        try:
            dests = build_destinations(config, environ, http)
            if args.command == "test-destinations":
                if not dests.all:
                    wanted = "; ".join(needs(n) for n in dests.skipped)
                    print(f"error: no destination is set up. Set one of: {wanted}", file=err)
                    return 2
                return test_destinations(dests, out)
            if dests.skipped:
                print(
                    f"(not set up, skipped: {', '.join(dests.skipped)}. `test-destinations` "
                    "lists the secrets each one needs.)",
                    file=out,
                )
            if args.command == "report":
                state = State.load(config.state)
                if state.report_week == iso_week(now.date()) and not args.force:
                    print(
                        f"This week's report ({state.report_week}) went out already. "
                        "--force sends it again.",
                        file=out,
                    )
                    return 0
                code = send_weekly(
                    config, state, dests, now=now, dry_run=args.dry_run, out=out, err=err
                )
                if not args.dry_run and state.save(config.state):
                    print(f"Saved {config.state.name}.", file=out)
                return code
            only = None
            if args.product:
                only = []
                for text in args.product:
                    matches = config.find_products(text)
                    if not matches:
                        raise ConfigError(f"--product {text!r} matches no product in config.yml")
                    only += [p for p in matches if p not in only]
            make: Callable[[], ShopClient] = lambda: client_factory(environ)  # noqa: E731
            if args.save_answers:
                folder = Path(args.save_answers)
                make = lambda: SavingClient(client_factory(environ), folder)  # noqa: E731
            return run_checks(
                config,
                make,
                dests,
                dry_run=args.dry_run,
                now=now,
                products=only,
                send_report=not args.no_report,
                out=out,
                err=err,
                annotate=environ.get("GITHUB_ACTIONS") == "true",
                summary_path=environ.get("GITHUB_STEP_SUMMARY") or None,
            )
        finally:
            close = getattr(http, "close", None)
            if callable(close):
                close()
    except (ConfigError, StateError, DeliveryError) as exc:
        print(f"error: {exc}", file=err)
        return 2
    except FATAL as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
