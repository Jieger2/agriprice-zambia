"""DynamoDB single-table access. Key design (see docs/architecture.md):

  FARMER#<pid>               PROFILE                         farmer profile (pid = keyed phone hash)
  PRICE#<crop>#<market>      CURRENT                         latest normalised price
  SUPPLY#<cell>              <crop>#<ts>#<pid>               farmer listing (TTL 30 d)
  DEMAND#<cell>              <crop>#<ts>#<demandId>          buyer demand (TTL 30 d), GSI1 OWNER#<sub>
  ALERT#<crop>#<market>      <pid>#<dir>#<threshold>         price alert subscription
  ALERTLOG#<pid>#<crop>      <yyyy-mm-dd>                    one alert SMS per crop per day (TTL 2 d)
  SESSION#<sessionId>        STATE                           USSD session state (TTL 180 s)
  RESUME#<pid>               LAST                            last USSD path for resume (TTL 180 s)
  SERIES#<crop>#<province>   ALL                             cached monthly series (analytics)
  SOURCE#<sourceId>          WATERMARK                       ingestion high-water mark
"""

from __future__ import annotations

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
    item = {"PK": f"FARMER#{pid}", "SK": "PROFILE", "phone": phone, "market": market, "lang": lang, "name": name, "by": registered_by, "created": now()}
    _table().put_item(Item=item)
    return item


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
def put_supply(cell: str, crop: str, pid: str, phone: str, qty_kg: int, market: str, lat: float, lon: float) -> dict:
    ts = now()
    item = {
        "PK": f"SUPPLY#{cell}", "SK": f"{crop}#{ts}#{pid}", "crop": crop, "pid": pid, "phone": phone,
        "q": qty_kg, "market": market, "lat": Decimal(str(lat)), "lon": Decimal(str(lon)), "ts": ts, "ttl": ts + LISTING_TTL,
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


def demands_by_owner(owner: str, limit: int = 20) -> list[dict]:
    resp = _table().query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"OWNER#{owner}"), ScanIndexForward=False, Limit=limit)
    return [_clean(i) for i in resp.get("Items", [])]


# ---------- alerts ----------
def put_alert(crop: str, market: str, pid: str, phone: str, threshold_ngwee_per_kg: int, direction: str = "up", lang: str = "en") -> dict:
    item = {"PK": f"ALERT#{crop}#{market}", "SK": f"{pid}#{direction}#{threshold_ngwee_per_kg}", "pid": pid, "phone": phone, "thr": threshold_ngwee_per_kg, "dir": direction, "lang": lang}
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
