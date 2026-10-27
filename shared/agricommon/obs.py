"""Observability helpers: one Logger/Tracer/Metrics per service with correlation IDs.

Correlation IDs are created at the edge (API Gateway request id, or the USSD
sessionId) and carried in EventBridge event detail ("correlation_id") and SQS
message attributes ("correlation_id") so a single Logs Insights query follows a
request across every service.
"""

from __future__ import annotations

import os
import random
import uuid

from aws_lambda_powertools import Logger, Metrics, Tracer

NAMESPACE = os.environ.get("POWERTOOLS_METRICS_NAMESPACE", "AgriPrice")
CORRELATION_ATTR = "correlation_id"


def tools(service: str) -> tuple[Logger, Tracer, Metrics]:
    logger = Logger(service=service)
    tracer = Tracer(service=service)
    metrics = Metrics(namespace=NAMESPACE, service=service)
    return logger, tracer, metrics


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def correlation_from_sqs_record(record: dict) -> str:
    attrs = record.get("messageAttributes") or {}
    attr = attrs.get(CORRELATION_ATTR) or {}
    cid = attr.get("stringValue")
    if cid:
        return cid
    # EventBridge -> SQS: the body is the event envelope
    body = record.get("body") or ""
    if CORRELATION_ATTR in body:
        try:
            import json

            detail = json.loads(body).get("detail") or {}
            if detail.get(CORRELATION_ATTR):
                return detail[CORRELATION_ATTR]
        except ValueError:
            pass
    return record.get("messageId") or new_correlation_id()


def maybe_inject_fault() -> None:
    """Chaos hook used ONLY to demonstrate canary rollback (CHAOS_ERROR_RATE, default 0)."""
    rate = float(os.environ.get("CHAOS_ERROR_RATE", "0") or 0)
    if rate > 0 and random.random() < rate:
        raise RuntimeError("Injected fault (CHAOS_ERROR_RATE) for rollback demonstration")
