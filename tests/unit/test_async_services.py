"""Matching and Alerts: SQS consumers with partial-batch failure reporting."""

import json

import boto3
import pytest

from agricommon import catalog, geo, repo


@pytest.fixture
def matching(aws):
    import importlib

    import matching_app

    return importlib.reload(matching_app)


@pytest.fixture
def alerts(aws):
    import importlib

    import alerts_app

    return importlib.reload(alerts_app)


def sqs_record(body: dict, mid="m1", cid="corr-1"):
    return {
        "messageId": mid, "receiptHandle": "rh", "body": json.dumps(body), "attributes": {}, "md5OfBody": "",
        "messageAttributes": {"correlation_id": {"stringValue": cid, "dataType": "String"}},
        "eventSource": "aws:sqs", "eventSourceARN": "arn:aws:sqs:eu-north-1:123456789012:q", "awsRegion": "eu-north-1",
    }


def farmer_listing(market="KTW", qty=800, phone="+260977000001"):
    m = catalog.markets()[market]
    repo.put_farmer("pid-" + market, phone, market)
    return repo.put_supply(geo.cell(m.lat, m.lon), "MZE", "pid-" + market, phone, qty, market, m.lat, m.lon)


def buyer_demand(lat=-12.96, lon=28.64, r=60, pmax=650):
    return repo.put_demand(geo.cell(lat, lon), "MZE", "d1", "buyer-1", "+260966000002", 2000, pmax, lat, lon, r)


# ---------------- matching
def test_rank_supply_filters_by_radius():
    from matching_app import rank_supply_for_demand

    demand = {"lat": -12.96, "lon": 28.64, "r": 40}
    near = {"lat": -12.80, "lon": 28.21, "q": 1}   # Kitwe ~ 50 km
    nearer = {"lat": -12.95, "lon": 28.63, "q": 2}
    ranked = rank_supply_for_demand(demand, [near, nearer])
    assert [s["q"] for s, _ in ranked] == [2]


def test_demand_event_notifies_buyer_and_farmers(matching, ctx, monkeypatch):
    sent = []
    monkeypatch.setattr(matching.sms, "send", lambda to, body: sent.append((to, body)))
    farmer_listing("KTW")
    farmer_listing("NDL", qty=300, phone="+260977000003")
    farmer_listing("LSK", phone="+260977000004")  # too far
    d = buyer_demand()
    event = {"Records": [sqs_record({"detail-type": "DemandPosted", "detail": {"pk": d["PK"], "sk": d["SK"], "correlation_id": "c"}})]}
    resp = matching.handler(event, ctx)
    assert resp == {"batchItemFailures": []}
    buyer_msgs = [b for to, b in sent if to == "+260966000002"]
    assert buyer_msgs and buyer_msgs[0].startswith("AgriPrice: 2 seller(s) of Maize near you. Nearest 300kg at Ndola")
    assert {to for to, _ in sent} == {"+260966000002", "+260977000001", "+260977000003"}
    assert all(len(b) <= 160 for _, b in sent)


def test_listing_event_picks_best_price(matching, ctx, monkeypatch):
    sent = []
    monkeypatch.setattr(matching.sms, "send", lambda to, body: sent.append((to, body)))
    buyer_demand(pmax=600)
    repo.put_demand(geo.cell(-12.96, 28.64), "MZE", "d2", "buyer-2", "+260966000009", 500, 700, -12.96, 28.64, 80)
    li = farmer_listing("NDL")
    event = {"Records": [sqs_record({"detail-type": "ListingCreated", "detail": {"pk": li["PK"], "sk": li["SK"]}})]}
    assert matching.handler(event, ctx)["batchItemFailures"] == []
    farmer_msg = [b for to, b in sent if to == "+260977000001"][0]
    assert "up to K350/50kg" in farmer_msg and "+260966000009" in farmer_msg


def test_missing_item_and_partial_failure(matching, ctx, monkeypatch):
    ok = sqs_record({"detail-type": "ListingCreated", "detail": {"pk": "SUPPLY#x", "sk": "gone"}}, mid="ok")
    bad = sqs_record({"detail-type": "ListingCreated"}, mid="bad")
    resp = matching.handler({"Records": [ok, bad]}, ctx)
    assert resp == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


# ---------------- alerts
@pytest.mark.parametrize("alert,new,old,expected", [
    ({"thr": 600, "dir": "up"}, 610, 590, True),
    ({"thr": 600, "dir": "up"}, 620, 610, False),
    ({"thr": 600, "dir": "up"}, 600, None, True),
    ({"thr": 500, "dir": "down"}, 490, 510, True),
    ({"thr": 500, "dir": "down"}, 520, 510, False),
])
def test_crossed(alert, new, old, expected):
    from alerts_app import crossed

    assert crossed(alert, new, old) is expected


def test_alert_sent_once_per_day(alerts, ctx, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts.sms, "send", lambda to, body: sent.append((to, body)))
    repo.put_alert("MZE", "NDL", "pid1", "+260977000001", 600)
    repo.put_alert("MZE", "NDL", "pid2", "+260977000002", 700)
    change = {"c": "MZE", "m": "NDL", "p": 620, "prev": 580, "d": "2026-10-26"}
    event = {"Records": [sqs_record(change, mid="a"), sqs_record(change, mid="b")]}  # duplicate delivery
    assert alerts.handler(event, ctx) == {"batchItemFailures": []}
    assert len(sent) == 1 and sent[0][0] == "+260977000001"
    assert sent[0][1].startswith("AgriPrice ALERT: Maize at Ndola now K310/50kg (+6.9%)")


def test_alert_slot_released_when_sms_fails(alerts, ctx, monkeypatch):
    def boom(to, body):
        raise RuntimeError("provider down")

    monkeypatch.setattr(alerts.sms, "send", boom)
    repo.put_alert("MZE", "NDL", "pid1", "+260977000001", 600)
    change = {"c": "MZE", "m": "NDL", "p": 620, "prev": 580, "d": "2026-10-26"}
    from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError

    with pytest.raises(BatchProcessingError):  # whole batch failed -> SQS redelivers, then DLQ
        alerts.handler({"Records": [sqs_record(change, mid="x")]}, ctx)
    assert repo.claim_alert_slot("pid1", "MZE", "2026-10-26") is True  # slot was released for the retry


def test_sms_log_mode_and_validation(monkeypatch):
    from agricommon import sms

    assert sms.send("+260977000001", "hi")["status"] == "logged"
    monkeypatch.setenv("SMS_MODE", "carrier-pigeon")
    with pytest.raises(sms.SmsError):
        sms.send("+260977000001", "hi")


def test_sms_sandbox_call(monkeypatch):
    from agricommon import sms

    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"SMSMessageData": {"Recipients": [{"status": "Success"}]}}).encode()

    def fake_urlopen(req, timeout):
        captured["url"], captured["headers"] = req.full_url, dict(req.headers)
        return Resp()

    monkeypatch.setenv("SMS_MODE", "sandbox")
    monkeypatch.setenv("SECRET_AT_API_KEY", "k")
    monkeypatch.setenv("SECRET_AT_USERNAME", "sandbox")
    monkeypatch.setattr(sms.urllib.request, "urlopen", fake_urlopen)
    assert sms.send("+260977000001", "x" * 300)["chars"] == 160
    assert "sandbox" in captured["url"] and captured["headers"]["Apikey"] == "k"


def test_bus_batches_messages(aws):
    from agricommon import bus

    n = bus.send_messages(aws["queue_url"], [{"i": i} for i in range(23)], "cid")
    assert n == 23
    msgs = boto3.client("sqs").receive_message(QueueUrl=aws["queue_url"], MaxNumberOfMessages=1, MessageAttributeNames=["All"])["Messages"]
    assert msgs[0]["MessageAttributes"]["correlation_id"]["StringValue"] == "cid"


def test_pair_notified_once_when_both_events_arrive(matching, ctx, monkeypatch):
    """ListingCreated and DemandPosted for the same pair (or an SQS redelivery) -> one SMS each side."""
    sent = []
    monkeypatch.setattr(matching.sms, "send", lambda to, body: sent.append(to))
    li = farmer_listing("NDL")
    d = buyer_demand()
    events = [sqs_record({"detail-type": "ListingCreated", "detail": {"pk": li["PK"], "sk": li["SK"]}}, mid="1"),
              sqs_record({"detail-type": "DemandPosted", "detail": {"pk": d["PK"], "sk": d["SK"]}}, mid="2"),
              sqs_record({"detail-type": "DemandPosted", "detail": {"pk": d["PK"], "sk": d["SK"]}}, mid="3")]
    assert matching.handler({"Records": events}, ctx)["batchItemFailures"] == []
    assert sorted(sent) == ["+260966000002", "+260977000001"]
    assert len(repo.matches_for("d1")) == 1
