"""Service 3 - Price Ingestion (scheduled by EventBridge Scheduler every 6 hours).

Pipeline per source:
  fetch -> parse -> validate & normalise (ngwee/kg, market & province codes)
  -> keep rows newer than the source watermark -> raw zone (gzip JSON lines)
  -> update current prices in DynamoDB -> publish changes to SQS (async path)
  -> Athena INSERT INTO the partitioned Parquet table (serverless ETL; no Glue jobs)

Source types (SOURCES_JSON env var):
  wfp_csv     WFP market-price CSV over HTTPS (Humanitarian Data Exchange format)
  wfp_csv_s3  the same format read from the lake bucket (seed / offline demo data)
  s3_inbox    extension-officer field reports written by the Listings API
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import urllib.request
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta

import boto3
from aws_lambda_powertools.metrics import MetricUnit

from agricommon import bus, catalog, obs, repo

logger, tracer, metrics = obs.tools("ingestion")

MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
DEFAULT_LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "730"))


# ---------------------------------------------------------------- parsing & normalisation
def normalise_wfp_row(row: dict, source: str) -> dict | None:
    """Map one WFP/HDX CSV row to the canonical record, or None if unusable."""
    d = (row.get("date") or "").strip()
    if not d or d.startswith("#"):
        return None  # HXL tag row or blank
    try:
        obs_date = date.fromisoformat(d[:10]).isoformat()
    except ValueError:
        return None
    crop = catalog.match_commodity(row.get("commodity", ""))
    if not crop:
        return None
    try:
        price = float(row.get("price") or 0)
    except ValueError:
        return None
    ngwee = catalog.to_ngwee_per_kg(price, row.get("unit", ""), row.get("currency", "ZMW"))
    if not ngwee or not 10 <= ngwee <= 1_000_000:  # sanity bounds: K0.10 - K10,000 per kg
        return None
    market = catalog.match_market(row.get("market", ""))
    if market:
        code, prov, lat, lon = market.code, market.province, market.lat, market.lon
    else:
        prov = catalog.match_province(row.get("admin1", ""))
        try:
            lat, lon = float(row.get("latitude") or ""), float(row.get("longitude") or "")
        except ValueError:
            return None
        if not prov:
            return None
        code = catalog.market_code_for(row.get("market", ""))
    return {
        "obs_date": obs_date, "commodity": crop, "market": code, "province": prov,
        "price_ngwee_kg": ngwee, "source": source, "lat": lat, "lon": lon,
        "ptype": (row.get("pricetype") or "").strip() or None,
    }


def parse_wfp_csv(text: str, source: str) -> Iterator[dict]:
    for row in csv.DictReader(io.StringIO(text)):
        rec = normalise_wfp_row({(k or "").strip().lower(): (v or "") for k, v in row.items()}, source)
        if rec:
            yield rec


def latest_per_key(rows: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """Latest observation per (commodity, market); wholesale wins ties (farmers sell wholesale).

    Each result also carries `prev_price`: the latest earlier observation of the same price type
    in the batch, so the very first load already shows a meaningful change percentage.
    """
    best: dict[tuple[str, str], dict] = {}
    history: dict[tuple[str, str, str | None], list[tuple[str, int]]] = {}
    for r in rows:
        k = (r["commodity"], r["market"])
        history.setdefault((*k, r.get("ptype")), []).append((r["obs_date"], r["price_ngwee_kg"]))
        cur = best.get(k)
        if cur is None or r["obs_date"] > cur["obs_date"] or (r["obs_date"] == cur["obs_date"] and r.get("ptype") == "Wholesale"):
            best[k] = r
    out = {}
    for k, r in best.items():
        earlier = sorted(h for h in history[(*k, r.get("ptype"))] if h[0] < r["obs_date"])
        out[k] = {**r, "prev_price": earlier[-1][1] if earlier else None}
    return out


# ---------------------------------------------------------------- sources
def _http_text(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError("only https sources are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "AgriPriceZambia/1.0 (+student project)"})
    with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310 (https enforced above)
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("source file too large")
    return data.decode("utf-8-sig")


def _s3_text(bucket: str, key: str) -> str:
    return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8-sig")


def read_inbox(bucket: str, prefix: str, source: str) -> tuple[list[dict], list[str]]:
    s3 = boto3.client("s3")
    rows, keys = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            try:
                rec = json.loads(_s3_text(bucket, obj["Key"]))
                m = catalog.markets()[rec["m"]]
                rows.append({
                    "obs_date": date.fromisoformat(rec["d"]).isoformat(), "commodity": rec["c"], "market": m.code,
                    "province": m.province, "price_ngwee_kg": int(rec["p"]), "source": source, "lat": m.lat, "lon": m.lon, "ptype": "Field",
                })
            except (KeyError, ValueError, TypeError):
                logger.warning("inbox.bad_record", extra={"key": obj["Key"]})
            keys.append(obj["Key"])
    return rows, keys


def archive_inbox(bucket: str, keys: list[str]) -> None:
    s3 = boto3.client("s3")
    for k in keys:
        s3.copy_object(Bucket=bucket, Key="processed/" + k, CopySource={"Bucket": bucket, "Key": k})
        s3.delete_object(Bucket=bucket, Key=k)


# ---------------------------------------------------------------- sinks
def write_raw(bucket: str, rows: list[dict], ingest_day: str, source: str, run_id: str) -> str:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for r in rows:
            gz.write((json.dumps({**r, "run_id": run_id}, separators=(",", ":")) + "\n").encode())
    key = f"raw/ingest_date={ingest_day}/{source}-{run_id}.json.gz"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=buf.getvalue(), ContentType="application/json", ContentEncoding="gzip")
    return key


def update_current(latest: dict[tuple[str, str], dict]) -> list[dict]:
    changes = []
    for (crop, market), r in latest.items():
        changed, prev = repo.upsert_price(crop, market, r["price_ngwee_kg"], r["obs_date"], r["province"], r["source"], r.get("prev_price"))
        if changed:
            changes.append({"c": crop, "m": market, "p": r["price_ngwee_kg"], "prev": prev or 0, "d": r["obs_date"]})
    return changes


def start_curation(ingest_day: str, run_id: str) -> str:
    db, wg = os.environ["GLUE_DATABASE"], os.environ["ATHENA_WORKGROUP"]
    sql = (
        f'INSERT INTO "{db}"."curated_prices" '
        "SELECT DISTINCT CAST(obs_date AS date), commodity, market, price_ngwee_kg, source, province, substr(obs_date, 1, 7) "
        f'FROM "{db}"."raw_prices" WHERE ingest_date = \'{ingest_day}\' AND run_id = \'{run_id}\''
    )
    return boto3.client("athena").start_query_execution(QueryString=sql, WorkGroup=wg)["QueryExecutionId"]


# ---------------------------------------------------------------- orchestration
def run_source(src: dict, bucket: str, ingest_day: str, run_id: str, lookback_days: int) -> dict:
    sid, kind = src["id"], src["type"]
    inbox_keys: list[str] = []
    if kind == "wfp_csv":
        rows = list(parse_wfp_csv(_http_text(src["url"]), sid))
    elif kind == "wfp_csv_s3":
        rows = list(parse_wfp_csv(_s3_text(bucket, src["key"]), sid))
    elif kind == "s3_inbox":
        rows, inbox_keys = read_inbox(bucket, src.get("prefix", "inbox/field/"), sid)
    else:
        raise ValueError(f"unknown source type {kind}")

    if kind != "s3_inbox":
        floor = (date.today() - timedelta(days=lookback_days)).isoformat()
        mark = max(repo.get_watermark(sid), floor)
        rows = [r for r in rows if r["obs_date"] > mark]
    result = {"source": sid, "rows": len(rows), "changes": 0}
    if rows:
        write_raw(bucket, rows, ingest_day, sid, run_id)
        changes = update_current(latest_per_key(rows))
        result["changes"] = len(changes)
        if changes:
            bus.send_messages(os.environ["PRICE_QUEUE_URL"], changes, run_id)
        if kind != "s3_inbox":
            repo.set_watermark(sid, max(r["obs_date"] for r in rows))
    if inbox_keys:
        archive_inbox(bucket, inbox_keys)
    return result


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    run_id = uuid.uuid4().hex[:12]
    logger.set_correlation_id(run_id)
    bucket = os.environ["LAKE_BUCKET"]
    ingest_day = datetime.now(UTC).date().isoformat()
    sources = json.loads(os.environ.get("SOURCES_JSON", "[]"))
    lookback = int((event or {}).get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    results, failures, total = [], 0, 0
    for src in sources:
        try:
            res = run_source(src, bucket, ingest_day, run_id, lookback)
            results.append(res)
            total += res["rows"]
            logger.info("ingest.source_done", extra=res)
        except Exception:
            failures += 1
            logger.exception("ingest.source_failed", extra={"source": src.get("id")})
    metrics.add_metric(name="IngestedRecords", unit=MetricUnit.Count, value=total)
    metrics.add_metric(name="IngestionFailures", unit=MetricUnit.Count, value=failures)
    if total:
        results.append({"athena_query": start_curation(ingest_day, run_id)})
    if failures and failures == len(sources):
        raise RuntimeError("all ingestion sources failed")  # -> async retry, then DLQ + alarm
    return {"run_id": run_id, "results": results}
