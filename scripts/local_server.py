#!/usr/bin/env python3
"""Run the whole platform on your laptop - no AWS account needed.

The real Lambda handlers serve every request. Production services are replaced
by faithful local stand-ins:

  AWS (DynamoDB, S3, SQS, EventBridge, Cognito)  -> moto, in memory
  EventBridge -> SQS -> Matching / SQS -> Alerts -> background pump threads (same handlers, same SQS events)
  Athena INSERT INTO + nightly aggregation        -> in-process curated table + Python aggregation
  Africa's Talking SMS                            -> SMS outbox, shown in the phone simulator
  Africa's Talking USSD                           -> /simulator.html (feature phone) or scripts/ussd_cli.py
  Cognito hosted UI                               -> "Sign in" role picker (buyer / officer / admin)
  Open-Meteo weather                              -> deterministic mock rainfall per province

  pip install -r requirements-dev.txt
  python scripts/local_server.py          # open http://localhost:8080 and http://localhost:8080/simulator.html
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import threading
import time
import urllib.parse
import warnings
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
for svc in (ROOT / "services").iterdir():
    if svc.is_dir():
        sys.path.insert(0, str(svc))

REGION = "eu-north-1"
os.environ.update({
    "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local",
    "APP_ENV": "local", "TABLE_NAME": "agriprice-local", "IDEMPOTENCY_TABLE": "agriprice-local-idempotency",
    "EVENT_BUS_NAME": "agriprice-local", "LAKE_BUCKET": "agriprice-local-lake", "GLUE_DATABASE": "agriprice_local",
    "ATHENA_WORKGROUP": "primary", "SMS_MODE": "outbox", "SECRET_PRIVACY_PEPPER": "local-pepper", "SECRET_AT_CALLBACK_TOKEN": "local",
    "CATALOG_DYNAMIC": "1", "WEATHER_MODE": "mock",
    "POWERTOOLS_TRACE_DISABLED": "1", "POWERTOOLS_METRICS_NAMESPACE": "AgriPriceLocal", "POWERTOOLS_LOG_LEVEL": "WARNING",
    "POWERTOOLS_METRICS_DISABLED": "true",
})
warnings.filterwarnings("ignore")
os.environ.pop("SOURCES_JSON", None)

from moto import mock_aws  # noqa: E402

MOCK = mock_aws()
MOCK.start()

import boto3  # noqa: E402

LOCK = threading.RLock()          # moto is not thread-safe: serialise every handler call
CURATED: dict[tuple, dict] = {}   # emulated Athena "curated_prices" table (deduplicated like SELECT DISTINCT)
QUEUES: dict[str, str] = {}
ROLE: list[str] | None = None
DEMO_USERS = [("admin@agriprice.local", "+260955000001", "admins"), ("officer@agriprice.local", "+260955000002", "officers"),
              ("buyer@agriprice.local", "+260966000100", "buyers")]


# ---------------------------------------------------------------- emulated AWS resources
def setup_aws() -> None:
    ddb = boto3.client("dynamodb")
    ddb.create_table(
        TableName="agriprice-local", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
        GlobalSecondaryIndexes=[{"IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}],
    )
    ddb.create_table(TableName="agriprice-local-idempotency", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}], AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])
    sqs = boto3.client("sqs")
    for name in ("price-normalised", "match-requests"):
        QUEUES[name] = sqs.create_queue(QueueName=name)["QueueUrl"]
    os.environ["PRICE_QUEUE_URL"] = QUEUES["price-normalised"]
    events = boto3.client("events")
    events.create_event_bus(Name="agriprice-local")
    events.put_rule(Name="match", EventBusName="agriprice-local", EventPattern=json.dumps({"source": ["agriprice"], "detail-type": ["ListingCreated", "DemandPosted"]}))
    match_arn = sqs.get_queue_attributes(QueueUrl=QUEUES["match-requests"], AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    events.put_targets(Rule="match", EventBusName="agriprice-local", Targets=[{"Id": "match", "Arn": match_arn}])
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="agriprice-local-lake", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.upload_file(str(ROOT / "data/zambia_sample_prices.csv"), "agriprice-local-lake", "seed/zambia_sample_prices.csv")
    idp = boto3.client("cognito-idp")
    pool = idp.create_user_pool(PoolName="agriprice-local")["UserPool"]["Id"]
    os.environ["USER_POOL_ID"] = pool
    for g in ("buyers", "officers", "admins"):
        idp.create_group(UserPoolId=pool, GroupName=g)
    for email, phone, group in DEMO_USERS:
        idp.admin_create_user(UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": email}, {"Name": "phone_number", "Value": phone}])
        idp.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=group)


# ---------------------------------------------------------------- emulated Athena (curation + nightly aggregation)
def local_curation(ingest_day: str, run_id: str) -> str:
    """Stand-in for the Athena INSERT INTO: copy this run's raw rows into the curated table."""
    s3 = boto3.client("s3")
    listing = s3.list_objects_v2(Bucket="agriprice-local-lake", Prefix=f"raw/ingest_date={ingest_day}/")
    for obj in listing.get("Contents", []):
        if run_id not in obj["Key"]:
            continue
        for line in gzip.decompress(s3.get_object(Bucket="agriprice-local-lake", Key=obj["Key"])["Body"].read()).splitlines():
            r = json.loads(line)
            CURATED[(r["obs_date"], r["commodity"], r["market"], r["price_ngwee_kg"])] = r
    return f"local-{run_id}"


def local_athena(sql: str) -> list[dict]:
    """Stand-in for the nightly Athena aggregation query (same output columns)."""
    agg: dict = defaultdict(list)
    for r in CURATED.values():
        agg[(r["commodity"], r["province"], r["obs_date"][:7])].append(r["price_ngwee_kg"])
    return [{"commodity": c, "province": p, "month": m, "avgp": str(round(sum(v) / len(v))), "minp": str(min(v)), "maxp": str(max(v)), "n": str(len(v))}
            for (c, p, m), v in sorted(agg.items())]


class Ctx:
    function_name, memory_limit_in_mb, aws_request_id = "local", 512, "local"
    invoked_function_arn = f"arn:aws:lambda:{REGION}:000000000000:function:local"

    @staticmethod
    def get_remaining_time_in_millis() -> int:
        return 300_000


def run_pipeline(payload: dict | None = None) -> dict:
    """Ingestion run followed by the analytics refresh (what the schedules do in AWS)."""
    import analytics_app
    import ingestion_app

    with LOCK:
        out = ingestion_app.handler(payload or {"lookback_days": 3650}, Ctx())
        analytics_app.handler({"action": "refresh"}, Ctx())
    return out


# ---------------------------------------------------------------- async pumps (SQS event source mappings)
def _sqs_event(messages: list[dict], queue: str) -> dict:
    return {"Records": [{
        "messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"], "attributes": {}, "md5OfBody": m.get("MD5OfBody", ""),
        "messageAttributes": {k: {"stringValue": v.get("StringValue"), "dataType": v.get("DataType", "String")} for k, v in (m.get("MessageAttributes") or {}).items()},
        "eventSource": "aws:sqs", "eventSourceARN": f"arn:aws:sqs:{REGION}:000000000000:{queue}", "awsRegion": REGION,
    } for m in messages]}


def pump_forever() -> None:
    import alerts_app
    import matching_app

    consumers = {"price-normalised": alerts_app.handler, "match-requests": matching_app.handler}
    sqs = boto3.client("sqs")
    while True:
        for name, handler in consumers.items():
            try:
                with LOCK:
                    msgs = sqs.receive_message(QueueUrl=QUEUES[name], MaxNumberOfMessages=10, MessageAttributeNames=["All"], VisibilityTimeout=30).get("Messages", [])
                    if not msgs:
                        continue  # (a busy queue is drained 10 messages per handler call, like a Lambda batch)
                    try:
                        failed = {f["itemIdentifier"] for f in handler(_sqs_event(msgs, name), Ctx()).get("batchItemFailures", [])}
                    except Exception as exc:  # whole batch failed: leave messages for redelivery
                        print(f"[local] {name} batch failed: {exc}", file=sys.stderr)
                        continue
                    for m in msgs:
                        if m["MessageId"] not in failed:
                            sqs.delete_message(QueueUrl=QUEUES[name], ReceiptHandle=m["ReceiptHandle"])
                    print(f"[local] {name}: processed {len(msgs) - len(failed)} message(s)", file=sys.stderr)
            except Exception as exc:  # keep the pump alive whatever happens
                print(f"[local] pump error: {exc}", file=sys.stderr)
        time.sleep(0.5)


# ---------------------------------------------------------------- HTTP front door (stands in for CloudFront + API Gateway)
def mock_claims(headers: dict) -> dict | None:
    """Decode the unsigned token issued by the web app's local sign-in (never accepted in AWS)."""
    token = headers.get("Authorization") or headers.get("authorization") or ""
    if token.count(".") == 2:
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            groups = claims.get("cognito:groups") or []
            claims["cognito:groups"] = ",".join(groups) if isinstance(groups, list) else groups
            return claims
        except ValueError:
            return None
    if ROLE:
        return {"sub": "local-user", "cognito:groups": ",".join(ROLE), "phone_number": "+260966000100", "email": "local@agriprice.local"}
    return None


def rest_event(method: str, path: str, query: dict, headers: dict, body: str | None) -> dict:
    claims = mock_claims(headers)
    return {
        "resource": path, "path": path, "httpMethod": method, "headers": headers, "multiValueHeaders": {},
        "queryStringParameters": query or None, "multiValueQueryStringParameters": {k: [v] for k, v in query.items()} or None,
        "pathParameters": None, "stageVariables": None, "body": body, "isBase64Encoded": False,
        "requestContext": {"requestId": os.urandom(8).hex(), "resourcePath": path, "httpMethod": method, "stage": "local",
                           "identity": {"sourceIp": "127.0.0.1"}, "authorizer": {"claims": claims} if claims else None},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        import analytics_app
        import listings_app
        import ussd_app

        from agricommon import privacy, repo

        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        headers = dict(self.headers.items())
        if url.path == "/config.js":
            cfg = {"env": "local", "region": REGION, "mockAuth": True, "simulator": True}
            return self._send(200, f"window.AGRI={json.dumps(cfg)};".encode(), "application/javascript", {"Cache-Control": "no-store"})
        if url.path == "/dev/outbox":
            try:
                phone = privacy.normalise_phone(query.get("phone", ""))
            except ValueError:
                return self._send(200, b"[]")
            with LOCK:
                rows = repo.outbox(phone)
            return self._send(200, json.dumps(rows).encode(), extra={"Cache-Control": "no-store"})
        if url.path.startswith("/api/"):
            target = listings_app.handler
        elif url.path.startswith("/analytics/"):
            target = analytics_app.handler
        elif url.path in ("/ussd", "/sms"):
            query.setdefault("k", "local")
            target = ussd_app.handler
        else:
            return self._static(url.path)
        with LOCK:
            resp = target(rest_event(method, url.path, query, headers, body), Ctx())
        hdrs = dict(resp.get("headers") or {})
        for k, v in (resp.get("multiValueHeaders") or {}).items():
            hdrs[k] = v[0] if isinstance(v, list) else v
        ctype = hdrs.pop("Content-Type", "application/json")
        self._send(resp["statusCode"], (resp.get("body") or "").encode(), ctype, hdrs)

    def _static(self, path: str) -> None:
        f = (ROOT / "web" / (path.lstrip("/") or "index.html")).resolve()
        if not str(f).startswith(str((ROOT / "web").resolve())) or not f.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                 ".webmanifest": "application/manifest+json"}.get(f.suffix, "application/octet-stream")
        self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def log_message(self, fmt, *args):  # quieter console: API calls only
        if any(p in self.path for p in ("/api/", "/ussd", "/sms", "/analytics/")):
            sys.stderr.write(f"[local] {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    global ROLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--as", dest="role", choices=["buyers", "officers", "admins"], help="treat unauthenticated API calls as this role")
    a = ap.parse_args()
    ROLE = [a.role] if a.role else None
    setup_aws()

    import analytics_app
    import ingestion_app
    import listings_app

    ingestion_app.start_curation = local_curation
    analytics_app.run_athena = local_athena

    def run_now(payload: dict) -> str:  # Admin -> "Run ingestion now"
        threading.Thread(target=run_pipeline, args=(payload,), daemon=True).start()
        return "started"

    listings_app.invoke_ingestion = run_now
    out = run_pipeline()
    rows = sum(r.get("rows", 0) for r in out["results"])
    print(f"[local] ingested {rows} sample rows; {len(CURATED)} curated; analytics ready")
    threading.Thread(target=pump_forever, daemon=True).start()
    print(f"[local] web app:          http://localhost:{a.port}")
    print(f"[local] phone simulator:  http://localhost:{a.port}/simulator.html")
    print("[local] sign in with the role picker (buyer / officer / admin); SMS land in the simulator inbox")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
