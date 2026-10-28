"""Service 5 - Alerts.

Consumes normalised price changes from the SQS queue `price-normalised` and
sends an SMS to every farmer whose threshold the new price crosses.

Exactly-once delivery per farmer/crop/day is enforced with a conditional
write (ALERTLOG item) taken *before* sending and released if the send fails,
so SQS retries and duplicate messages can never double-send.
"""

from __future__ import annotations

import json

from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord

from agricommon import catalog, codec, i18n, obs, repo, sms

logger, tracer, metrics = obs.tools("alerts")
processor = BatchProcessor(event_type=EventType.SQS)


def crossed(alert: dict, new: int, old: int | None) -> bool:
    thr = alert["thr"]
    if alert.get("dir", "up") == "up":
        return new >= thr and (old is None or old < thr)
    return new <= thr and (old is None or old > thr)


def process_change(change: dict) -> int:
    crop, market, new, old, day = change["c"], change["m"], change["p"], change.get("prev") or None, change["d"]
    sent = 0
    crop_name = (catalog.commodity(crop) or {"name": crop})["name"]
    m = catalog.markets().get(market)
    market_name = m.name if m else market
    for alert in repo.alerts_for(crop, market):
        if not crossed(alert, new, old):
            continue
        if not repo.claim_alert_slot(alert["pid"], crop, day):
            continue  # already alerted today (dedupe + frequency cap)
        body = i18n.t(
            "sms_alert", alert.get("lang", "en"), crop=crop_name, market=market_name, price=codec.kwacha_per_bag(new),
            chg=codec.pct(codec.change_tenths(new, old)), target=codec.kwacha_per_bag(alert["thr"]),
        )
        try:
            sms.send(alert["phone"], body)
        except Exception:
            repo.release_alert_slot(alert["pid"], crop, day)
            raise
        sent += 1
    return sent


@tracer.capture_method
def record_handler(record: SQSRecord) -> None:
    logger.set_correlation_id(obs.correlation_from_sqs_record(record.raw_event))
    sent = process_change(json.loads(record.body))
    if sent:
        metrics.add_metric(name="AlertsSent", unit=MetricUnit.Count, value=sent)
        repo.bump(alerts_sent=sent)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    return process_partial_response(event=event, record_handler=record_handler, processor=processor, context=context)
