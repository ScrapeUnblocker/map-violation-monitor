"""map-violation-monitor: every seller advertising your products below your Minimum
Advertised Price on Amazon, Google Shopping, eBay and idealo.

It reads every seller's price through the ScrapeUnblocker API, alerts Slack, Teams,
Discord, email or a webhook when a seller newly goes below MAP or drops further, logs
each finding to a Google Sheet and sends a weekly report.
"""

from .collect import Fetch, Offer, fetch
from .config import Config, ConfigError, Product, Target, load_config
from .process import Event, Finding, check, compare

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ConfigError",
    "Event",
    "Fetch",
    "Finding",
    "Offer",
    "Product",
    "Target",
    "__version__",
    "check",
    "compare",
    "fetch",
    "load_config",
]
