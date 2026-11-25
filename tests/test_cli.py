import io
import json
from datetime import timedelta

import pytest
from conftest import NOW, FakeClient, FakeHttp, FakeSmtp, amazon_json

from map_violation_monitor import cli, sinks
from map_violation_monitor.state import State


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    monkeypatch.setattr(sinks, "_connect", FakeSmtp)


class Run:
    """Runs the CLI with fakes and keeps what it printed and posted."""

    def __init__(self, config_path, env):
        self.config_path = config_path
        self.env = env
        self.http = FakeHttp()
        self.client = FakeClient()

    def __call__(self, *args, now=NOW, client=None):
        out, err = io.StringIO(), io.StringIO()
        client = client or self.client
        code = cli.main(
            [args[0], "-c", str(self.config_path), "--env-file", "none", *args[1:]],
            environ=dict(self.env),
            client_factory=lambda env: client,
            http_factory=lambda: self.http,
            now=now,
            out=out,
            err=err,
        )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    @property
    def state(self):
        return State.load(self.config_path.parent / "state" / "findings.json")


@pytest.fixture
def run(config_path, env):
    return Run(config_path, env)


def test_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--dry-run", "--product", "DE", "--product", "US"])
    assert args.command == "run" and args.dry_run and args.product == ["DE", "US"]
    assert args.config == "config.yml" and args.env_file == ".env"
    args = parser.parse_args(["report", "--force", "-c", "other.yml"])
    assert args.force and args.config == "other.yml"
    for command in ("status", "test-destinations"):
        assert parser.parse_args([command]).command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["scrape"])


def test_first_run_delivers_everywhere(run):
    assert run("run") == 0
    assert "Checking 2 products on 4 sources: 4 API calls." in run.out
    assert "Delivered to slack, teams, discord, webhook: Below MAP: 6 sellers" in run.out
    assert "Google Sheet: appended 6 rows to Findings." in run.out
    assert len(run.http.posts("hooks.slack.test")) == 1
    assert len(run.http.posts("teams.test")) == 1
    assert len(run.http.posts("hooks.test/map")[0]["events"]) == 6
    assert len(run.http.tabs["Findings"]) == 7  # header + 6
    assert len(run.state.open) == 6 and run.state.first_run == "2026-11-25"
    assert FakeSmtp.sent == []  # the report waits for Monday


def test_second_run_with_no_change_sends_nothing(run):
    run("run")
    posted = len(run.http.requests)
    assert run("run", now=NOW + timedelta(hours=1)) == 0
    assert "Nothing new to alert." in run.out
    sends = [r for r in run.http.requests[posted:] if r["method"] == "POST"]
    assert [r["url"] for r in sends] == []
    assert len(run.http.tabs["Findings"]) == 7


def test_dry_run_delivers_and_saves_nothing(run):
    assert run("run", "--dry-run") == 0
    assert "Dry run: would deliver Below MAP: 6 sellers on 2 products (first check)" in run.out
    assert "--- slack ---" in run.out and "--- teams ---" in run.out
    assert "Dry run: would append 6 rows for tab 'Findings'" in run.out
    assert "Dry run: nothing was sent or saved." in run.out
    assert [r for r in run.http.requests if r["method"] in ("POST", "PUT")] == []
    assert not (run.config_path.parent / "state" / "findings.json").exists()


def test_failed_delivery_is_retried_next_run(run):
    for name in ("slack", "teams", "discord", "hooks.test"):
        run.http.fail[name] = 500
    assert run("run") == 1
    assert "Not delivered anywhere" in run.out
    assert run.state.open == {}
    run.http.fail.clear()
    assert run("run") == 0
    assert "Below MAP: 6 sellers on 2 products (first check)" in run.out


def test_sheet_failure_keeps_rows_for_next_run(run):
    run.http.fail["sheets.test"] = 403
    assert run("run") == 1
    assert len(run.state.pending_rows) == 6 and len(run.state.open) == 6
    run.http.fail.clear()
    run("run")
    assert "Google Sheet: appended 6 rows" in run.out
    assert run.state.pending_rows == []


def test_weekly_report_goes_out_once_on_monday(run):
    run("run")
    monday = NOW + timedelta(days=5)
    assert run("run", now=monday) == 0
    assert "Weekly report delivered to email, slack" in run.out
    assert FakeSmtp.sent[0]["Subject"].startswith("MAP report · week 49: 6 sellers below MAP")
    assert run.state.report_week == "2026-W49"
    FakeSmtp.sent.clear()
    run("run", now=monday + timedelta(hours=2))
    assert FakeSmtp.sent == []
    assert run("report", now=monday) == 0 and "went out already" in run.out
    assert run("report", "--force", "--dry-run", now=monday) == 0
    assert "Dry run: would send the weekly report" in run.out and FakeSmtp.sent == []


def test_deeper_undercut_and_back_at_map(run):
    run("run")
    later = FakeClient({"amazon": amazon_json([("Gadget Outlet", "A2GADGETOUT", 160.0)])})
    run("run", now=NOW + timedelta(days=1), client=later)
    assert "Below MAP: 1 further down on 1 product" in run.out
    tab = run.http.tabs["Findings"]
    assert [r[1] for r in tab[7:]] == ["further below MAP", "back at MAP"]


def test_product_filter(run):
    run("run", "--product", "(DE)")
    assert "Checking 1 product on 1 source: 1 API call." in run.out
    assert [c[0] for c in run.client.calls] == ["idealo"]
    assert run("run", "--product", "nothing") == 2
    assert "matches no product" in run.err


def test_save_answers(run, tmp_path):
    run("run", "--dry-run", "--product", "(DE)", "--save-answers", str(tmp_path / "answers"))
    saved = list((tmp_path / "answers").glob("*-idealo-de-123456789.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8"))["productId"] == "123456789"


def test_status(run):
    run("status")
    assert "No run yet" in run.out
    run("run")
    run("status")
    assert "Rocket Skates RS-200 (US) · MAP $200.00 · 4 sellers below MAP" in run.out
    assert "Amazon (amazon.com) · Gadget Outlet" in run.out


def test_test_destinations(run):
    assert run("test-destinations") == 0
    assert "sheet    ok, tab 'Findings' created with its header row" in run.out
    assert "email    ok, test email sent to brand@acme.test" in run.out


def test_missing_key_and_bad_config(run, write_config):
    run.env.pop("SCRAPEUNBLOCKER_KEY")
    assert run("run") == 2
    assert "SCRAPEUNBLOCKER_KEY" in run.err and "utm_source=github" in run.err
    write_config("products: []\n")
    assert run("status") == 2 and "products" in run.err


def test_no_destinations_prints_the_alert(run):
    for name in list(run.env):
        if name != "SCRAPEUNBLOCKER_KEY":
            run.env.pop(name)
    assert run("run") == 0
    assert "No alert destination is set up" in run.out
    assert "🆕 Amazon (amazon.com) · Gadget Outlet" in run.out
    assert len(run.state.open) == 6


def test_github_summary_and_annotations(run, tmp_path):
    from scrapeunblocker import InvalidRequestError

    summary = tmp_path / "summary.md"
    run.env.update(GITHUB_ACTIONS="true", GITHUB_STEP_SUMMARY=str(summary))
    run.client.answers["amazon"] = InvalidRequestError("no such ASIN", status_code=400)
    assert run("run") == 1
    assert "::warning title=Amazon (amazon.com) not read::B0TESTRS20" in run.out
    text = summary.read_text(encoding="utf-8")
    assert "| Product | MAP | Source | Seller | Price | Below MAP | Since |" in text
    assert "Roller Depot" in text
