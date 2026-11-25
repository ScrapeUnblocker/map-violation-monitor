# MAP violation monitor

**For brand and channel managers: every seller advertising your products below your Minimum Advertised Price on Amazon, Google Shopping, eBay and idealo, in Slack, Teams, Discord or a webhook the day it shows up, logged to a Google Sheet as evidence, with a weekly MAP report by email.**

[![CI](https://github.com/ScrapeUnblocker/map-violation-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/ScrapeUnblocker/map-violation-monitor/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Powered by ScrapeUnblocker](https://img.shields.io/badge/powered%20by-ScrapeUnblocker-6f42c1.svg)](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)

You list your products with their MAP and where they are sold. Once a day a GitHub Actions
run reads every seller's advertised price, keeps the ones below your MAP, and tells you
about each seller once: when it first goes below MAP, and again only if it drops further.
You need no seller account and no marketplace API keys.

## What arrives

The first alert of the default config, from a real run on 25 Nov 2026 (shortened where
marked `…`). The MAP values in the default config are made up for the demo, so these
sellers are only below *that* number. That is why their names are replaced here:

```text
🏷️ Below MAP · Wed 25 Nov 2026 · first check, 107 sellers

Sony WH-1000XM5 headphones (US) · MAP $279.99
• 🆕 Amazon (amazon.com) · Seller A: $253.95, 9.3% below MAP (3 listings)
  https://www.amazon.com/dp/B09XS7JWHH?aod=1
• 🆕 Amazon (amazon.com) · Seller B: $278.00, 0.7% below MAP
  https://www.amazon.com/dp/B09XS7JWHH?aod=1
• 🆕 Google Shopping (us) · Seller C: $169.99, 39.3% below MAP
  https://www.google.com/search?udm=28&q=Sony+WH-1000XM5&gl=us
  “Sony WH-1000XM5 Noise Canceling Wireless Over-Ear Headphones”
• 🆕 Google Shopping (us) · Seller D: $189.99, 32.1% below MAP
  https://www.google.com/search?udm=28&q=Sony+WH-1000XM5&gl=us
  “Sony WH-1000XM5 Wireless Noise Cancelling Headphones”
  …
• 🆕 eBay (ebay.com) · seller_e: $154.99, 44.6% below MAP
  https://www.ebay.com/itm/…
  “Sony WH-1000XM5 Wireless Noise Cancelling Headphones WH1000XM5 Silver”
• 🆕 eBay (ebay.com) · seller_f: $159.95, 42.9% below MAP
  https://www.ebay.com/itm/…
  “SONY WH-1000XM5 Wireless Noise Canceling Over Ear Headphones - BRAND NEW” · + $57.44 shipping
  …
• + 81 more sellers below MAP on eBay (ebay.com), listed in the Google Sheet and the weekly report

Sony WH-1000XM5 headphones (DE) · MAP €229.00
• 🆕 idealo (de) · Shop G: €199.00, 13.1% below MAP
  https://www.idealo.de/relocator/relocate?…
  variant: Sony WH-1000XM5 Black + Softtasche
• 🆕 idealo (de) · Shop H: €209.00, 8.7% below MAP (3 listings)
  https://www.idealo.de/relocator/relocate?…
  variant: Sony WH-1000XM5 Black
  …

From now on a message arrives only when a seller newly goes below MAP or drops further.
```

After that, a message lists only what changed. The lines below are templates, not data:

```text
• 🆕 eBay (ebay.com) · <seller>: <price>, <n>% below MAP
  <link to the listing>
  “<listing title>” · 2nd time below MAP
• 🔻 Amazon (amazon.com) · <seller>: <price>, was <price last alerted> (<n>% below MAP)
• ✅ idealo (de) · <shop>: back at MAP after <n> days (was <price>)
```

(The last kind only with `alerts.back_at_map: true`; it is always in the weekly report.)

Slack and Discord get the same message with a **view** link per seller, Teams gets an
Adaptive Card, and the webhook gets the text plus every field as JSON. In the Google Sheet
each finding is one row: time, event (below MAP, further below MAP, back at MAP), product, SKU,
source, market, seller, seller id, authorized, price, MAP, currency, below MAP by, %,
shipping, listings, listing title, link, seller page, first seen, time below MAP.

The **weekly MAP report** (email by default) lists every seller below MAP right now, per
product and source with the date first seen, then the sellers back at MAP this week, then
repeat offenders (sellers that went below MAP twice or more in 12 months):

```text
🏷️ MAP report · week 48 · 19-25 Nov 2026 · 198 sellers below MAP now

New this week: 198 · back at MAP: 1 · repeat offenders: 0
Amazon 2 · Google Shopping 11 · eBay 176 · idealo 9

Sony WH-1000XM5 headphones (US) · MAP $279.99 · 189 sellers below
• 🆕 Amazon (amazon.com) · Seller A: $253.95, 9.3% below MAP (3 listings) · since 25 Nov
  …
```

## Setup in three steps

1. **Use this template** (green button, top right) to make your own copy. Make it
   **private**: your price list and findings are your business.
2. **Add repository secrets** (Settings > Secrets and variables > Actions):
   `SCRAPEUNBLOCKER_KEY` ([get a key](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)),
   plus the destinations you want:

   | Destination | Secrets |
   |---|---|
   | Slack | `SLACK_WEBHOOK_URL` (an [incoming webhook](https://api.slack.com/messaging/webhooks)) |
   | Microsoft Teams | `TEAMS_WEBHOOK_URL` (Workflows app > "Send webhook alerts to a channel") |
   | Discord | `DISCORD_WEBHOOK_URL` (channel > Integrations > Webhooks) |
   | Any URL (Zapier, Make, n8n) | `ALERT_WEBHOOK_URL` |
   | Google Sheet | `GOOGLE_SHEET_ID` (the sheet's URL) and `GOOGLE_SERVICE_ACCOUNT_JSON` (a service account key; share the sheet with its email as Editor) |
   | Email | `SMTP_HOST`, `EMAIL_TO`, and as needed `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `SMTP_SECURITY` |

3. **Edit [`config.yml`](config.yml)**: your products, their MAP, and where each is sold
   (an ASIN, Google Shopping search words, an eBay search, an idealo product id).

Then open **Actions > MAP check > Run workflow** and tick *Dry run* to see what it would
send. After that it runs every day at 06:17 UTC. Until `SCRAPEUNBLOCKER_KEY` is set the
workflow only prints a notice and stays green.

`map-violation-monitor test-destinations` sends a hello to every destination you set up
and creates the sheet tab, without spending API calls.

### Run it on your own machine

```bash
pip install -e .            # Python 3.11+
cp .env.example .env        # put your key and destinations in .env
map-violation-monitor run --dry-run
map-violation-monitor run   # deliver, and save state/findings.json
```

On a schedule with cron (daily at 07:00):

```cron
0 7 * * * cd /path/to/map-violation-monitor && .venv/bin/map-violation-monitor run >> monitor.log 2>&1
```

Or with Docker, from the repo folder:

```bash
docker run --rm -v "$PWD":/app -w /app --env-file .env python:3.13-slim \
  sh -c "pip install -q . && map-violation-monitor run"
```

## How it works

1. **Collect.** One call per source and product to the
   [ScrapeUnblocker API](https://docs.scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
   through the official `scrapeunblocker` Python SDK:
   - [Amazon offers](https://docs.scrapeunblocker.com/plugins/amazon-offers?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
     (`client.amazon_offers`): every seller under "All buying options" for an ASIN, new condition.
   - [Google Shopping](https://docs.scrapeunblocker.com/plugins/google-shopping?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
     (`client.google_shopping`): the product cards of a search, one merchant each.
   - [eBay search](https://docs.scrapeunblocker.com/plugins/ebay?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
     (`client.ebay_search`): new Buy It Now listings priced between the floor and your MAP.
   - [idealo offers](https://docs.scrapeunblocker.com/plugins/idealo?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
     (`client.idealo_offers`): every shop in an idealo price comparison (DE, AT, UK, FR, IT, ES).

   Calls go out from the market's own country (Google Shopping from `country`, eBay from
   the marketplace's country), so prices and currency are what a local shopper sees.
2. **Process.** Each offer must be this product: new condition, your currency, every
   `match` term in the title (model numbers match whatever the spacing or dashes) and no
   `exclude` term (refurbished, used, open box, parts... and your own). Prices under
   `floor_percent` of MAP are dropped as accessories or wrong matches. Ignored sellers are
   dropped. What is left below MAP, less the tolerance, is grouped **per seller**: one
   finding at the seller's lowest price, with its number of listings.
3. **Compare with the last run.** A seller newly below MAP is delivered. An open finding
   is delivered again only when the price drops another `realert_drop_percent`. A finding
   is closed (back at MAP) when:
   - **Amazon, idealo**: the answer lists every offer, so the seller is gone from it or
     priced at MAP again;
   - **eBay, Google Shopping**: these show each visitor a different selection of listings,
     so a seller missing from one answer means little. The finding closes after
     `resolve_after_days` without a sighting below MAP.

   Closed findings go into the weekly report. A seller that goes below MAP again later
   opens a new episode, which counts towards the repeat offenders.
4. **Deliver.** One alert per run to every alert destination, one sheet row per event, the
   weekly report on the first run on or after `report.day`. If no alert destination
   accepts the alert, nothing is marked as sent and the same findings go out next run.
   Sheet rows that fail to write wait in the state file for the next run.
5. **State.** `state/findings.json` holds the open findings, the episode count per seller,
   recent back-at-MAP events, unwritten sheet rows and the week of the last report. The
   workflow commits it back after each run (one line per finding, so the diffs read well).
   Delete it to start over.

### Good to know

- **The first days on eBay and Google Shopping.** Because each answer is a sample, the
  next runs keep finding sellers the first one did not show. Each is delivered once. In
  the smoke test the second eBay answer, a minute after the first, held 147 sellers below
  MAP instead of 89, and none of the new ones had been in the first answer. Once the
  list fills up, the alerts slow down to the sellers that are really new.
- **You decide what is a violation.** The tool compares advertised prices with the number
  you give it. Promotions you allowed, authorized resellers (`sellers.authorized`) and
  your own store (`sellers.ignore`) are yours to configure. A seller below MAP is a
  finding to look into, not a verdict.
- **Variants.** An idealo price comparison and an eBay search can include colors and
  bundles. The variant name is shown in the alert; list a bundle you price differently as
  its own product with its own `match` and `exclude` words.
- **Debugging a result.** `map-violation-monitor run --dry-run --save-answers answers/`
  writes every API answer as JSON next to what the run concluded.

## Config reference

Everything lives in [`config.yml`](config.yml). Secrets never go there.

| Setting | Default | What it does |
|---|---|---|
| `products[].name` | required | Shown in alerts; also the product's id in the state file |
| `products[].sku` | | Your reference, shown in alerts and the sheet |
| `products[].map` | required | Your Minimum Advertised Price |
| `products[].currency` | required | `USD`, `EUR`, `GBP`, ... Offers in another currency are skipped |
| `products[].match` | required for searches | Terms every listing title must contain, e.g. `[WH-1000XM5]` |
| `products[].exclude` | | Terms that rule a listing out, e.g. another model or an accessory |
| `products[].amazon[]` | | `asin`, `marketplace` (`amazon.com`, `amazon.de`, ... 20 sites), `pages` (1-5, 10 offers each), `proxy_country` |
| `products[].google_shopping[]` | | `query`, `country` (2 letters), `proxy_country` (default: `country`) |
| `products[].ebay[]` | | `keyword`, `marketplace` (`ebay.com`, `ebay.de`, ... 19 sites), `listing_type` (`buy_it_now`, `all`, `auction`), `page_size` (60, 120, 240), `proxy_country` |
| `products[].idealo[]` | | `product_id` (the number in the price comparison link), `country` (`de`, `at`, `uk`, `fr`, `it`, `es`), `max_offers` (1-200) |
| `rules.tolerance_percent` | `0.5` | Prices less than this under MAP are not flagged |
| `rules.floor_percent` | `50` | Prices under this share of MAP are ignored as parts or wrong matches |
| `rules.include_shipping` | `false` | Compare price + shipping instead of the item price |
| `rules.realert_drop_percent` | `3` | Alert again when an open finding drops this much further |
| `rules.resolve_after_days` | `7` | eBay and Google Shopping: days without a sighting below MAP before a finding closes |
| `rules.exclude` | | More title terms to skip, on top of the built-in ones |
| `rules.keep_default_exclude` | `true` | `false` drops the built-in list (refurbished, used, open box, ...) |
| `sellers.ignore` | | Seller names or ids never reported (your own store, second-hand marketplaces) |
| `sellers.authorized` | | Seller names or ids marked "authorized reseller" |
| `alerts.to` | `[slack, teams, discord, webhook]` | Alert destinations, each used when its secret is set (`email` works too) |
| `alerts.max_per_source` | `8` | Sellers listed per product and source in one alert; the rest are counted |
| `alerts.back_at_map` | `false` | Also alert when a seller is back at MAP (always in the report) |
| `report.day` | `monday` | Weekday of the weekly report, or `off` |
| `report.to` | `[email]` | Report destinations: any of `slack`, `teams`, `discord`, `webhook`, `email` |
| `sheet.enabled` / `sheet.tab` | `true` / `Findings` | The Google Sheet log |
| `state` | `state/findings.json` | Where the state lives |

Seller names in `sellers.*` match whole words of the seller name, case-insensitive
(`Mercari` matches `mercari.com`), or an exact seller id (an Amazon seller id such as
`A1B2C3D4E5F6G7`, an eBay user name, an idealo shop id).

## Cost

Every run costs one API call per Amazon offers page, Google Shopping search and eBay
search, and one per 20 idealo offers. The run prints the count before it starts.

The default config is **4 API calls per run**: Amazon 1 + Google Shopping 1 + eBay 1 for
the US product, idealo 1 for the German one. At the daily schedule that is about 120 calls
a month. Ten products sold on all three US sources would be 30 calls a run. `report`,
`status` and `test-destinations` make no API calls.

## Adding another destination

Destinations live in [`src/map_violation_monitor/sinks.py`](src/map_violation_monitor/sinks.py)
behind one small interface: a `name`, `send(message)`, `preview(message)` and `test()`.
For example, a Google Chat space webhook:

```python
class GoogleChatSink(ChatSink):
    """Google Chat space webhook."""

    name = "gchat"
    style = messages.PLAIN

    def wrap(self, text: str) -> dict[str, Any]:
        return {"text": text}


REGISTRY["gchat"] = SinkSpec(
    ("GOOGLE_CHAT_WEBHOOK_URL",),
    lambda env, http: GoogleChatSink(env["GOOGLE_CHAT_WEBHOOK_URL"].strip(), http),
)
```

Add `"gchat"` to `ALERT_DESTINATIONS` in `config.py`, list it under `alerts.to`, and pass
the secret to the run step in `.github/workflows/run.yml`.

## Development

```bash
make install   # pip install -e ".[dev]"
make lint      # ruff check + ruff format --check
make test      # pytest: offline, the SDK and every destination are faked
make dry-run   # one real run that sends and saves nothing
```

The tests never call the API or post anywhere. `pre-commit install` runs ruff on every
commit.

## Links

- [ScrapeUnblocker](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos): the API behind the prices
- [Documentation](https://docs.scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos) and the [Python SDK](https://docs.scrapeunblocker.com/sdks/python?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
- [All plugins](https://docs.scrapeunblocker.com/plugins/overview?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
- Raw data instead of alerts: [amazon-scraper](https://github.com/ScrapeUnblocker/amazon-scraper) and [ebay-scraper](https://github.com/ScrapeUnblocker/ebay-scraper)
- Price drops for shoppers rather than brands: [amazon-price-drop-alerts](https://github.com/ScrapeUnblocker/amazon-price-drop-alerts)

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 ScrapeUnblocker.
