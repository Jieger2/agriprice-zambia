import pytest
from conftest import b64, rest_event

PHONE = "+260977123456"


@pytest.fixture
def ussd(aws):
    import importlib

    import ussd_app

    return importlib.reload(ussd_app)


def dial(mod, ctx, text="", session="s1", token="cb-token", phone=PHONE):
    ev = rest_event("POST", "/ussd", body={"sessionId": session, "serviceCode": "*384*123#", "phoneNumber": phone, "text": text}, form=True, query={"k": token})
    resp = mod.handler(ev, ctx)
    return resp["statusCode"], resp["body"]


def seed_price(crop="MZE", market="NDL", p=570, d="2026-10-26", prev=552):
    import boto3

    boto3.resource("dynamodb").Table("agriprice-test").put_item(Item={"PK": f"PRICE#{crop}#{market}", "SK": "CURRENT", "p": p, "d": d, "prev": prev, "prov": "CB", "src": "t"})


def register(mod, ctx):
    # 5 = register, 2 = Copperbelt, 1 = Ndola
    return dial(mod, ctx, "5*2*1", session="reg")


def test_rejects_bad_token(ussd, ctx):
    status, _ = dial(ussd, ctx, token="wrong")
    assert status == 403


def test_ip_allowlist(ussd, ctx, monkeypatch):
    monkeypatch.setenv("ALLOWED_CIDRS", "10.0.0.0/8")
    assert dial(ussd, ctx)[0] == 403
    monkeypatch.setenv("ALLOWED_CIDRS", "196.201.214.0/24")
    assert dial(ussd, ctx)[0] == 200


def test_main_menu_and_every_response_fits(ussd, ctx):
    status, body = dial(ussd, ctx)
    assert status == 200 and body.startswith("CON AgriPrice") and len(body) <= 182


def test_register_then_check_prices(ussd, ctx):
    status, body = dial(ussd, ctx, "5", session="r1")
    assert "Copperbelt" in body
    _, body = dial(ussd, ctx, "5*2", session="r1")
    assert "1.Ndola" in body
    _, body = register(ussd, ctx)
    assert body.startswith("END Registered at Ndola")
    seed_price()
    seed_price(market="KTW", p=560, prev=560)
    _, body = dial(ussd, ctx, "1", session="p1")
    assert "1.Maize" in body
    _, body = dial(ussd, ctx, "1*1", session="p1")
    assert body.startswith("END MAIZE (K/50kg) 26Oct")
    assert "Ndola 285 +3.3%" in body and "Kitwe 280 =" in body
    assert len(body) <= 182


def test_prices_for_unregistered_and_none(ussd, ctx):
    _, body = dial(ussd, ctx, "1*2", session="p2")
    assert body.startswith("END No recent Soya beans prices")
    _, body = dial(ussd, ctx, "1*9", session="p3")
    assert "Invalid" in body


def test_sell_requires_registration(ussd, ctx):
    _, body = dial(ussd, ctx, "2", session="x")
    assert "register first" in body


def test_sell_flow_creates_listing_and_event(ussd, ctx):
    register(ussd, ctx)
    assert dial(ussd, ctx, "2*1", session="s")[1].startswith("CON Quantity of Maize")
    assert "Sell 500kg Maize at Ndola?" in dial(ussd, ctx, "2*1*500", session="s")[1]
    _, body = dial(ussd, ctx, "2*1*500*1", session="s")
    assert body.startswith("END Listed 500kg Maize")
    import boto3

    items = boto3.resource("dynamodb").Table("agriprice-test").scan()["Items"]
    assert any(i["PK"].startswith("SUPPLY#") and i["q"] == 500 for i in items)
    assert dial(ussd, ctx, "2*1*500*2", session="s2")[1] == "END Cancelled."
    assert "Invalid" in dial(ussd, ctx, "2*1*abc", session="s3")[1]


def test_alert_flow(ussd, ctx):
    register(ussd, ctx)
    assert "above K per 50kg" in dial(ussd, ctx, "3*1", session="a")[1]
    _, body = dial(ussd, ctx, "3*1*300", session="a")
    assert body == "END Alert set: Maize above K300/50kg at Ndola."
    import boto3

    item = boto3.resource("dynamodb").Table("agriprice-test").scan(FilterExpression="begins_with(PK, :p)", ExpressionAttributeValues={":p": "ALERT#"})["Items"][0]
    assert item["thr"] == 600 and item["PK"] == "ALERT#MZE#NDL"
    assert "Invalid" in dial(ussd, ctx, "3*1*0", session="a2")[1]


def test_language_switch(ussd, ctx):
    assert "1.English" in dial(ussd, ctx, "4", session="l")[1]
    _, body = dial(ussd, ctx, "4*3", session="l")
    assert body == "END Chiyankhulo chasungidwa."
    _, body = dial(ussd, ctx, "", session="l2")
    assert "Mitengo" in body
    register(ussd, ctx)
    dial(ussd, ctx, "4*1", session="l3")
    assert "Check prices" in dial(ussd, ctx, "", session="l4")[1]


def test_resume_after_dropped_session(ussd, ctx):
    register(ussd, ctx)
    dial(ussd, ctx, "2", session="d1")
    dial(ussd, ctx, "2*1", session="d1")  # session drops here
    _, body = dial(ussd, ctx, "", session="d2")
    assert "Continue where you left off" in body
    _, body = dial(ussd, ctx, "", session="d2")  # still waiting for an answer
    assert "Continue" in body
    _, body = dial(ussd, ctx, "1", session="d2")
    assert body.startswith("CON Quantity of Maize")  # back on the quantity screen
    _, body = dial(ussd, ctx, "1*200", session="d2")
    assert "Sell 200kg" in body
    # declining the resume returns the main menu
    dial(ussd, ctx, "2*1", session="d3")
    dial(ussd, ctx, "", session="d4")
    assert "Check prices" in dial(ussd, ctx, "2", session="d4")[1]


def test_invalid_top_level_and_bad_phone(ussd, ctx):
    assert "Invalid" in dial(ussd, ctx, "9", session="i")[1]
    status, body = dial(ussd, ctx, "", session="i2", phone="abc")
    assert status == 200 and body.startswith("END")


def test_base64_body(ussd, ctx):
    ev = rest_event("POST", "/ussd", body={"sessionId": "b", "phoneNumber": PHONE, "text": ""}, form=True, query={"k": "cb-token"})
    ev["body"], ev["isBase64Encoded"] = b64(ev["body"]), True
    assert ussd.handler(ev, ctx)["body"].startswith("CON")


def sms_in(mod, ctx, text, phone=PHONE):
    ev = rest_event("POST", "/sms", body={"from": phone, "to": "12345", "text": text}, form=True, query={"k": "cb-token"}, resource="/sms")
    return mod.handler(ev, ctx)


def test_sms_keywords(ussd, ctx, monkeypatch):
    sent = []
    monkeypatch.setattr(ussd.sms, "send", lambda to, body: sent.append((to, body)))
    register(ussd, ctx)
    seed_price()
    assert sms_in(ussd, ctx, "price maize")["statusCode"] == 200
    assert sent[-1][1].startswith("MAIZE K/50kg 26Oct: Ndola 285")
    sms_in(ussd, ctx, "PRICE rice")
    assert "No recent Rice" in sent[-1][1]
    sms_in(ussd, ctx, "hello")
    assert "send PRICE MAIZE" in sent[-1][1]
    sms_in(ussd, ctx, "STOP")
    assert "deleted" in sent[-1][1]
    assert sms_in(ussd, ctx, "PRICE maize", phone="bad")["statusCode"] == 400
