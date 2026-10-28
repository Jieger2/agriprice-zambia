"""Every stakeholder's use cases wired end to end (see docs/use-cases.md).

Farmer (USSD/SMS) · Buyer (web + USSD) · Extension officer (web) · Administrator (web)
"""

import json

import boto3
import pytest
from conftest import rest_event

from agricommon import repo

FARMER = "+260977123456"


@pytest.fixture
def ussd(aws):
    import importlib

    import ussd_app

    return importlib.reload(ussd_app)


@pytest.fixture
def api(aws):
    import importlib

    import listings_app

    return importlib.reload(listings_app)


def dial(mod, ctx, text, session, phone=FARMER):
    ev = rest_event("POST", "/ussd", body={"sessionId": session, "phoneNumber": phone, "text": text}, form=True, query={"k": "cb-token"})
    return mod.handler(ev, ctx)["body"]


def call(mod, ctx, method, path, **kw):
    resp = mod.handler(rest_event(method, path, **kw), ctx)
    return resp["statusCode"], (json.loads(resp["body"]) if resp.get("body") else None)


# ---------------- Farmer
def test_farmer_my_account_lists_and_cancels(ussd, ctx):
    dial(ussd, ctx, "5*2*1*Mary", "r")
    dial(ussd, ctx, "2*1*1*300*1*1", "s1")          # 300 kg maize
    dial(ussd, ctx, "2*2*2*4*2*1", "s2")            # 4 bags soya in 2 weeks
    dial(ussd, ctx, "3*1*1*350", "a1")              # maize above K350
    body = dial(ussd, ctx, "6", "m")
    assert "1.My listings (2)" in body and "2.My alerts (1)" in body and "3.My buy requests (0)" in body
    listings = dial(ussd, ctx, "6*1", "m")
    assert listings.startswith("CON Cancel which listing?") and "Soya 200kg" in listings and "Maize 300kg" in listings
    first = listings.split("\n")[1]  # newest first
    assert dial(ussd, ctx, "6*1*1", "m").startswith("END Listing cancelled: " + first[2:])
    assert "My listings (1)" in dial(ussd, ctx, "6", "m2")
    assert "Maize >K350 NDL" in dial(ussd, ctx, "6*2", "m3")
    assert dial(ussd, ctx, "6*2*1", "m3") == "END Alert removed: Maize >K350 NDL."
    assert dial(ussd, ctx, "6*2", "m4") == "END You have no price alerts."
    assert dial(ussd, ctx, "6*3", "m5") == "END You have no open buy requests."
    assert "Invalid" in dial(ussd, ctx, "6*9", "m6")
    assert "Invalid" in dial(ussd, ctx, "6*1*7", "m7")


def test_unregistered_cannot_use_account_or_buy(ussd, ctx):
    assert "register first" in dial(ussd, ctx, "6", "x")
    assert "register first" in dial(ussd, ctx, "7", "y")


def test_sms_stop_erases_profile_listings_and_alerts(ussd, ctx, monkeypatch):
    monkeypatch.setattr(ussd.sms, "send", lambda to, body: None)
    dial(ussd, ctx, "5*2*1*Mary", "r")
    dial(ussd, ctx, "2*1*1*300*1*1", "s1")
    dial(ussd, ctx, "3*1*1*350", "a1")
    ussd.handler(rest_event("POST", "/sms", body={"from": FARMER, "text": "STOP"}, form=True, query={"k": "cb-token"}, resource="/sms"), ctx)
    left = [i["PK"] for i in boto3.resource("dynamodb").Table("agriprice-test").scan()["Items"] if i["PK"].split("#")[0] in ("FARMER", "SUPPLY", "ALERT")]
    assert left == []


# ---------------- Buyer on a feature phone
def test_buyer_posts_demand_by_ussd_and_closes_it(ussd, ctx):
    dial(ussd, ctx, "5*2*1*Bwalya", "r", phone="+260966000222")
    assert dial(ussd, ctx, "7*1", "b", phone="+260966000222") == "CON How many kg of Maize do you want?"
    assert "Highest price" in dial(ussd, ctx, "7*1*2000", "b", phone="+260966000222")
    assert "3.100km" in dial(ussd, ctx, "7*1*2000*360", "b", phone="+260966000222")
    confirm = dial(ussd, ctx, "7*1*2000*360*2", "b", phone="+260966000222")
    assert confirm.startswith("CON Buy 2000kg Maize up to K360/50kg within 50km of Ndola?")
    assert dial(ussd, ctx, "7*1*2000*360*2*1", "b", phone="+260966000222").startswith("END Request posted")
    demand = [i for i in boto3.resource("dynamodb").Table("agriprice-test").scan()["Items"] if i["PK"].startswith("DEMAND#")][0]
    assert demand["owner"].startswith("ussd-") and demand["pmax"] == 720 and demand["r"] == 50 and demand["phone"] == "+260966000222"
    assert "My buy requests (1)" in dial(ussd, ctx, "6", "c", phone="+260966000222")
    assert dial(ussd, ctx, "6*3*1", "c", phone="+260966000222") == "END Buy request closed: Maize 2000kg <=K360."
    assert dial(ussd, ctx, "7*1*2000*360*2*2", "d", phone="+260966000222") == "END Cancelled."
    for bad in ("7*9", "7*1*x", "7*1*10*0", "7*1*10*300*5"):
        assert "Invalid" in dial(ussd, ctx, bad, "e" + bad, phone="+260966000222")


# ---------------- Buyer on the web
def test_web_buyer_closes_own_demand_only(api, ctx):
    body = {"c": "MZE", "q": 1000, "p": 600, "lat": -12.96, "lon": 28.64, "r": 50}
    _, made = call(api, ctx, "POST", "/api/demand", body=body, groups=["buyers"])
    assert call(api, ctx, "DELETE", f"/api/demand/{made['id']}", groups=["buyers"], sub="someone-else")[0] == 404
    assert call(api, ctx, "DELETE", f"/api/demand/{made['id']}", groups=["buyers"]) == (200, {"closed": made["id"]})
    assert call(api, ctx, "GET", "/api/demand/mine", groups=["buyers"])[1] == []


# ---------------- Extension officer
def test_officer_manages_own_farmers(api, ctx, ussd):
    off = {"groups": ["officers"], "sub": "officer-1"}
    call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "NDL", "name": "Mary"}, **off)
    call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 8, "u": "bag50"}, **off)
    _, farmers = call(api, ctx, "GET", "/api/officer/farmers", **off)
    pid = farmers[0]["pid"]
    assert farmers[0]["name"] == "Mary" and farmers[0]["listings"][0]["q"] == 400
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/alerts", body={"c": "MZE", "dir": "down", "k": 250}, **off)[0] == 201
    _, farmers = call(api, ctx, "GET", "/api/officer/farmers", **off)
    alert = farmers[0]["alerts"][0]
    assert alert["dir"] == "down" and alert["thr"] == 500 and alert["m"] == "NDL"
    # another officer cannot touch this farmer
    other = {"groups": ["officers"], "sub": "officer-2"}
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/alerts", body={"c": "MZE", "k": 250}, **other)[0] == 403
    assert call(api, ctx, "GET", "/api/officer/farmers", **other)[1] == []
    # the farmer re-registering by USSD keeps the officer link
    dial(ussd, ctx, "5*2*2*0", "rr")
    assert call(api, ctx, "GET", "/api/officer/farmers", **off)[1][0]["m"] == "KTW"
    listing = farmers[0]["listings"][0]
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/remove", body={"pk": listing["pk"], "sk": listing["sk"]}, **off) == (200, {"removed": True})
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/remove", body={"pk": listing["pk"], "sk": listing["sk"]}, **off)[0] == 404
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/alerts", body={"c": "MZE", "dir": "sideways", "k": 1}, **off)[0] == 400
    # admins may manage any farmer
    assert call(api, ctx, "POST", f"/api/officer/farmers/{pid}/alerts", body={"c": "SOY", "k": 700}, groups=["officers", "admins"], sub="admin-9")[0] == 201


def test_board_shows_open_supply(api, ctx):
    off = {"groups": ["officers"], "sub": "officer-1"}
    call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "KTW"}, **off)
    call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 750}, **off)
    _, board = call(api, ctx, "GET", "/api/officer/board", query={"r": "CB"}, **off)
    maize = next(r for r in board["rows"] if r["c"] == "MZE")
    assert maize["s"] == 750 and next(r for r in board["rows"] if r["c"] == "SOY")["s"] == 0


# ---------------- Administrator
def test_admin_overview_counts_activity(api, ctx, ussd, monkeypatch):
    monkeypatch.setattr(ussd.sms, "send", lambda to, body: None)
    dial(ussd, ctx, "5*2*1*Mary", "r")
    dial(ussd, ctx, "2*1*1*300*1*1", "s")
    dial(ussd, ctx, "3*1*1*350", "a")
    call(api, ctx, "POST", "/api/demand", body={"c": "MZE", "q": 1, "p": 1, "lat": -12.9, "lon": 28.6}, groups=["buyers"])
    call(api, ctx, "POST", "/api/field-prices", body={"c": "MZE", "m": "NDL", "p": 800}, groups=["officers"])
    assert call(api, ctx, "GET", "/api/admin/overview", groups=["officers"])[0] == 403
    _, stats = call(api, ctx, "GET", "/api/admin/overview", groups=["admins"], sub="admin-1")
    assert {k: stats["total"].get(k) for k in ("farmers", "listings", "alerts", "demands", "field_reports")} == {"farmers": 1, "listings": 1, "alerts": 1, "demands": 1, "field_reports": 1}
    assert len(stats["days"]) == 7 and stats["days"][0]["listings"] == 1


def test_counters_never_break_business_writes(aws, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("throttled")

    class T:
        update_item = staticmethod(boom)

    monkeypatch.setattr(repo, "_table", lambda: T())
    repo.bump(listings=1)  # swallowed
    repo.bump()  # no-op
