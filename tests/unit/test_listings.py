import json

import boto3
import pytest
from conftest import rest_event


@pytest.fixture
def api(aws):
    import importlib

    import listings_app

    return importlib.reload(listings_app)


def call(mod, ctx, method, path, **kw):
    resp = mod.handler(rest_event(method, path, **kw), ctx)
    body = json.loads(resp["body"]) if resp.get("body") else None
    return resp["statusCode"], body, resp.get("headers") or resp.get("multiValueHeaders") or {}


def header(headers, name):
    v = headers.get(name)
    return v[0] if isinstance(v, list) else v


def seed(crop="MZE", market="NDL", p=570, d="2026-10-26", prev=552):
    boto3.resource("dynamodb").Table("agriprice-test").put_item(Item={"PK": f"PRICE#{crop}#{market}", "SK": "CURRENT", "p": p, "d": d, "prev": prev})


def test_markets_dictionary_is_cacheable(api, ctx):
    status, body, headers = call(api, ctx, "GET", "/api/markets")
    assert status == 200 and len(body["m"]) == 20 and ["MZE", "Maize"] in body["c"]
    assert "max-age=3600" in header(headers, "Cache-Control")
    status, _, _ = call(api, ctx, "GET", "/api/markets", headers={"If-None-Match": header(headers, "ETag")})
    assert status == 304


def test_prices_compact_with_etag(api, ctx):
    seed()
    seed(market="LSK", p=600, prev=600)
    status, body, headers = call(api, ctx, "GET", "/api/prices", query={"c": "MZE", "lat": "-12.96", "lon": "28.64"})
    assert status == 200 and body[0] == {"c": "MZE", "m": "NDL", "p": 28500, "d": 20752, "t": 33}
    assert call(api, ctx, "GET", "/api/prices", query={"c": "MZE", "lat": "-12.96", "lon": "28.64"}, headers={"If-None-Match": header(headers, "ETag")})[0] == 304
    _, body, _ = call(api, ctx, "GET", "/api/prices", query={"c": "MZE", "m": "LSK,ZZZ"})
    assert [r["m"] for r in body] == ["LSK"]
    _, body, _ = call(api, ctx, "GET", "/api/prices", query={"c": "MZE"})
    assert {r["m"] for r in body} == {"NDL", "LSK"}
    assert call(api, ctx, "GET", "/api/prices", query={"c": "XXX"})[0] == 400
    assert call(api, ctx, "GET", "/api/prices", query={"c": "MZE", "lat": "x", "lon": "1"})[0] == 400


def test_demand_requires_buyer_group(api, ctx):
    body = {"c": "MZE", "q": 1000, "p": 600, "lat": -12.96, "lon": 28.64, "r": 50}
    assert call(api, ctx, "POST", "/api/demand", body=body)[0] == 403
    assert call(api, ctx, "POST", "/api/demand", body=body, groups=["officers"])[0] == 403


def test_demand_create_list_and_idempotency(api, ctx):
    body = {"c": "MZE", "q": 1000, "p": 600, "lat": -12.96, "lon": 28.64, "r": 50}
    s1, b1, _ = call(api, ctx, "POST", "/api/demand", body=body, groups=["buyers"], headers={"Idempotency-Key": "k1"})
    s2, b2, _ = call(api, ctx, "POST", "/api/demand", body=body, groups=["buyers"], headers={"Idempotency-Key": "k1"})
    assert s1 == s2 == 201 and b1 == b2
    call(api, ctx, "POST", "/api/demand", body=body, groups=["buyers"])
    status, mine, _ = call(api, ctx, "GET", "/api/demand/mine", groups=["buyers"])
    assert status == 200 and len(mine) == 2 and mine[0]["c"] == "MZE"
    bad = {**body, "q": "lots"}
    assert call(api, ctx, "POST", "/api/demand", body=bad, groups=["buyers"])[0] == 400
    assert call(api, ctx, "POST", "/api/demand", body={**body, "r": 999}, groups=["buyers"])[0] == 400


def test_officer_registers_farmer_lists_crop_and_buyer_sees_it(api, ctx):
    off = {"groups": ["officers"]}
    assert call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "KTW", "name": "Mary"}, **off)[0] == 201
    assert call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "ZZZ"}, **off)[0] == 400
    assert call(api, ctx, "POST", "/api/farmers", body={"phone": "0977123456", "m": "KTW", "lang": "fr"}, **off)[0] == 400
    assert call(api, ctx, "POST", "/api/listings", body={"phone": "0977999999", "c": "MZE", "q": 100}, **off)[0] == 404
    status, body, _ = call(api, ctx, "POST", "/api/listings", body={"phone": "0977123456", "c": "MZE", "q": 750}, **off)
    assert status == 201 and body["sk"].startswith("MZE#")
    status, rows, _ = call(api, ctx, "GET", "/api/listings", query={"c": "MZE", "lat": "-12.96", "lon": "28.64", "r": "60"}, groups=["buyers"])
    assert status == 200 and rows[0]["m"] == "KTW" and rows[0]["q"] == 750 and "***" in rows[0]["ph"]
    _, far, _ = call(api, ctx, "GET", "/api/listings", query={"c": "MZE", "lat": "-12.96", "lon": "28.64", "r": "5"}, groups=["buyers"])
    assert far == []


def test_field_price_goes_to_inbox(api, ctx):
    status, body, _ = call(api, ctx, "POST", "/api/field-prices", body={"c": "SOY", "m": "CPT", "p": 1450, "d": "2026-10-20"}, groups=["officers"])
    assert status == 202
    keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="agriprice-test-lake", Prefix="inbox/field/")["Contents"]]
    assert keys and keys[0].startswith("inbox/field/2026-10-20/")
    assert call(api, ctx, "POST", "/api/field-prices", body={"c": "SOY", "m": "CPT", "p": 1450, "d": "20-10-2026"}, groups=["officers"])[0] == 400
    assert call(api, ctx, "POST", "/api/field-prices", body={"c": "SOY", "m": "QQQ", "p": 1450}, groups=["officers"])[0] == 400


def test_invalid_json_body(api, ctx):
    ev = rest_event("POST", "/api/demand", groups=["buyers"])
    ev["body"] = "{not json"
    assert api.handler(ev, ctx)["statusCode"] == 400
    ev["body"] = "[1,2]"
    assert api.handler(ev, ctx)["statusCode"] == 400
