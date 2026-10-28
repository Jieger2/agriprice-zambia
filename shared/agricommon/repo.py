"""DynamoDB single-table access. Key design (see docs/architecture.md):

  FARMER#<pid>               PROFILE                         farmer profile (pid = keyed phone hash); GSI1 OFFICER#<sub> when an officer registered them
  PRICE#<crop>#<market>      CURRENT                         latest normalised price
  SUPPLY#<cell>              <crop>#<ts>#<pid>               farmer listing (TTL 30 d); GSI1 FARMERITEMS#<pid>
  DEMAND#<cell>              <crop>#<ts>#<demandId>          buyer demand (TTL 30 d), GSI1 OWNER#<sub>
  ALERT#<crop>#<market>      <pid>#<dir>#<threshold>         price alert subscription; GSI1 FARMERITEMS#<pid>
  STATS#<day>|STATS#ALL      COUNTS                          platform counters for the admin overview
  ALERTLOG#<pid>#<crop>      <yyyy-mm-dd>                    one alert SMS per crop per day (TTL 2 d)
  SESSION#<sessionId>        STATE                           USSD session state (TTL 180 s)
  RESUME#<pid>               LAST                            last USSD path for resume (TTL 180 s)
  SERIES#<crop>#<province>   ALL                             cached monthly series (analytics)
  SOURCE#<sourceId>          WATERMARK                       ingestion high-water mark
  CONFIG#<name>              CURRENT                         admin-managed catalog / price sources (JSON document)
  MATCH#<demandId>           <supply SK>                     supply matched to a buyer demand (TTL 30 d)
  SMSOUT#<phone>             <ts>#<n>                        mock SMS outbox (SMS_MODE=outbox, TTL 7 d)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config

SESSION_TTL = 180
LISTING_TTL = 30 * 86400


def _table():
    return boto3.resource("dynamodb").Table(config.TABLE_NAME)


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _clean(item: dict | None) -> dict | None:
    """Convert DynamoDB Decimals (recursively) to int/float."""
    return None if item is None else _plain(item)


def now() -> int:
    return int(time.time())


# ---------- farmers ----------
def get_farmer(pid: str) -> dict | None:
    return _clean(_table().get_item(Key={"PK": f"FARMER#{pid}", "SK": "PROFILE"}).get("Item"))


def put_farmer(pid: str, phone: str, market: str, lang: str = "en", name: str = "", registered_by: str = "ussd") -> dict:
    """Create or update a farmer. An officer's link to a farmer survives the farmer re-registering by USSD."""
    existing = get_farmer(pid) or {}
    by = registered_by if registered_by != "ussd" or not existing.get("by") else existing["by"]
    item = {"PK": f"FARMER#{pid}", "SK": "PROFILE", "phone": phone, "market": market, "lang": lang, "name": name or existing.get("name", ""),
            "by": by, "created": existing.get("created", now()), "pid": pid}
    if by != "ussd":
        item.update({"GSI1PK": f"OFFICER#{by}", "GSI1SK": f"FARMER#{item['created']}"})
    _table().put_item(Item=item)
    return item


def farmers_by_officer(officer: str, limit: int = 100) -> list[dict]:
    resp = _table().query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"OFFICER#{officer}"), ScanIndexForward=False, Limit=limit)
    return [_clean(i) for i in resp.get("Items", [])]


def farmer_items(pid: str) -> dict[str, list[dict]]:
    """A farmer's active listings and alert subscriptions (GSI1 FARMERITEMS#<pid>)."""
    resp = _table().query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"FARMERITEMS#{pid}"), Limit=100)
    items = [_clean(i) for i in resp.get("Items", []) if i.get("ttl", now() + 1) > now()]
    return {"listings": [i for i in items if i["PK"].startswith("SUPPLY#")], "alerts": [i for i in items if i["PK"].startswith("ALERT#")]}


def delete_item(pk: str, sk: str) -> None:
    _table().delete_item(Key={"PK": pk, "SK": sk})


def forget_farmer(pid: str) -> int:
    """Right to erasure (SMS STOP): profile, listings and alerts. Returns the number of items removed."""
    items = farmer_items(pid)
    for i in items["listings"] + items["alerts"]:
        delete_item(i["PK"], i["SK"])
    delete_farmer(pid)
    return len(items["listings"]) + len(items["alerts"]) + 1


def set_farmer_lang(pid: str, lang: str) -> None:
    _table().update_item(Key={"PK": f"FARMER#{pid}", "SK": "PROFILE"}, UpdateExpression="SET lang = :l", ExpressionAttributeValues={":l": lang})


def delete_farmer(pid: str) -> None:
    _table().delete_item(Key={"PK": f"FARMER#{pid}", "SK": "PROFILE"})


# ---------- prices ----------
def get_prices(crop: str, market_codes: Iterable[str]) -> dict[str, dict]:
    keys = [{"PK": f"PRICE#{crop}#{m}", "SK": "CURRENT"} for m in market_codes]
    if not keys:
        return {}
    resp = boto3.resource("dynamodb").batch_get_item(RequestItems={config.TABLE_NAME: {"Keys": keys}})
    items = resp.get("Responses", {}).get(config.TABLE_NAME, [])
    return {i["PK"].split("#")[2]: _clean(i) for i in items}


def get_price(crop: str, market: str) -> dict | None:
    return _clean(_table().get_item(Key={"PK": f"PRICE#{crop}#{market}", "SK": "CURRENT"}).get("Item"))


def upsert_price(crop: str, market: str, ngwee_per_kg: int, obs_date: str, province: str, source: str, fallback_prev: int | None = None) -> tuple[bool, int | None]:
    """Write the current price only if the observation is newer. Returns (changed, previous_price)."""
    try:
        resp = _table().update_item(
            Key={"PK": f"PRICE#{crop}#{market}", "SK": "CURRENT"},
            UpdateExpression="SET p = :p, d = :d, prov = :prov, src = :src, prev = if_not_exists(p, :none)",
            ConditionExpression="attribute_not_exists(d) OR d < :d OR (d = :d AND p <> :p)",
            ExpressionAttributeValues={":p": ngwee_per_kg, ":d": obs_date, ":prov": province, ":src": source, ":none": fallback_prev or 0},
            ReturnValues="UPDATED_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False, None
        raise
    # Right-hand operands are evaluated against the pre-update item, so prev already holds the old price.
    old = _clean(resp.get("Attributes") or {})
    return True, old.get("p") or fallback_prev


# ---------- supply / demand ----------
def put_supply(cell: str, crop: str, pid: str, phone: str, qty_kg: int, market: str, lat: float, lon: float,
               unit: str = "kg", available_from: str | None = None) -> dict:
    """A farmer listing (FR2). q is always kilograms; `unit` records how the farmer counted it (kg or 50 kg bags)."""
    ts = now()
    item = {
        "PK": f"SUPPLY#{cell}", "SK": f"{crop}#{ts}#{pid}", "crop": crop, "pid": pid, "phone": phone,
        "q": qty_kg, "unit": unit, "from": available_from or time.strftime("%Y-%m-%d", time.gmtime(ts)),
        "market": market, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)), "ts": ts, "ttl": ts + LISTING_TTL,
        "GSI1PK": f"FARMERITEMS#{pid}", "GSI1SK": f"SUPPLY#{ts}",
    }
    _table().put_item(Item=item)
    return _clean(item)


def put_demand(cell: str, crop: str, demand_id: str, owner: str, phone: str, qty_kg: int, max_ngwee_per_kg: int, lat: float, lon: float, radius_km: int) -> dict:
    ts = now()
    item = {
        "PK": f"DEMAND#{cell}", "SK": f"{crop}#{ts}#{demand_id}", "crop": crop, "id": demand_id, "owner": owner, "phone": phone,
        "q": qty_kg, "pmax": max_ngwee_per_kg, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)), "r": radius_km,
        "ts": ts, "ttl": ts + LISTING_TTL, "GSI1PK": f"OWNER#{owner}", "GSI1SK": f"DEMAND#{ts}",
    }
    _table().put_item(Item=item)
    return _clean(item)


def query_cells(prefix: str, cells: Iterable[str], crop: str) -> list[dict]:
    out: list[dict] = []
    t = _table()
    for c in cells:
        resp = t.query(KeyConditionExpression=Key("PK").eq(f"{prefix}#{c}") & Key("SK").begins_with(f"{crop}#"), Limit=50)
        out.extend(_clean(i) for i in resp.get("Items", []) if i.get("ttl", now() + 1) > now())
    return out


def query_cells_all(prefix: str, cells: Iterable[str]) -> list[dict]:
    """Every crop in the given cells (officer board: open supply per province)."""
    out: list[dict] = []
    t = _table()
    for c in cells:
        resp = t.query(KeyConditionExpression=Key("PK").eq(f"{prefix}#{c}"), Limit=500)
        out.extend(_clean(i) for i in resp.get("Items", []) if i.get("ttl", now() + 1) > now())
    return out


def demands_by_owner(owner: str, limit: int = 20) -> list[dict]:
    resp = _table().query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"OWNER#{owner}"), ScanIndexForward=False, Limit=limit)
    return [_clean(i) for i in resp.get("Items", [])]


# ---------- alerts ----------
def put_alert(crop: str, market: str, pid: str, phone: str, threshold_ngwee_per_kg: int, direction: str = "up", lang: str = "en") -> dict:
    item = {"PK": f"ALERT#{crop}#{market}", "SK": f"{pid}#{direction}#{threshold_ngwee_per_kg}", "pid": pid, "phone": phone, "thr": threshold_ngwee_per_kg,
            "dir": direction, "lang": lang, "crop": crop, "market": market, "GSI1PK": f"FARMERITEMS#{pid}", "GSI1SK": f"ALERT#{crop}#{market}#{direction}#{threshold_ngwee_per_kg}"}
    _table().put_item(Item=item)
    return item


def alerts_for(crop: str, market: str) -> list[dict]:
    t, out, kwargs = _table(), [], {"KeyConditionExpression": Key("PK").eq(f"ALERT#{crop}#{market}")}
    while True:
        resp = t.query(**kwargs)
        out.extend(_clean(i) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def claim_alert_slot(pid: str, crop: str, day: str) -> bool:
    """Idempotency + frequency cap: at most one alert SMS per farmer, crop and day."""
    try:
        _table().put_item(
            Item={"PK": f"ALERTLOG#{pid}#{crop}", "SK": day, "ttl": now() + 2 * 86400},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_alert_slot(pid: str, crop: str, day: str) -> None:
    _table().delete_item(Key={"PK": f"ALERTLOG#{pid}#{crop}", "SK": day})


# ---------- USSD sessions ----------
def get_session(session_id: str) -> dict | None:
    item = _clean(_table().get_item(Key={"PK": f"SESSION#{session_id}", "SK": "STATE"}).get("Item"))
    if item and item.get("ttl", 0) < now():
        return None
    return item


def put_session(session_id: str, **state: Any) -> None:
    _table().put_item(Item={"PK": f"SESSION#{session_id}", "SK": "STATE", "ttl": now() + SESSION_TTL, **state})


def get_resume(pid: str) -> str | None:
    item = _clean(_table().get_item(Key={"PK": f"RESUME#{pid}", "SK": "LAST"}).get("Item"))
    if not item or item.get("ttl", 0) < now():
        return None
    return item.get("path")


def put_resume(pid: str, path: str) -> None:
    _table().put_item(Item={"PK": f"RESUME#{pid}", "SK": "LAST", "path": path, "ttl": now() + SESSION_TTL})


def clear_resume(pid: str) -> None:
    _table().delete_item(Key={"PK": f"RESUME#{pid}", "SK": "LAST"})


# ---------- analytics cache & ingestion watermark ----------
def put_series(crop: str, province: str, payload: dict) -> None:
    _table().put_item(Item={"PK": f"SERIES#{crop}#{province}", "SK": "ALL", "updated": now(), **payload})


def get_series(crop: str, province: str) -> dict | None:
    return _clean(_table().get_item(Key={"PK": f"SERIES#{crop}#{province}", "SK": "ALL"}).get("Item"))


def get_watermark(source_id: str) -> str:
    item = _table().get_item(Key={"PK": f"SOURCE#{source_id}", "SK": "WATERMARK"}).get("Item")
    return item["d"] if item else "1970-01-01"


def set_watermark(source_id: str, day: str) -> None:
    _table().put_item(Item={"PK": f"SOURCE#{source_id}", "SK": "WATERMARK", "d": day, "updated": now()})


# ---------- admin configuration (FR10) ----------
def get_config(name: str) -> dict | None:
    item = _table().get_item(Key={"PK": f"CONFIG#{name}", "SK": "CURRENT"}).get("Item")
    return json.loads(item["doc"]) if item else None


def put_config(name: str, doc: dict, by: str) -> None:
    """Stored as a JSON string: keeps floats (coordinates, kg factors) exact and the item schema-free."""
    _table().put_item(Item={"PK": f"CONFIG#{name}", "SK": "CURRENT", "doc": json.dumps(doc, separators=(",", ":")), "by": by, "updated": now()})


# ---------- matches (FR5) ----------
def put_match(demand: dict, listing: dict, km: float) -> bool:
    """Record a demand/listing pair once. Returns False if it already existed.

    Both sides of a pair can raise an event (ListingCreated and DemandPosted arriving close together,
    or an SQS redelivery); the conditional write makes notification exactly-once per pair.
    """
    try:
        _table().put_item(
            Item={
                "PK": f"MATCH#{demand['id']}", "SK": listing["SK"], "owner": demand["owner"], "crop": listing["crop"],
                "q": listing["q"], "market": listing["market"], "km": Decimal(str(round(km, 1))), "phone": listing["phone"],
                "from": listing.get("from", ""), "ts": now(), "ttl": now() + LISTING_TTL,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def matches_for(demand_id: str) -> list[dict]:
    resp = _table().query(KeyConditionExpression=Key("PK").eq(f"MATCH#{demand_id}"), Limit=100)
    return [_clean(i) for i in resp.get("Items", [])]


# ---------- mock SMS outbox (SMS_MODE=outbox) ----------
def put_outbox(phone: str, body: str) -> None:
    ts = time.time()
    _table().put_item(Item={"PK": f"SMSOUT#{phone}", "SK": f"{ts:.6f}", "body": body, "ts": int(ts), "ttl": int(ts) + 7 * 86400})


def outbox(phone: str, limit: int = 30) -> list[dict]:
    resp = _table().query(KeyConditionExpression=Key("PK").eq(f"SMSOUT#{phone}"), ScanIndexForward=False, Limit=limit)
    return [{"body": i["body"], "ts": int(i["ts"])} for i in resp.get("Items", [])]


# ---------- platform counters (admin overview) ----------
def bump(**counts: int) -> None:
    """Atomically add to today's and all-time counters. Never fails the caller."""
    if not counts:
        return
    expr = "ADD " + ", ".join(f"#{k} :{k}" for k in counts)
    names = {f"#{k}": k for k in counts}
    values = {f":{k}": v for k, v in counts.items()}
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        for pk in (f"STATS#{day}", "STATS#ALL"):
            _table().update_item(Key={"PK": pk, "SK": "COUNTS"}, UpdateExpression=expr, ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except Exception as exc:  # counters are informational; business writes must not depend on them
        logging.getLogger(__name__).debug("stats bump skipped: %s", exc)


def get_stats(days: int = 7) -> dict:
    keys = [{"PK": "STATS#ALL", "SK": "COUNTS"}] + [{"PK": f"STATS#{time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400 * d))}", "SK": "COUNTS"} for d in range(days)]
    resp = boto3.resource("dynamodb").batch_get_item(RequestItems={config.TABLE_NAME: {"Keys": keys}})
    found = {i["PK"]: {k: v for k, v in _clean(i).items() if k not in ("PK", "SK")} for i in resp.get("Responses", {}).get(config.TABLE_NAME, [])}
    return {"total": found.get("STATS#ALL", {}), "days": [{"day": k["PK"][6:], **found.get(k["PK"], {})} for k in keys[1:]]}
