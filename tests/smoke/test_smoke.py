"""Post-deploy smoke tests against a real environment.

Run: pytest tests/smoke --env dev   (reads .deploy-outputs-<env>.json written by scripts/deploy.py)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def out(request) -> dict:
    env = request.config.getoption("--env")
    path = ROOT / f".deploy-outputs-{env}.json"
    if not path.exists():
        pytest.skip(f"{path.name} not found - deploy first")
    return json.loads(path.read_text())


def ussd(url: str, text: str, session: str, phone: str = "+260970000001") -> requests.Response:
    return requests.post(url, data={"sessionId": session, "serviceCode": "*384*1#", "phoneNumber": phone, "text": text}, timeout=10)


def test_ussd_main_menu_within_budget(out):
    r = ussd(out["ussd_callback"], "", uuid.uuid4().hex)
    assert r.status_code == 200 and r.text.startswith("CON ") and len(r.text) <= 182


def test_ussd_rejects_missing_token(out):
    r = ussd(out["ussd_callback"].split("?")[0], "", uuid.uuid4().hex)
    assert r.status_code == 403


def test_price_check_journey(out):
    sid = uuid.uuid4().hex
    assert ussd(out["ussd_callback"], "1", sid).text.startswith("CON")
    r = ussd(out["ussd_callback"], "1*1", sid)
    assert r.status_code == 200 and r.text.startswith("END") and len(r.text) <= 182


def test_public_prices_via_cdn(out):
    r = requests.get(out["web_app"] + "/api/prices", params={"c": "MZE", "m": "LSK,NDL"}, timeout=10)
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert r.headers.get("ETag")


def test_web_app_served(out):
    r = requests.get(out["web_app"], timeout=10)
    assert r.status_code == 200 and "AgriPrice" in r.text
