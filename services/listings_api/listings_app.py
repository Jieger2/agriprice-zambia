"""Service 2 - Farmer & Listings API (REST, behind API Gateway + Cognito).

Public (no auth, cacheable, compact):
  GET  /api/markets                  reference dictionary (fetched once, cached a day)
  GET  /api/prices?c=MZE[&lat&lon|&m=NDL]   compact current prices, ETag/304
Authenticated (Cognito JWT; group checked here):
  GET  /api/demand/<id>/matches      sellers matched to my demand  buyers
  GET  /api/officer/board?r=CB       regional price dashboard    officers
  GET|PUT /api/admin/catalog         markets, crops, units       admins
  GET|PUT /api/admin/sources         price-source configuration  admins
  POST /api/admin/ingest             run ingestion now           admins
  GET|POST /api/admin/users, POST /api/admin/users/<u>/roles     admins
  GET  /api/listings?c&lat&lon&r     nearby supply              buyers, officers
  POST /api/demand                   post buyer demand          buyers
  GET  /api/demand/mine              my demands                 buyers
  POST /api/farmers                  register a farmer          officers
  POST /api/listings                 list crop for a farmer     officers
  POST /api/field-prices             submit a field price       officers

POST requests accept an Idempotency-Key header so retries from flaky 2G links
never create duplicates.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from datetime import date

import boto3
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, ForbiddenError, NotFoundError
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import DynamoDBPersistenceLayer, IdempotencyConfig, idempotent_function
from botocore.exceptions import ClientError

from agricommon import bus, catalog, codec, geo, obs, privacy, repo, sources

logger, tracer, metrics = obs.tools("listings-api")
app = APIGatewayRestResolver()

_persistence = DynamoDBPersistenceLayer(table_name=os.environ.get("IDEMPOTENCY_TABLE", "agriprice-dev-idempotency"))
UNIT_KG = {"kg": 1, "bag50": 50}
_idem_config = IdempotencyConfig(event_key_jmespath="key", expires_after_seconds=24 * 3600)


# ---------------------------------------------------------------- helpers
def _json(body, status: int = 200, cache: str = "no-store", headers: dict | None = None) -> Response:
    payload = codec.dumps(body)
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=payload, headers={"Cache-Control": cache, **(headers or {})})


def _cached_json(body, max_age: int) -> Response:
    payload = codec.dumps(body)
    tag = codec.etag(payload)
    if app.current_event.headers.get("If-None-Match") == tag or app.current_event.headers.get("if-none-match") == tag:
        return Response(status_code=304, body="", headers={"ETag": tag, "Cache-Control": f"public, max-age={max_age}"})
    return Response(status_code=200, content_type=content_types.APPLICATION_JSON, body=payload, headers={"ETag": tag, "Cache-Control": f"public, max-age={max_age}"})


def _claims() -> dict:
    return ((app.current_event.request_context.authorizer or {}).get("claims")) or {}


def _require(*groups: str) -> dict:
    claims = _claims()
    raw = claims.get("cognito:groups") or ""
    member = set(raw.replace("[", "").replace("]", "").replace(",", " ").split()) if isinstance(raw, str) else set(raw)
    if not claims.get("sub") or not member.intersection(groups + ("admins",)):
        raise ForbiddenError("insufficient role")
    return claims


def _body() -> dict:
    try:
        body = app.current_event.json_body or {}
    except (ValueError, TypeError) as exc:
        raise BadRequestError("invalid JSON") from exc
    if not isinstance(body, dict):
        raise BadRequestError("JSON object expected")
    return body


def _int(v, name: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"{name} must be an integer") from exc
    if not lo <= n <= hi:
        raise BadRequestError(f"{name} out of range")
    return n


def _float(v, name: str, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"{name} must be a number") from exc
    if not lo <= n <= hi:
        raise BadRequestError(f"{name} out of range")
    return n


def _crop(code) -> str:
    if code not in catalog.commodity_codes():
        raise BadRequestError("unknown commodity code")
    return code


def _idempotent(op: str, fn, body: dict):
    key = app.current_event.headers.get("Idempotency-Key") or app.current_event.headers.get("idempotency-key")
    if not key:
        return fn(body)
    caller = _claims().get("sub", "anon")

    @idempotent_function(data_keyword_argument="payload", persistence_store=_persistence, config=_idem_config)
    def run(payload: dict) -> dict:
        return fn(payload["body"])

    return run(payload={"key": f"{op}:{caller}:{key}", "body": body})


def _cid() -> str:
    return app.current_event.headers.get("X-Correlation-Id") or app.current_event.request_context.request_id


# ---------------------------------------------------------------- public routes
@app.get("/api/markets")
def get_markets():
    return _cached_json({
        "m": [[m.code, m.name, m.province, m.lat, m.lon] for m in catalog.markets().values()],
        "c": [[c["code"], c["name"]] for c in catalog.commodities()],
        "p": [[p["code"], p["name"]] for p in catalog.provinces()],
    }, max_age=3600)


@app.get("/api/prices")
def get_prices():
    q = app.current_event.query_string_parameters or {}
    crop = _crop(q.get("c", "MZE"))
    mk = catalog.markets()
    if q.get("m"):
        codes = [c for c in q["m"].split(",") if c in mk][:10]
    elif q.get("lat") and q.get("lon"):
        lat, lon = _float(q["lat"], "lat", -90, 90), _float(q["lon"], "lon", -180, 180)
        codes = [m.code for m, _ in catalog.nearest_markets(lat, lon, 5)]
    else:
        codes = list(mk)
    prices = repo.get_prices(crop, codes)
    rows = [codec.price_record(crop, code, p["p"], p["d"], p.get("prev") or None) for code, p in prices.items()]
    rows.sort(key=lambda r: codes.index(r["m"]))
    if q.get("since"):  # delta sync: only records observed after the client's newest day number
        since = _int(q["since"], "since", 0, 100_000)
        rows = [r for r in rows if r["d"] > since]
    metrics.add_metric(name="PriceLookups", unit=MetricUnit.Count, value=1)
    return _cached_json(rows, max_age=300)


# ---------------------------------------------------------------- buyers
@app.get("/api/listings")
def nearby_supply():
    _require("buyers", "officers")
    q = app.current_event.query_string_parameters or {}
    crop = _crop(q.get("c"))
    lat, lon = _float(q.get("lat"), "lat", -90, 90), _float(q.get("lon"), "lon", -180, 180)
    radius = _int(q.get("r", 50), "r", 1, geo.MAX_RADIUS_KM)
    items = repo.query_cells("SUPPLY", geo.neighbours(geo.cell(lat, lon)), crop)
    out = []
    for it in items:
        km = geo.haversine_km(lat, lon, float(it["lat"]), float(it["lon"]))
        if km <= radius:
            out.append({"q": it["q"], "u": it.get("unit", "kg"), "f": it.get("from", ""), "m": it["market"], "km": round(km), "ts": it["ts"], "ph": privacy.mask(it["phone"])})
    out.sort(key=lambda r: r["km"])
    return _json(out[:50])


def _create_demand(body: dict) -> dict:
    claims = _claims()
    crop = _crop(body.get("c"))
    qty = _int(body.get("q"), "q", 1, 10_000_000)
    pmax = _int(body.get("p"), "p", 1, 10_000_000)  # ngwee per kg
    lat, lon = _float(body.get("lat"), "lat", -90, 90), _float(body.get("lon"), "lon", -180, 180)
    radius = _int(body.get("r", 50), "r", 1, geo.MAX_RADIUS_KM)
    phone = privacy.normalise_phone(claims.get("phone_number") or body.get("phone", ""))
    demand_id = uuid.uuid4().hex[:12]
    cell = geo.cell(lat, lon)
    item = repo.put_demand(cell, crop, demand_id, claims["sub"], phone, qty, pmax, lat, lon, radius)
    bus.publish_event("DemandPosted", {"pk": f"DEMAND#{cell}", "sk": item["SK"]}, _cid())
    metrics.add_metric(name="DemandsPosted", unit=MetricUnit.Count, value=1)
    return {"id": demand_id}


@app.post("/api/demand")
def post_demand():
    _require("buyers")
    return _json(_idempotent("demand", _create_demand, _body()), status=201)


@app.get("/api/demand/mine")
def my_demand():
    claims = _require("buyers")
    rows = repo.demands_by_owner(claims["sub"])
    return _json([{"id": r["id"], "c": r["crop"], "q": r["q"], "p": r["pmax"], "r": r["r"], "ts": r["ts"], "n": len(repo.matches_for(r["id"]))} for r in rows])


@app.get("/api/demand/<demand_id>/matches")
def demand_matches(demand_id: str):
    """Sellers matched to one of my demands (written by the Matching service). Buyers see full numbers to call."""
    claims = _require("buyers")
    rows = [m for m in repo.matches_for(demand_id) if m.get("owner") == claims["sub"]]
    rows.sort(key=lambda m: m["km"])
    return _json([{"q": m["q"], "m": m["market"], "km": m["km"], "f": m.get("from", ""), "ph": m["phone"]} for m in rows])


# ---------------------------------------------------------------- officers
def _register_farmer(body: dict) -> dict:
    phone = privacy.normalise_phone(body.get("phone", ""))
    market = body.get("m")
    if market not in catalog.markets():
        raise BadRequestError("unknown market code")
    lang = body.get("lang", "en")
    if lang not in ("en", "bem", "nya"):
        raise BadRequestError("unsupported language")
    pid = privacy.phone_id(phone)
    repo.put_farmer(pid, phone, market, lang, str(body.get("name", ""))[:60], registered_by=_claims().get("sub", "officer"))
    metrics.add_metric(name="FarmersRegistered", unit=MetricUnit.Count, value=1)
    return {"id": pid}


@app.post("/api/farmers")
def post_farmer():
    _require("officers")
    return _json(_idempotent("farmer", _register_farmer, _body()), status=201)


def _create_listing(body: dict) -> dict:
    phone = privacy.normalise_phone(body.get("phone", ""))
    pid = privacy.phone_id(phone)
    farmer = repo.get_farmer(pid)
    if not farmer or not farmer.get("market"):
        raise NotFoundError("farmer not registered")
    crop = _crop(body.get("c"))
    unit = body.get("u", "kg")
    if unit not in UNIT_KG:
        raise BadRequestError("u must be kg or bag50")
    qty = _int(body.get("q"), "q", 1, 1_000_000) * UNIT_KG[unit]
    available = body.get("from") or date.today().isoformat()
    try:
        date.fromisoformat(available)
    except ValueError as exc:
        raise BadRequestError("from must be YYYY-MM-DD") from exc
    m = catalog.markets().get(farmer["market"])
    if not m:
        raise BadRequestError("farmer's market is no longer active; re-register the farmer")
    cell = geo.cell(m.lat, m.lon)
    item = repo.put_supply(cell, crop, pid, farmer["phone"], qty, m.code, m.lat, m.lon, unit, available)
    bus.publish_event("ListingCreated", {"pk": f"SUPPLY#{cell}", "sk": item["SK"]}, _cid())
    metrics.add_metric(name="ListingsCreated", unit=MetricUnit.Count, value=1)
    return {"sk": item["SK"]}


@app.post("/api/listings")
def post_listing():
    _require("officers")
    return _json(_idempotent("listing", _create_listing, _body()), status=201)


def _field_price(body: dict) -> dict:
    crop = _crop(body.get("c"))
    market = body.get("m")
    if market not in catalog.markets():
        raise BadRequestError("unknown market code")
    p = _int(body.get("p"), "p", 1, 10_000_000)  # ngwee per kg
    obs_date = body.get("d") or date.today().isoformat()
    try:
        date.fromisoformat(obs_date)
    except ValueError as exc:
        raise BadRequestError("d must be YYYY-MM-DD") from exc
    key = f"inbox/field/{obs_date}/{uuid.uuid4().hex}.json"
    record = {"c": crop, "m": market, "p": p, "d": obs_date, "by": _claims().get("sub", "officer")}
    boto3.client("s3").put_object(Bucket=os.environ["LAKE_BUCKET"], Key=key, Body=json.dumps(record).encode(), ContentType="application/json")
    return {"queued": key.rsplit("/", 1)[-1]}


@app.post("/api/field-prices")
def post_field_price():
    _require("officers")
    return _json(_idempotent("field", _field_price, _body()), status=202)


@app.get("/api/officer/board")
def regional_board():
    """Regional dashboard (stakeholder table 4.1): every crop's current price in every market of a province."""
    _require("officers")
    q = app.current_event.query_string_parameters or {}
    region = q.get("r", "")
    if region not in catalog.province_codes():
        raise BadRequestError("unknown province code")
    codes = [m.code for m in catalog.markets_in_province(region)]
    rows = []
    for crop in catalog.commodity_codes():
        prices = repo.get_prices(crop, codes)
        rows.append({"c": crop, "p": {code: codec.price_record(crop, code, p["p"], p["d"], p.get("prev") or None) for code, p in prices.items()}})
    return _json({"r": region, "m": codes, "rows": rows})


# ---------------------------------------------------------------- administrators (FR10)
def _admin() -> dict:
    claims = _claims()
    raw = claims.get("cognito:groups") or ""
    groups = set(raw.replace("[", "").replace("]", "").replace(",", " ").split()) if isinstance(raw, str) else set(raw)
    if "admins" not in groups:
        raise ForbiddenError("administrators only")
    return claims


@app.get("/api/admin/catalog")
def admin_get_catalog():
    _admin()
    catalog.invalidate()
    return _json({
        "doc": repo.get_config("catalog") or {"markets": [], "commodities": [], "units": {}},
        "markets": [m.as_dict() for m in catalog.markets().values()],
        "commodities": catalog.commodities(),
        "units": catalog.unit_aliases(),
        "provinces": catalog.provinces(),
    })


@app.put("/api/admin/catalog")
def admin_put_catalog():
    claims = _admin()
    try:
        doc = catalog.validate_catalog(_body())
    except (ValueError, TypeError) as exc:
        raise BadRequestError(str(exc)) from exc
    repo.put_config("catalog", doc, claims["sub"])
    catalog.invalidate()
    logger.info("admin.catalog_saved", extra={"markets": len(doc["markets"]), "commodities": len(doc["commodities"]), "units": len(doc["units"])})
    return _json({"saved": True, "markets": len(catalog.markets()), "commodities": len(catalog.commodities())})


@app.get("/api/admin/sources")
def admin_get_sources():
    _admin()
    doc = repo.get_config("sources") or {"sources": []}
    return _json({"doc": doc, "builtin": sources.builtin(), "effective": sources.effective(doc)})


@app.put("/api/admin/sources")
def admin_put_sources():
    claims = _admin()
    try:
        doc = sources.validate(_body())
    except (ValueError, TypeError) as exc:
        raise BadRequestError(str(exc)) from exc
    repo.put_config("sources", doc, claims["sub"])
    return _json({"saved": True, "effective": sources.effective(doc)})


def invoke_ingestion(payload: dict) -> str:
    """Start an ingestion run asynchronously (the local emulator replaces this with a direct call)."""
    fn = os.environ.get("INGESTION_FUNCTION")
    if not fn:
        raise BadRequestError("ingestion function not configured")
    boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps(payload).encode())
    return "started"


@app.post("/api/admin/ingest")
def admin_run_ingestion():
    claims = _admin()
    body = _body()
    lookback = _int(body.get("lookback_days", 730), "lookback_days", 1, 3650)
    status = invoke_ingestion({"lookback_days": lookback, "requested_by": claims["sub"]})
    return _json({"status": status}, status=202)


ROLES = ("buyers", "officers", "admins")


def _cognito():
    pool = os.environ.get("USER_POOL_ID")
    if not pool:
        raise BadRequestError("user pool not configured")
    return boto3.client("cognito-idp"), pool


def _cognito_error(exc: ClientError):
    code = exc.response["Error"]["Code"]
    if code == "UserNotFoundException":
        return NotFoundError("user not found")
    if code in ("UsernameExistsException", "InvalidParameterException"):
        return BadRequestError(exc.response["Error"].get("Message", code))
    return exc


def _user_row(u: dict, groups: list[str]) -> dict:
    attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", u.get("UserAttributes", []))}
    return {"username": u["Username"], "email": attrs.get("email", ""), "phone": attrs.get("phone_number", ""),
            "status": u.get("UserStatus", ""), "enabled": u.get("Enabled", True), "groups": sorted(groups)}


@app.get("/api/admin/users")
def admin_list_users():
    _admin()
    idp, pool = _cognito()
    membership: dict[str, list[str]] = {}
    for g in ROLES:
        for u in idp.list_users_in_group(UserPoolId=pool, GroupName=g, Limit=60).get("Users", []):
            membership.setdefault(u["Username"], []).append(g)
    users = idp.list_users(UserPoolId=pool, Limit=60).get("Users", [])
    return _json([_user_row(u, membership.get(u["Username"], [])) for u in users])


@app.post("/api/admin/users")
def admin_invite_user():
    _admin()
    body = _body()
    email = str(body.get("email", "")).strip().lower()
    group = body.get("group")
    if "@" not in email or group not in ROLES:
        raise BadRequestError("email and a valid group (buyers, officers, admins) are required")
    phone = privacy.normalise_phone(body.get("phone", ""))
    idp, pool = _cognito()
    try:
        resp = idp.admin_create_user(UserPoolId=pool, Username=email, DesiredDeliveryMediums=["EMAIL"],
                                     UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}, {"Name": "phone_number", "Value": phone}])
        idp.admin_add_user_to_group(UserPoolId=pool, Username=resp["User"]["Username"], GroupName=group)
    except ClientError as exc:
        raise _cognito_error(exc) from exc
    return _json(_user_row(resp["User"], [group]), status=201)


@app.post("/api/admin/users/<username>/roles")
def admin_change_role(username: str):
    claims = _admin()
    username = urllib.parse.unquote(username)
    body = _body()
    group, action = body.get("group"), body.get("action")
    if group not in ROLES or action not in ("add", "remove"):
        raise BadRequestError("group must be buyers/officers/admins and action add/remove")
    if action == "remove" and group == "admins" and username == claims.get("cognito:username", claims.get("sub")):
        raise BadRequestError("you cannot remove your own admin role")
    idp, pool = _cognito()
    try:
        if action == "add":
            idp.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=group)
        else:
            idp.admin_remove_user_from_group(UserPoolId=pool, Username=username, GroupName=group)
        groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool, Username=username).get("Groups", [])]
    except ClientError as exc:
        raise _cognito_error(exc) from exc
    return _json({"username": username, "groups": sorted(groups)})


@logger.inject_lambda_context(correlation_id_path="requestContext.requestId", clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    obs.maybe_inject_fault()
    _idem_config.register_lambda_context(context)
    return app.resolve(event, context)
