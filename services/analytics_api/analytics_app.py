"""Service 6 - Analytics API.

Nightly (EventBridge Scheduler): one Athena aggregation over the partitioned
Parquet table -> monthly series per commodity and province (plus national
'ZM') -> seasonal index and a 3-month forecast -> cached in DynamoDB.
Users never trigger an Athena scan, which bounds both latency and cost.

  GET /analytics/series?c=MZE&r=CB   compact series, seasonal index, forecast (ETag)
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import date, timedelta

import boto3
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError
from aws_lambda_powertools.metrics import MetricUnit

from agricommon import catalog, codec, obs, repo, weather

logger, tracer, metrics = obs.tools("analytics-api")
app = APIGatewayRestResolver()

NATIONAL = "ZM"
SEASON = 12


# ---------------------------------------------------------------- pure analytics (unit-tested)
def month_add(yyyymm: str, k: int) -> str:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    idx = y * 12 + (m - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def fill_gaps(series: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Forward-fill missing months so seasonal maths sees a regular series."""
    if not series:
        return []
    series = sorted(series)
    out, cur, i = [], series[0][0], 0
    last = series[0][1]
    while cur <= series[-1][0]:
        if i < len(series) and series[i][0] == cur:
            last = series[i][1]
            i += 1
        out.append((cur, last))
        cur = month_add(cur, 1)
    return out


def seasonal_index(series: list[tuple[str, int]]) -> list[int]:
    """Average price in each calendar month as % of the overall mean (100 = average). 0 = no data."""
    if not series:
        return [0] * 12
    overall = sum(v for _, v in series) / len(series)
    by_month: dict[int, list[int]] = defaultdict(list)
    for ym, v in series:
        by_month[int(ym[5:7])].append(v)
    return [round(sum(by_month[m]) / len(by_month[m]) * 100 / overall) if by_month[m] else 0 for m in range(1, 13)]


def holt_winters(values: list[float], horizon: int = 3, season: int = SEASON, alpha: float = 0.3, beta: float = 0.05, gamma: float = 0.2) -> list[float]:
    """Additive Holt-Winters. Falls back to seasonal-naive or last value on short series."""
    n = len(values)
    if n == 0:
        return []
    if n < season:
        return [values[-1]] * horizon
    if n < 2 * season:  # seasonal naive: same month last year
        return [values[n - season + (h % season)] for h in range(horizon)]
    level = sum(values[:season]) / season
    trend = (sum(values[season:2 * season]) - sum(values[:season])) / season ** 2
    seasonals = [values[i] - level for i in range(season)]
    for t in range(n):
        s = seasonals[t % season]
        prev_level = level
        level = alpha * (values[t] - s) + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        seasonals[t % season] = gamma * (values[t] - level) + (1 - gamma) * s
    return [level + (h + 1) * trend + seasonals[(n + h) % season] for h in range(horizon)]


def weekly_forecast(last_value: float, next_month: float, start: date, weeks: int = 4) -> list[tuple[str, float]]:
    """FR12: next 4 weeks, interpolated from the latest monthly value towards next month's forecast."""
    return [((start + timedelta(days=7 * k)).isoformat(), last_value + (next_month - last_value) * min(1.0, 7 * k / 30)) for k in range(1, weeks + 1)]


def build_payload(series: list[tuple[str, int, int, int]], weather_ratio: float | None = None, today: date | None = None) -> dict:
    """series rows: (yyyy-mm, avg, min, max) in ngwee/kg.

    f  = 3 monthly forecasts, w = 4 weekly forecasts (FR12), both multiplied by the weather
    adjustment for the forecast month when a rainfall signal is available (wx).
    """
    today = today or date.today()
    series = sorted(series)
    filled = fill_gaps([(m, a) for m, a, _, _ in series])
    fc = holt_winters([float(v) for _, v in filled]) if filled else []
    last = filled[-1][0] if filled else None
    adj = weather.price_adjustment(weather_ratio, today.month)
    monthly = [[month_add(last, i + 1).replace("-", ""), max(0, round(v * adj))] for i, v in enumerate(fc)] if last else []
    weekly = [[d, max(0, round(v))] for d, v in weekly_forecast(float(filled[-1][1]), monthly[0][1], today)] if monthly else []
    return {
        "s": [[m.replace("-", ""), a, lo, hi] for m, a, lo, hi in series],
        "si": seasonal_index([(m, a) for m, a, _, _ in series]),
        "f": monthly,
        "w": weekly,
        "wx": {"ratio": round(weather_ratio, 2) if weather_ratio is not None else None, "adj": round(adj, 3)},
    }


def group_rows(rows: list[dict]) -> dict[tuple[str, str], list[tuple[str, int, int, int]]]:
    """Group Athena rows into series per (crop, province) and a count-weighted national series."""
    out: dict[tuple[str, str], list[tuple[str, int, int, int]]] = defaultdict(list)
    nat: dict[tuple[str, str], list[tuple[int, int, int, int]]] = defaultdict(list)
    for r in rows:
        crop, prov, month = r["commodity"], r["province"], r["month"]
        a, lo, hi, n = int(r["avgp"]), int(r["minp"]), int(r["maxp"]), int(r["n"])
        out[(crop, prov)].append((month, a, lo, hi))
        nat[(crop, month)].append((a, lo, hi, n))
    for (crop, month), parts in nat.items():
        total = sum(p[3] for p in parts)
        avg = round(sum(p[0] * p[3] for p in parts) / total)
        out[(crop, NATIONAL)].append((month, avg, min(p[1] for p in parts), max(p[2] for p in parts)))
    return out


def _decimal_safe(payload: dict) -> dict:
    """DynamoDB rejects floats: store the weather ratio and factor as strings."""
    wx = payload.get("wx") or {}
    payload["wx"] = {k: (str(v) if isinstance(v, float) else v) for k, v in wx.items()}
    return payload


# ---------------------------------------------------------------- Athena refresh
AGG_SQL = """
SELECT commodity, province, month,
       CAST(round(avg(price_ngwee_kg)) AS integer) AS avgp,
       min(price_ngwee_kg) AS minp, max(price_ngwee_kg) AS maxp, count(*) AS n
FROM (SELECT DISTINCT obs_date, commodity, market, price_ngwee_kg, province, month FROM "{db}"."curated_prices")
GROUP BY commodity, province, month
ORDER BY commodity, province, month
"""


def run_athena(sql: str, timeout_s: int = 240) -> list[dict]:
    athena = boto3.client("athena")
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=os.environ["ATHENA_WORKGROUP"])["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise TimeoutError("athena query timed out")
        time.sleep(1.5)
    if status["State"] != "SUCCEEDED":
        raise RuntimeError(f"athena query {status['State']}: {status.get('StateChangeReason', '')}")
    rows, header = [], None
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            vals = [c.get("VarCharValue") for c in row["Data"]]
            if header is None:
                header = vals
                continue
            rows.append(dict(zip(header, vals, strict=False)))
    return rows


@tracer.capture_method
def refresh() -> dict:
    rows = run_athena(AGG_SQL.format(db=os.environ["GLUE_DATABASE"]))
    groups = group_rows(rows)
    ratios = {p: weather.rainfall_ratio(p) for p in catalog.province_codes()}  # one call per province, reused for every crop
    known = [r for r in ratios.values() if r is not None]
    ratios[NATIONAL] = sum(known) / len(known) if known else None
    for (crop, prov), series in groups.items():
        repo.put_series(crop, prov, _decimal_safe(build_payload(series, ratios.get(prov))))
    metrics.add_metric(name="SeriesRefreshed", unit=MetricUnit.Count, value=len(groups))
    return {"series": len(groups), "rows": len(rows)}


# ---------------------------------------------------------------- API
@app.get("/analytics/series")
def get_series():
    q = app.current_event.query_string_parameters or {}
    crop = q.get("c", "MZE")
    region = q.get("r", NATIONAL)
    if crop not in catalog.commodity_codes():
        raise BadRequestError("unknown commodity code")
    if region != NATIONAL and region not in catalog.province_codes():
        raise BadRequestError("unknown province code")
    item = repo.get_series(crop, region)
    if not item:
        raise NotFoundError("no series yet")
    body = codec.dumps({"c": crop, "r": region, "u": item.get("updated"), "s": item.get("s", []), "si": item.get("si", []), "f": item.get("f", []), "w": item.get("w", []), "wx": item.get("wx", {})})
    tag = codec.etag(body)
    headers = {"ETag": tag, "Cache-Control": "public, max-age=3600"}
    if (app.current_event.headers.get("If-None-Match") or app.current_event.headers.get("if-none-match")) == tag:
        return Response(status_code=304, body="", headers=headers)
    return Response(status_code=200, content_type=content_types.APPLICATION_JSON, body=body, headers=headers)


@logger.inject_lambda_context(correlation_id_path="requestContext.requestId", clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context):
    if isinstance(event, dict) and event.get("action") == "refresh":
        return refresh()
    obs.maybe_inject_fault()
    return app.resolve(event, context)
