"""Service 4 - Matching.

Consumes EventBridge domain events (ListingCreated, DemandPosted) delivered
through the SQS queue `match-requests`, finds counterparties in the same and
neighbouring geohash cells, filters by great-circle distance, ranks them and
notifies both sides by SMS.

Events carry only keys (no phone numbers); this service re-reads the item.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord

from agricommon import catalog, codec, config, geo, i18n, obs, repo, sms

logger, tracer, metrics = obs.tools("matching")
processor = BatchProcessor(event_type=EventType.SQS)

MAX_NOTIFY = 3


def _item(pk: str, sk: str) -> dict | None:
    item = boto3.resource("dynamodb").Table(config.TABLE_NAME).get_item(Key={"PK": pk, "SK": sk}).get("Item")
    return repo._clean(item)  # noqa: SLF001 (shared decimal cleaner)


def _crop_name(code: str) -> str:
    return (catalog.commodity(code) or {"name": code})["name"]


def _market_name(code: str) -> str:
    m = catalog.markets().get(code)
    return m.name if m else code


def rank_supply_for_demand(demand: dict, supply: list[dict]) -> list[tuple[dict, float]]:
    lat, lon, radius = float(demand["lat"]), float(demand["lon"]), float(demand["r"])
    scored = [(s, geo.haversine_km(lat, lon, float(s["lat"]), float(s["lon"]))) for s in supply]
    return sorted([t for t in scored if t[1] <= radius], key=lambda t: (t[1], -t[0]["q"]))


def rank_demand_for_supply(listing: dict, demand: list[dict]) -> list[tuple[dict, float]]:
    lat, lon = float(listing["lat"]), float(listing["lon"])
    scored = [(d, geo.haversine_km(lat, lon, float(d["lat"]), float(d["lon"]))) for d in demand]
    return sorted([t for t in scored if t[1] <= float(t[0]["r"])], key=lambda t: (-t[0]["pmax"], t[1]))


def on_demand(demand: dict) -> int:
    supply = repo.query_cells("SUPPLY", geo.neighbours(geo.cell(float(demand["lat"]), float(demand["lon"]))), demand["crop"])
    matches = rank_supply_for_demand(demand, supply)
    if not matches:
        return 0
    crop = _crop_name(demand["crop"])
    nearest, km = matches[0]
    sms.send(demand["phone"], i18n.t("sms_match_buyer", n=len(matches), crop=crop, qty=nearest["q"], market=_market_name(nearest["market"]), km=round(km), phone=nearest["phone"]))
    for listing, _ in matches[:MAX_NOTIFY]:
        farmer = repo.get_farmer(listing["pid"]) or {}
        sms.send(listing["phone"], i18n.t("sms_match_farmer", farmer.get("lang", "en"), qty=demand["q"], crop=crop, market=_market_name(listing["market"]), price=codec.kwacha_per_bag(demand["pmax"]), phone=demand["phone"]))
    return len(matches)


def on_listing(listing: dict) -> int:
    demand = repo.query_cells("DEMAND", geo.neighbours(geo.cell(float(listing["lat"]), float(listing["lon"]))), listing["crop"])
    matches = rank_demand_for_supply(listing, demand)
    if not matches:
        return 0
    crop = _crop_name(listing["crop"])
    farmer = repo.get_farmer(listing["pid"]) or {}
    best, _ = matches[0]
    sms.send(listing["phone"], i18n.t("sms_match_farmer", farmer.get("lang", "en"), qty=best["q"], crop=crop, market=_market_name(listing["market"]), price=codec.kwacha_per_bag(best["pmax"]), phone=best["phone"]))
    for d, km in matches[:MAX_NOTIFY]:
        sms.send(d["phone"], i18n.t("sms_match_buyer", n=1, crop=crop, qty=listing["q"], market=_market_name(listing["market"]), km=round(km), phone=listing["phone"]))
    return len(matches)


@tracer.capture_method
def record_handler(record: SQSRecord) -> None:
    cid = obs.correlation_from_sqs_record(record.raw_event)
    logger.set_correlation_id(cid)
    envelope = json.loads(record.body)
    kind, detail = envelope.get("detail-type"), envelope.get("detail") or {}
    item = _item(detail["pk"], detail["sk"])
    if not item:
        logger.info("match.item_gone", extra={"kind": kind})
        return
    found = on_demand(item) if kind == "DemandPosted" else on_listing(item) if kind == "ListingCreated" else 0
    lag = datetime.now(UTC).timestamp() - item.get("ts", 0)
    logger.info("match.done", extra={"kind": kind, "matches": found, "lag_s": round(lag, 1)})
    metrics.add_metric(name="MatchesFound", unit=MetricUnit.Count, value=found)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    return process_partial_response(event=event, record_handler=record_handler, processor=processor, context=context)
