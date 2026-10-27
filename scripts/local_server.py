#!/usr/bin/env python3
"""Run the whole platform on your laptop - no AWS account needed.

AWS services are emulated in memory with moto; the real Lambda handlers serve
requests. The synthetic sample data is ingested on start-up and the analytics
series are pre-computed, so every screen works.

  pip install -r requirements-dev.txt
  python scripts/local_server.py            # then open http://localhost:8080
  python scripts/ussd_cli.py                # in a second terminal: USSD simulator

Sign-in is not available locally (no Cognito); use the deployed environment for buyer/officer flows,
or pass --as buyers / --as officers to impersonate a role.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
for svc in (ROOT / "services").iterdir():
    if svc.is_dir():
        sys.path.insert(0, str(svc))

REGION = "af-south-1"
os.environ.update({
    "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local",
    "APP_ENV": "local", "TABLE_NAME": "agriprice-local", "IDEMPOTENCY_TABLE": "agriprice-local-idempotency",
    "EVENT_BUS_NAME": "agriprice-local", "LAKE_BUCKET": "agriprice-local-lake", "GLUE_DATABASE": "agriprice_local",
    "ATHENA_WORKGROUP": "primary", "SMS_MODE": "log", "SECRET_PRIVACY_PEPPER": "local-pepper", "SECRET_AT_CALLBACK_TOKEN": "local",
    "POWERTOOLS_TRACE_DISABLED": "1", "POWERTOOLS_METRICS_NAMESPACE": "AgriPriceLocal", "POWERTOOLS_LOG_LEVEL": "WARNING",
})

from moto import mock_aws  # noqa: E402

MOCK = mock_aws()
MOCK.start()

import boto3  # noqa: E402

ROLE: list[str] | None = None


def setup() -> None:
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
    os.environ["PRICE_QUEUE_URL"] = boto3.client("sqs").create_queue(QueueName="price-normalised")["QueueUrl"]
    boto3.client("events").create_event_bus(Name="agriprice-local")
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="agriprice-local-lake", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.upload_file(str(ROOT / "data/zambia_sample_prices.csv"), "agriprice-local-lake", "seed/zambia_sample_prices.csv")

    import analytics_app
    import ingestion_app

    from agricommon import repo

    ingestion_app.start_curation = lambda day, run: "local-skip"
    res = ingestion_app.run_source({"id": "seed", "type": "wfp_csv_s3", "key": "seed/zambia_sample_prices.csv"}, "agriprice-local-lake", "local", "local", 3650)
    # Emulate the nightly Athena aggregation in Python
    rows = list(ingestion_app.parse_wfp_csv((ROOT / "data/zambia_sample_prices.csv").read_text(), "seed"))
    agg: dict = defaultdict(list)
    for r in rows:
        agg[(r["commodity"], r["province"], r["obs_date"][:7])].append(r["price_ngwee_kg"])
    athena_like = [{"commodity": c, "province": p, "month": m, "avgp": round(sum(v) / len(v)), "minp": min(v), "maxp": max(v), "n": len(v)} for (c, p, m), v in agg.items()]
    for (crop, prov), series in analytics_app.group_rows(athena_like).items():
        repo.put_series(crop, prov, analytics_app.build_payload(series))
    print(f"[local] ingested {res['rows']} sample rows, {res['changes']} current prices; analytics ready")


def rest_event(method: str, path: str, query: dict, headers: dict, body: str | None) -> dict:
    claims = {"sub": "local-user", "cognito:groups": ",".join(ROLE), "phone_number": "+260970000999", "email": "local@example.com"} if ROLE else None
    return {
        "resource": path, "path": path, "httpMethod": method, "headers": headers, "multiValueHeaders": {},
        "queryStringParameters": query or None, "multiValueQueryStringParameters": {k: [v] for k, v in query.items()} or None,
        "pathParameters": None, "stageVariables": None, "body": body, "isBase64Encoded": False,
        "requestContext": {"requestId": os.urandom(8).hex(), "resourcePath": path, "httpMethod": method, "stage": "local",
                           "identity": {"sourceIp": "127.0.0.1"}, "authorizer": {"claims": claims} if claims else None},
    }


class Ctx:
    function_name, memory_limit_in_mb, aws_request_id = "local", 512, "local"
    invoked_function_arn = f"arn:aws:lambda:{REGION}:000000000000:function:local"

    @staticmethod
    def get_remaining_time_in_millis() -> int:
        return 30_000


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        import analytics_app
        import listings_app
        import ussd_app

        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        headers = {k: v for k, v in self.headers.items()}
        if url.path.startswith("/api/"):
            resp = listings_app.handler(rest_event(method, url.path, query, headers, body), Ctx())
        elif url.path.startswith("/analytics/"):
            resp = analytics_app.handler(rest_event(method, url.path, query, headers, body), Ctx())
        elif url.path in ("/ussd", "/sms"):
            query.setdefault("k", "local")
            resp = ussd_app.handler(rest_event(method, url.path, query, headers, body), Ctx())
        else:
            return self._static(url.path)
        self.send_response(resp["statusCode"])
        hdrs = dict(resp.get("headers") or {})
        for k, v in (resp.get("multiValueHeaders") or {}).items():
            hdrs[k] = v[0] if isinstance(v, list) else v
        for k, v in hdrs.items():
            self.send_header(k, v)
        out = (resp.get("body") or "").encode()
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _static(self, path: str) -> None:
        f = (ROOT / "web" / (path.lstrip("/") or "index.html")).resolve()
        if not str(f).startswith(str((ROOT / "web").resolve())) or not f.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in self.path or "/ussd" in self.path:
            sys.stderr.write(f"[local] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    global ROLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--as", dest="role", choices=["buyers", "officers", "admins"], help="impersonate a signed-in role")
    a = ap.parse_args()
    ROLE = [a.role] if a.role else None
    setup()
    print(f"[local] open http://localhost:{a.port}  (USSD endpoint: POST http://localhost:{a.port}/ussd)")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
