"""The state file, ``state/findings.json`` by default.

It holds the open findings (sellers below MAP right now, with the price last alerted),
how many times each seller has gone below MAP on each product, the sellers back at MAP in
the last weeks (for the report), sheet rows that failed to write, and the week of the last
report. The workflow commits it back to the repo, so it survives between scheduled runs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .process import BACK, NEW, Event, Opened

VERSION = 1
KEEP_BACK_DAYS = 35  # sellers back at MAP stay listed this long (the report looks 7 back)
KEEP_HISTORY_DAYS = 365  # repeat-offender counts reach this far back
MAX_PENDING_ROWS = 2000


class StateError(RuntimeError):
    """The state file can't be read or written."""


@dataclass
class State:
    first_run: str = ""  # ISO date of the first run
    report_week: str = ""  # ISO week of the last weekly report, e.g. 2026-W48
    open: dict[str, Opened] = field(default_factory=dict)
    history: dict[str, dict[str, Any]] = field(default_factory=dict)
    back: list[dict[str, Any]] = field(default_factory=list)
    pending_rows: list[list[Any]] = field(default_factory=list)

    # -- reading ------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            return cls(
                first_run=str(raw.get("first_run") or ""),
                report_week=str(raw.get("report_week") or ""),
                open={str(k): Opened.from_json(v) for k, v in (raw.get("open") or {}).items()},
                history={str(k): dict(v) for k, v in (raw.get("history") or {}).items()},
                back=[dict(b) for b in raw.get("back") or []],
                pending_rows=[list(r) for r in raw.get("pending_rows") or []],
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise StateError(f"cannot read {path}: {exc}") from None

    def episodes(self) -> dict[str, int]:
        return {k: int(v.get("episodes") or 0) for k, v in self.history.items()}

    # -- updating -----------------------------------------------------------------------

    def record(self, events: list[Event], opened: dict[str, Opened], today: date) -> None:
        """Take the open findings after a run, and remember new episodes and sellers back
        at MAP."""
        day = today.isoformat()
        self.first_run = self.first_run or day
        self.open = opened
        for e in events:
            f = e.finding
            if e.kind == NEW:
                self.history[f.key] = {
                    "product": f.product,
                    "source": f.source,
                    "market": f.market,
                    "seller": f.seller,
                    "episodes": e.episode,
                    "last": day,
                }
            elif e.kind == BACK:
                entry = self.history.setdefault(f.key, {"episodes": e.episode})
                entry["last"] = day
                self.back.append(
                    {
                        "key": f.key,
                        "product": f.product,
                        "source": f.source,
                        "market": f.market,
                        "seller": f.seller,
                        "last_price": f.price,
                        "currency": f.currency,
                        "map": f.map,
                        "first_seen": e.first_seen,
                        "on": day,
                        "days": e.days,
                        "episode": e.episode,
                    }
                )
        for key in self.open:
            if key in self.history:
                self.history[key]["last"] = day

    def queue_rows(self, rows: list[list[Any]]) -> int:
        """Keep sheet rows that could not be written. Returns how many were dropped
        because the queue is full."""
        self.pending_rows.extend(rows)
        over = len(self.pending_rows) - MAX_PENDING_ROWS
        if over > 0:
            del self.pending_rows[:over]
            return over
        return 0

    def prune(self, today: date) -> None:
        back_cut = (today - timedelta(days=KEEP_BACK_DAYS)).isoformat()
        self.back = [b for b in self.back if str(b.get("on") or "") >= back_cut]
        cut = (today - timedelta(days=KEEP_HISTORY_DAYS)).isoformat()
        self.history = {
            k: v for k, v in self.history.items() if k in self.open or str(v.get("last")) >= cut
        }

    # -- writing ------------------------------------------------------------------------

    def dumps(self) -> str:
        """JSON with one line per finding, so the committed file diffs well."""

        def obj(items: dict[str, Any]) -> str:
            if not items:
                return "{}"
            lines = [f"    {json.dumps(k)}: {_line(v)}" for k, v in sorted(items.items())]
            return "{\n" + ",\n".join(lines) + "\n  }"

        def arr(items: list[Any]) -> str:
            if not items:
                return "[]"
            return "[\n" + ",\n".join(f"    {_line(v)}" for v in items) + "\n  ]"

        parts = [
            f'  "version": {VERSION}',
            f'  "first_run": {json.dumps(self.first_run)}',
            f'  "report_week": {json.dumps(self.report_week)}',
            f'  "open": {obj({k: v.to_json() for k, v in self.open.items()})}',
            f'  "history": {obj(self.history)}',
            f'  "back": {arr(self.back)}',
            f'  "pending_rows": {arr(self.pending_rows)}',
        ]
        return "{\n" + ",\n".join(parts) + "\n}\n"

    def save(self, path: Path) -> bool:
        """Write the file when its content changed. Returns True when it was written."""
        text = self.dumps()
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            raise StateError(f"cannot write {path}: {exc}") from None
        return True


def _line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=isinstance(value, dict))


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"
