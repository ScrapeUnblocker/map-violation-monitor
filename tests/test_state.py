import json
from datetime import date, timedelta

import pytest
from test_process import TODAY, checked_all

from map_violation_monitor.process import BACK, compare
from map_violation_monitor.state import MAX_PENDING_ROWS, State, StateError, iso_week


def recorded(config):
    events, opened = compare(checked_all(config), {}, config.rules, TODAY, {})
    state = State()
    state.record(events, opened, TODAY)
    return state


def test_round_trip(config, tmp_path):
    state = recorded(config)
    state.report_week = "2026-W48"
    path = tmp_path / "state" / "findings.json"
    assert state.save(path)
    assert not state.save(path)  # unchanged: not written again
    again = State.load(path)
    assert again.first_run == "2026-11-25" and again.report_week == "2026-W48"
    assert again.open.keys() == state.open.keys()
    some = next(iter(again.open.values()))
    assert some.finding.map in (200.0, 189.0) and some.episode == 1
    assert again.episodes() == {k: 1 for k in state.open}


def test_one_line_per_finding(config, tmp_path):
    state = recorded(config)
    text = state.dumps()
    json.loads(text)
    lines = [line for line in text.splitlines() if '"seller_key"' in line]
    assert len(lines) == len(state.open) == 6


def test_back_at_map_is_remembered_for_the_report(config):
    from conftest import FakeClient, amazon_json

    state = recorded(config)
    later = TODAY + timedelta(days=2)
    fixed = FakeClient({"amazon": amazon_json([("Full Price Store", "A3FULLPRICE", 199.99)])})
    events, opened = compare(checked_all(config, fixed), state.open, config.rules, later, {})
    assert sum(e.kind == BACK for e in events) == 2
    state.record(events, opened, later)
    assert len(state.back) == 2
    assert {b["seller"] for b in state.back} == {"Skate World", "Gadget Outlet"}
    assert all(b["on"] == "2026-11-27" and b["days"] == 2 for b in state.back)
    state.prune(later + timedelta(days=40))
    assert state.back == []


def test_history_is_pruned_after_a_year(config):
    state = recorded(config)
    state.open = {}
    state.prune(TODAY + timedelta(days=366))
    assert state.history == {}


def test_queue_rows_keeps_the_newest():
    state = State()
    assert state.queue_rows([[i] for i in range(MAX_PENDING_ROWS)]) == 0
    assert state.queue_rows([["new"]]) == 1
    assert state.pending_rows[-1] == ["new"] and len(state.pending_rows) == MAX_PENDING_ROWS


def test_broken_file(tmp_path):
    path = tmp_path / "findings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        State.load(path)


def test_missing_file_is_empty(tmp_path):
    state = State.load(tmp_path / "nope.json")
    assert state.open == {} and not state.first_run


def test_iso_week():
    assert iso_week(date(2026, 11, 25)) == "2026-W48"
    assert iso_week(date(2027, 1, 1)) == "2026-W53"
