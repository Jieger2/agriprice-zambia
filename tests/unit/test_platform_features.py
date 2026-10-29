"""FR5, FR10, FR12 and the mock providers: admin catalog/sources/users, matches, officer board,
delta sync, SMS outbox, weather-adjusted weekly forecasts."""

import json

import boto3
import pytest
from conftest import rest_event

from agricommon import catalog, repo, sms, sources, weather


@pytest.fixture
def api(aws, monkeypatch):
    import importlib

    import listings_app

    monkeypatch.setenv("CATALOG_DYNAMIC", "1")
    catalog.invalidate()
    yield importlib.reload(listings_app)
    catalog.invalidate()


def call(mod, ctx, method, path, **kw):
    resp = mod.handler(rest_event(method, path, **kw), ctx)
    return resp["statusCode"], (json.loads(resp["body"]) if resp.get("body") else None)


ADMIN = {"groups": ["admins"], "sub": "admin-1"}


# ---------------- catalog (FR10)
def test_validate_catalog_rules():
    ok = catalog.validate_catalog({"markets": [{"code": "nkd", "name": "Nakonde", "province": "MUC", "lat": -9.33, "lon": 32.75}], "units": {"Tin (5 L)": 4.5}})
    assert ok["markets"][0]["code"] == "NKD" and ok["units"] == {"tin (5 l)": 4.5}
    for bad in (
        {"markets": [{"code": "N1", "name": "x", "province": "MUC", "lat": -9, "lon": 32}]},
        {"markets": [{"code": "NKD", "name": "x", "province": "ZZ", "lat": -9, "lon": 32}]},
        {"markets": [{"code": "NKD", "name": "x", "province": "MUC", "lat": 51.5, "lon": 0}]},
        {"commodities": [{"code": "SRG", "name": ""}]},
        {"units": {"bucket": 0}},
        [],
    ):
        with pytest.raises(ValueError):
            catalog.validate_catalog(bad)


def test_admin_catalog_round_trip_changes_every_service(api, ctx):
    assert call(api, ctx, "GET", "/api/admin/catalog", groups=["officers"])[0] == 403
    doc = {
        "markets": [{"code": "NKD", "name": "Nakonde", "province": "MUC", "lat": -9.33, "lon": 32.75}, {"code": "MNZ", "disabled": True}],
        "commodities": [{"code": "SRG", "name": "Sorghum", "match": ["sorghum"]}],
        "units": {"tin (5 l)": 4.5},
    }
    status, body = call(api, ctx, "PUT", "/api/admin/catalog", body=doc, **ADMIN)
    assert status == 200 and body["markets"] == 20 and body["commodities"] == 8
    status, body = call(api, ctx, "GET", "/api/admin/catalog", **ADMIN)
    codes = {m["code"] for m in body["markets"]}
    assert "NKD" in codes and "MNZ" not in codes and any(c["code"] == "SRG" for c in body["commodities"])
    # every consumer of the catalog now sees the change
    assert catalog.match_commodity("Sorghum (red)") == "SRG"
    assert catalog.to_ngwee_per_kg(45, "Tin (5 L)") == 1000
    _, markets = call(api, ctx, "GET", "/api/markets")
    assert any(m[0] == "NKD" for m in markets["m"])
    assert call(api, ctx, "PUT", "/api/admin/catalog", body={"units": {"x": -1}}, **ADMIN)[0] == 400


def test_catalog_falls_back_when_store_unreachable(monkeypatch):
    monkeypatch.setenv("CATALOG_DYNAMIC", "1")
    catalog.invalidate()

    def boom(name):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(repo, "get_config", boom)
    assert len(catalog.markets()) == 20  # baseline still served
    catalog.invalidate()


# ---------------- price sources (FR6 + FR10)
def test_sources_validate_and_merge(monkeypatch):
    monkeypatch.delenv("SOURCES_JSON", raising=False)
    doc = sources.validate({"sources": [
        {"id": "wfp_hdx", "type": "wfp_csv", "url": "https://example.org/zmb.csv", "label": "WFP"},
        {"id": "field", "enabled": False},
    ]})
    ids = [s["id"] for s in sources.effective(doc)]
    assert ids == ["seed", "wfp_hdx"]
    for bad in ({"sources": [{"id": "x", "type": "wfp_csv"}]}, {"sources": [{"id": "ok", "type": "wfp_csv", "url": "http://plain"}]},
                {"sources": [{"id": "ok", "type": "ftp"}]}, {"sources": [{"id": "a1", "type": "s3_inbox", "prefix": "raw/"}]},
                {"sources": [{"id": "a1", "type": "wfp_csv_s3", "key": "raw/x"}]},
                {"sources": [{"id": "dup", "type": "s3_inbox"}, {"id": "dup", "type": "s3_inbox"}]}, {"sources": "nope"}):
        with pytest.raises(ValueError):
            sources.validate(bad)


def test_admin_sources_and_run_now(api, ctx, monkeypatch):
    status, body = call(api, ctx, "PUT", "/api/admin/sources", body={"sources": [{"id": "wfp_hdx", "type": "wfp_csv", "url": "https://example.org/z.csv"}]}, **ADMIN)
    assert status == 200 and [s["id"] for s in body["effective"]] == ["seed", "field", "wfp_hdx"]
    _, body = call(api, ctx, "GET", "/api/admin/sources", **ADMIN)
    assert body["doc"]["sources"][0]["id"] == "wfp_hdx"
    assert call(api, ctx, "PUT", "/api/admin/sources", body={"sources": [{"id": "bad id!"}]}, **ADMIN)[0] == 400
    assert call(api, ctx, "POST", "/api/admin/ingest", body={}, **ADMIN)[0] == 400  # no function configured
    calls = []

    class FakeLambda:
        def invoke(self, **kw):
            calls.append(kw)

    monkeypatch.setenv("INGESTION_FUNCTION", "agriprice-test-ingestion:live")
    real_client = api.boto3.client
    monkeypatch.setattr(api.boto3, "client", lambda name, *a, **k: FakeLambda() if name == "lambda" else real_client(name, *a, **k))
    status, body = call(api, ctx, "POST", "/api/admin/ingest", body={"lookback_days": 30}, **ADMIN)
    assert status == 202 and calls[0]["InvocationType"] == "Event" and json.loads(calls[0]["Payload"])["lookback_days"] == 30


def test_ingestion_uses_admin_sources(aws, ctx, monkeypatch):
    import importlib

    import ingestion_app

    mod = importlib.reload(ingestion_app)
    monkeypatch.setattr(mod, "start_curation", lambda d, r: "q")
    monkeypatch.setenv("SOURCES_JSON", json.dumps([{"id": "seed", "type": "wfp_csv_s3", "key": "seed/missing.csv"}]))
    repo.put_config("sources", {"sources": [{"id": "seed", "enabled": False}, {"id": "field", "type": "s3_inbox", "prefix": "inbox/field/", "enabled": True}]}, "admin")
    out = mod.handler({}, ctx)
    assert [r.get("source") for r in out["results"]] == ["field"]  # disabled built-in skipped, admin source ran


# ---------------- users and roles (FR9/FR10) on a moto Cognito pool
@pytest.fixture
def pool(aws, monkeypatch):
    idp = boto3.client("cognito-idp")
    pid = idp.create_user_pool(PoolName="test")["UserPool"]["Id"]
    for g in ("buyers", "officers", "admins"):
        idp.create_group(UserPoolId=pid, GroupName=g)
    monkeypatch.setenv("USER_POOL_ID", pid)
    return idp, pid


def test_admin_invites_lists_and_changes_roles(api, ctx, pool):
    status, user = call(api, ctx, "POST", "/api/admin/users", body={"email": "Buyer@Coop.zm", "phone": "0966000111", "group": "buyers"}, **ADMIN)
    assert status == 201 and user["groups"] == ["buyers"] and user["email"] == "buyer@coop.zm"
    assert call(api, ctx, "POST", "/api/admin/users", body={"email": "nope", "group": "buyers"}, **ADMIN)[0] == 400
    name = user["username"]
    status, body = call(api, ctx, "POST", f"/api/admin/users/{name}/roles", body={"group": "officers", "action": "add"}, **ADMIN)
    assert body["groups"] == ["buyers", "officers"]
    _, body = call(api, ctx, "POST", f"/api/admin/users/{name}/roles", body={"group": "buyers", "action": "remove"}, **ADMIN)
    assert body["groups"] == ["officers"]
    _, users = call(api, ctx, "GET", "/api/admin/users", **ADMIN)
    assert [u["groups"] for u in users if u["username"] == name] == [["officers"]]
    assert call(api, ctx, "POST", f"/api/admin/users/{name}/roles", body={"group": "kings", "action": "add"}, **ADMIN)[0] == 400


def test_admin_cannot_remove_own_admin_role(api, ctx, pool):
    path = "/api/admin/users/admin-1/roles"
    assert call(api, ctx, "POST", path, body={"group": "admins", "action": "remove"}, **ADMIN)[0] == 400


def test_users_endpoint_needs_pool(api, ctx, monkeypatch):
    monkeypatch.delenv("USER_POOL_ID", raising=False)
    assert call(api, ctx, "GET", "/api/admin/users", **ADMIN)[0] == 400


# ---------------- matches (FR5), board, delta sync
def test_matches_are_visible_to_the_owning_buyer_only(api, ctx):
    demand = {"id": "d1", "owner": "user-1"}
    listing = {"SK": "MZE#1#p", "crop": "MZE", "q": 500, "market": "KTW", "phone": "+260977000001", "from": "2026-11-01"}
    repo.put_match(demand, listing, 42.04)
    status, rows = call(api, ctx, "GET", "/api/demand/d1/matches", groups=["buyers"])
    assert status == 200 and rows == [{"q": 500, "m": "KTW", "km": 42.0, "f": "2026-11-01", "ph": "+260977000001"}]
    _, rows = call(api, ctx, "GET", "/api/demand/d1/matches", groups=["buyers"], sub="someone-else")
    assert rows == []


def test_officer_board_and_delta_sync(api, ctx):
    t = boto3.resource("dynamodb").Table("agriprice-test")
    t.put_item(Item={"PK": "PRICE#MZE#NDL", "SK": "CURRENT", "p": 570, "d": "2026-10-26", "prev": 552})
    t.put_item(Item={"PK": "PRICE#MZE#KTW", "SK": "CURRENT", "p": 560, "d": "2026-09-15", "prev": 560})
    status, board = call(api, ctx, "GET", "/api/officer/board", query={"r": "CB"}, groups=["officers"])
    maize = next(r for r in board["rows"] if r["c"] == "MZE")
    assert status == 200 and set(board["m"]) == {"NDL", "KTW", "CHG"} and maize["p"]["NDL"]["p"] == 28500
    assert call(api, ctx, "GET", "/api/officer/board", query={"r": "XX"}, groups=["officers"])[0] == 400
    _, rows = call(api, ctx, "GET", "/api/prices", query={"c": "MZE", "m": "NDL,KTW", "since": "20730"})
    assert [r["m"] for r in rows] == ["NDL"]  # only the record observed after day 20730


def test_listing_with_unit_and_available_date(api, ctx):
    off = {"groups": ["officers"]}
    call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "NDL", "name": "Mary"}, **off)
    status, _ = call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 12, "u": "bag50", "from": "2026-11-15"}, **off)
    assert status == 201
    _, rows = call(api, ctx, "GET", "/api/listings", query={"c": "MZE", "lat": "-12.96", "lon": "28.64"}, groups=["buyers"])
    assert rows[0]["q"] == 600 and rows[0]["u"] == "bag50" and rows[0]["f"] == "2026-11-15"
    assert call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 1, "u": "sack"}, **off)[0] == 400
    assert call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 1, "from": "soon"}, **off)[0] == 400


# ---------------- mock providers
def test_sms_outbox_mode(aws, monkeypatch):
    monkeypatch.setenv("SMS_MODE", "outbox")
    sms.send("+260977000001", "first")
    sms.send("+260977000001", "second – dash")
    msgs = repo.outbox("+260977000001")
    assert [m["body"] for m in msgs] == ["second - dash", "first"]  # newest first, GSM-7 cleaned


def test_weather_modes_and_adjustment(monkeypatch):
    monkeypatch.setenv("WEATHER_MODE", "off")
    assert weather.rainfall_ratio("SOU") is None
    monkeypatch.setenv("WEATHER_MODE", "mock")
    assert weather.rainfall_ratio("SOU") == 0.6
    assert weather.price_adjustment(0.6, 1) == pytest.approx(1.04)
    assert weather.price_adjustment(1.6, 12) == pytest.approx(0.985)
    assert weather.price_adjustment(0.6, 7) == 1.0 and weather.price_adjustment(None, 1) == 1.0
    assert weather.price_adjustment(0.0, 1) == pytest.approx(1.08)
    lat, lon = weather.province_centroid("CB")
    assert -13.5 < lat < -12 and 27 < lon < 29


def test_weather_live_mode_parses_open_meteo(monkeypatch):
    from datetime import date

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"daily": {"precipitation_sum": [4.0, 4.0, None, 4.0]}}).encode()

    monkeypatch.setenv("WEATHER_MODE", "live")
    monkeypatch.setattr(weather.urllib.request, "urlopen", lambda url, timeout: Resp())
    assert weather.rainfall_ratio("LSK", date(2026, 1, 10)) == pytest.approx(0.5)
    assert weather.rainfall_ratio("LSK", date(2026, 7, 10)) is None  # dry season: no meaningful normal

    def down(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(weather.urllib.request, "urlopen", down)
    assert weather.rainfall_ratio("LSK", date(2026, 1, 10)) is None


def test_weekly_forecast_with_weather():
    from datetime import date

    from analytics_app import build_payload, weekly_forecast

    weeks = weekly_forecast(500, 560, date(2026, 1, 1))
    assert [w[0] for w in weeks] == ["2026-01-08", "2026-01-15", "2026-01-22", "2026-01-29"] and weeks[-1][1] == 556
    series = [(f"2025-{m:02d}", 500 + m * 10, 480, 560) for m in range(1, 13)]
    dry = build_payload(series, weather_ratio=0.5, today=date(2026, 1, 5))
    normal = build_payload(series, weather_ratio=None, today=date(2026, 1, 5))
    assert len(dry["w"]) == 4 and dry["f"][0][1] > normal["f"][0][1] and dry["wx"]["adj"] > 1


def test_role_change_errors_are_clean(api, ctx, pool):
    assert call(api, ctx, "POST", "/api/admin/users/ghost%40x.zm/roles", body={"group": "buyers", "action": "add"}, **ADMIN)[0] == 404
    call(api, ctx, "POST", "/api/admin/users", body={"email": "a@b.zm", "phone": "0966000111", "group": "buyers"}, **ADMIN)
    assert call(api, ctx, "POST", "/api/admin/users", body={"email": "a@b.zm", "phone": "0966000111", "group": "buyers"}, **ADMIN)[0] == 400
    status, body = call(api, ctx, "POST", "/api/admin/users/a%40b.zm/roles", body={"group": "admins", "action": "add"}, **ADMIN)
    assert status == 200 and "admins" in body["groups"]


def test_sources_json_survives_windows_quote_stripping(monkeypatch):
    """sam.cmd on Windows strips double quotes, so deploy.py sends the sources base64-encoded."""
    import sys
    from pathlib import Path

    from agricommon import sources

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import deploy

    monkeypatch.setenv("SOURCES_JSON", deploy.sources_json())
    assert '"' not in deploy.sources_json() and "=" not in deploy.sources_json()
    assert [s["id"] for s in sources.builtin()] == ["seed", "field"]
    monkeypatch.setenv("SOURCES_JSON", '[{"id":"x","type":"s3_inbox","prefix":"p/"}]')
    assert sources.builtin()[0]["id"] == "x"
