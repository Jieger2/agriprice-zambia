"""Asynchronous messaging: EventBridge domain events and SQS messages with correlation IDs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable

import boto3

from .obs import CORRELATION_ATTR

SOURCE = "agriprice"


def publish_event(detail_type: str, detail: dict, correlation_id: str) -> None:
    boto3.client("events").put_events(
        Entries=[{
            "Source": SOURCE,
            "DetailType": detail_type,
            "Detail": json.dumps({**detail, CORRELATION_ATTR: correlation_id}, separators=(",", ":"), default=str),
            "EventBusName": os.environ["EVENT_BUS_NAME"],
        }]
    )


def send_messages(queue_url: str, bodies: Iterable[dict], correlation_id: str) -> int:
    """Send in batches of 10 (the SQS maximum per request, which also minimises request cost)."""
    sqs = boto3.client("sqs")
    batch, sent = [], 0
    for i, body in enumerate(bodies):
        batch.append({
            "Id": str(i),
            "MessageBody": json.dumps(body, separators=(",", ":")),
            "MessageAttributes": {CORRELATION_ATTR: {"DataType": "String", "StringValue": correlation_id}},
        })
        if len(batch) == 10:
            sent += _flush(sqs, queue_url, batch)
            batch = []
    if batch:
        sent += _flush(sqs, queue_url, batch)
    return sent


def _flush(sqs, queue_url: str, batch: list[dict]) -> int:
    resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=batch)
    if resp.get("Failed"):
        raise RuntimeError(f"SQS batch send failed for {len(resp['Failed'])} messages")
    return len(resp.get("Successful", []))
