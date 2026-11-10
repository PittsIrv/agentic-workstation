"""The time panel: a Daybook reader behind a small interface. Every number here is made up."""
from datetime import date

import httpx
import pytest

from conftest import H, make
from workstation import timesources as ts

END = date(2026, 11, 10)


def day(d, total, scope="all_devices", **purposes):
    base = {"Focus": 0, "Entertainment": 0, "Other": 0, "Untagged": 0, **purposes}
    return {"date": d, "device_scope": scope, "complete": True, "total_seconds": total, "purpose_seconds": base,
            "entries": [{"name": "SECRET APP NAME", "seconds": total}]}


def fake(days, demo=False, health=None):
    def fetch(path):
        if path == "/api/health":
            return health if health is not None else {"application": "daybook", "demo": demo}
        return {"days": days, "demo": demo}
    return fetch


def read(days, scope="", **kw):
    return ts.window(ts.Daybook(scope=scope, fetch=fake(days, **kw)), END)


def test_totals_gaps_and_latest_day():
    w = read([day("2026-11-08", 7200, Focus=3600, Other=3600), day("2026-11-09", 3600, Entertainment=3600),
              day("2026-10-01", 999)])  # outside the window: ignored
    assert w["state"] == "connected" and w["scope"] == "all_devices"
    assert [d["recorded"] for d in w["days"]] == [False, False, False, False, True, True, False]
    assert w["total_seconds"] == 10800 and w["recorded_days"] == 2 and w["missing_days"] == 5
    assert w["by_purpose"] == {"Focus": 3600, "Entertainment": 3600, "Other": 3600, "Untagged": 0}
    assert w["latest_recorded"] == "2026-11-09" and w["today"]["recorded"] is False
    assert w["days"][0]["seconds"] is None  # missing is unknown, never zero


def test_app_names_never_leave_the_reader():
    assert "SECRET APP NAME" not in repr(read([day("2026-11-09", 3600)]))


def test_scopes_are_never_added_together():
    w = read([day("2026-11-09", 3600, scope="mac"), day("2026-11-09", 5400, scope="phone"),
              day("2026-11-08", 100, scope="mac")])
    assert w["scopes"] == ["mac", "phone"] and w["scope"] in ("mac", "phone")
    assert w["total_seconds"] in (3700, 5400)
    assert read([day("2026-11-09", 3600, scope="mac"), day("2026-11-09", 5400, scope="phone")], scope="phone")["total_seconds"] == 5400
    w = read([day("2026-11-09", 3600, scope="mac")], scope="nope")
    assert w["scope"] == "mac" and "not found" in w["message"]


def test_not_connected_says_how_to_connect():
    def refuse(path):
        raise httpx.ConnectError("refused")
    w = ts.window(ts.Daybook(fetch=refuse), END)
    assert w["state"] == "not_connected" and "launch.py" in w["message"] and w["days"][0]["recorded"] is False
    assert w["total_seconds"] == 0 and w["recorded_days"] == 0


def test_a_different_program_on_the_port_is_not_daybook():
    assert read([], health={"application": "something-else"})["state"] == "unreadable"
    assert read([], health=["nope"])["state"] == "unreadable"


def test_unreadable_and_empty_data():
    assert ts.window(ts.Daybook(fetch=lambda p: {"application": "daybook"} if p == "/api/health" else {"nope": 1}), END)["state"] == "unreadable"
    w = read([])
    assert w["state"] == "connected" and "no recorded days" in w["message"]


def test_hostile_or_malformed_rows_are_dropped_not_trusted():
    bad = [day("2026-11-09", -5), day("2026-11-09", 10**9), day("<script>", 5), {"date": "2026-11-09"}, "x", None,
           day("2026-11-09", True), {**day("2026-11-09", 60), "purpose_seconds": {"Focus": "lots", "Evil": 5}}]
    w = read(bad)
    assert w["recorded_days"] == 1 and w["total_seconds"] == 60 and w["by_purpose"]["Focus"] == 0
    w = read([day("2026-11-09", 60, scope="<script>alert(1)</script>" + "x" * 200)])
    assert len(w["scope"]) == 80


def test_demo_data_is_flagged():
    assert read([day("2026-11-09", 60)], demo=True)["demo"] is True


@pytest.mark.parametrize("url", ["http://example.com:8765", "https://127.0.0.1:8765", "http://10.0.0.5:8765",
                                 "http://127.0.0.1:8765/secret", "http://user:pw@127.0.0.1:8765", "file:///etc/passwd"])
def test_only_loopback_http_addresses_are_accepted(url):
    with pytest.raises(ValueError, match="loopback"):
        ts.Daybook(url)


def test_loopback_forms_are_accepted():
    for u in ("http://127.0.0.1:8765", "http://localhost:9000", "http://[::1]:8765/"):
        ts.Daybook(u)


def test_source_off_and_bad_config(tmp_path, agents):
    (tmp_path / "config.toml").write_text('[time]\nsource = "off"\n')
    app, c, tokens = make(tmp_path, agents)
    r = c.get("/api/time", headers=H(tokens)).json()
    assert r["state"] == "off" and "switched off" in r["message"]
    app.state.ws.close()
    for bad in ('source = "rize"', 'daybook_url = "http://example.com"'):
        (tmp_path / "config.toml").write_text(f"[time]\n{bad}\n")
        with pytest.raises(ValueError):
            make(tmp_path, agents)


def test_the_api_is_owner_only_and_validates_the_date(ws):
    app, c, tokens = ws
    app.state.ws.time_source = ts.Daybook(fetch=fake([day("2026-11-09", 3600, Focus=3600)]))
    for who in ("claude", "gpt", "gemini"):
        assert c.get("/api/time", headers=H(tokens, who)).status_code == 403
    assert c.get("/api/time").status_code == 401
    assert c.get("/api/time?end=soon", headers=H(tokens)).status_code == 422
    r = c.get("/api/time?end=2026-11-10", headers=H(tokens)).json()
    assert r["total_seconds"] == 3600 and r["end"] == "2026-11-10"


def test_the_default_server_with_no_daybook_says_not_connected(tmp_path, agents):
    (tmp_path / "config.toml").write_text('[time]\ndaybook_url = "http://127.0.0.1:1"\n')  # nothing listens here
    app, c, tokens = make(tmp_path, agents)
    assert c.get("/api/time", headers=H(tokens)).json()["state"] == "not_connected"


def test_nothing_is_written_to_the_event_log(ws):
    app, c, tokens = ws
    app.state.ws.time_source = ts.Daybook(fetch=fake([day("2026-11-09", 3600)]))
    before = len(app.state.ws.store.state.events)
    c.get("/api/time", headers=H(tokens)).raise_for_status()
    assert len(app.state.ws.store.state.events) == before
