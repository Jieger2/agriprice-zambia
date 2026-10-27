"""Shared fixtures: moto-backed AWS resources and API Gateway event builders."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
for svc in (ROOT / "services").iterdir():
    if svc.is_dir():
        sys.path.insert(0, str(svc))

REGION = "eu-north-1"
os.environ.update({
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "APP_ENV": "test",
    "TABLE_NAME": "agriprice-test",
    "IDEMPOTENCY_TABLE": "agriprice-test-idempotency",
    "EVENT_BUS_NAME": "agriprice-test",
    "LAKE_BUCKET": "agriprice-test-lake",
    "GLUE_DATABASE": "agriprice_test",
    "ATHENA_WORKGROUP": "primary",
    "SMS_MODE": "log",
    "SECRET_PRIVACY_PEPPER": "test-pepper",
    "SECRET_AT_CALLBACK_TOKEN": "cb-token",
    "POWERTOOLS_TRACE_DISABLED": "1",
    "POWERTOOLS_METRICS_NAMESPACE": "AgriPriceTest",
    "POWERTOOLS_SERVICE_NAME": "test",
    "POWERTOOLS_DEV": "false",
})


class FakeContext:
    function_name = "test-fn"
    memory_limit_in_mb = 256
    invoked_function_arn = f"arn:aws:lambda:{REGION}:123456789012:function:test-fn"
    aws_request_id = "req-123"

    @staticmethod
    def get_remaining_time_in_millis() -> int:
        return 30_000


@pytest.fixture
def ctx():
    return FakeContext()


@pytest.fixture
def aws():
    import boto3
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="agriprice-test",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="agriprice-test-idempotency",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="price-normalised")["QueueUrl"]
        os.environ["PRICE_QUEUE_URL"] = queue_url
        boto3.client("events", region_name=REGION).create_event_bus(Name="agriprice-test")
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="agriprice-test-lake", CreateBucketConfiguration={"LocationConstraint": REGION})
        yield {"queue_url": queue_url}


def rest_event(method: str, path: str, *, body=None, query=None, headers=None, groups=None, sub="user-1", phone="+260977000111", form=False, resource=None):
    claims = None
    if groups is not None:
        claims = {"sub": sub, "cognito:groups": ",".join(groups), "phone_number": phone}
    if form:
        raw = urllib.parse.urlencode(body or {})
        content_type = "application/x-www-form-urlencoded"
    else:
        raw = json.dumps(body) if body is not None else None
        content_type = "application/json"
    return {
        "resource": resource or path,
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": content_type, **(headers or {})},
        "multiValueHeaders": {},
        "queryStringParameters": query,
        "multiValueQueryStringParameters": {k: [v] for k, v in (query or {}).items()} if query else None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "requestId": "req-abc",
            "resourcePath": resource or path,
            "httpMethod": method,
            "stage": "live",
            "path": path,
            "identity": {"sourceIp": "196.201.214.10"},
            "authorizer": {"claims": claims} if claims else None,
        },
        "body": raw,
        "isBase64Encoded": False,
    }


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()
