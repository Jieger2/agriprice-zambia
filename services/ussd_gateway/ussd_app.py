"""Service 1 - USSD/SMS Gateway.

Receives Africa's Talking USSD and inbound-SMS callbacks through API Gateway,
runs the menu state machine, and answers within the USSD session timeout.

USSD protocol: AT posts form fields sessionId, serviceCode, phoneNumber and
text (all inputs so far joined by '*'). We reply text/plain starting with
'CON ' (keep session open) or 'END ' (close it).

Session state is externalised to DynamoDB (TTL 180 s), so the function is
stateless. If a session drops mid-menu, the next dial within 3 minutes offers
to resume at the same screen.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import os
import re
import urllib.parse
from datetime import date, timedelta

from aws_lambda_powertools.metrics import MetricUnit

from agricommon import bus, catalog, codec, config, geo, i18n, obs, privacy, repo, sms, text

logger, tracer, metrics = obs.tools("ussd-gateway")

DEFAULT_MARKETS = ["LSK", "NDL", "CPT"]
MAX_QTY_KG = 1_000_000
MAX_PRICE_K = 100_000
UNITS = {"1": {"code": "kg", "label": "kg", "kg": 1}, "2": {"code": "bag50", "label": "50kg bags", "kg": 50}}
AVAILABLE_IN_DAYS = {"1": 0, "2": 14, "3": 30}


# ---------------------------------------------------------------- helpers
def _resp(status: int, body: str, content_type: str = "text/plain") -> dict:
    return {"statusCode": status, "headers": {"Content-Type": f"{content_type}; charset=utf-8", "Cache-Control": "no-store"}, "body": body}


def _form(event: dict) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}


def _authorised(event: dict) -> bool:
    qs = event.get("queryStringParameters") or {}
    supplied = qs.get("k", "")
    expected = config.secret("at/callback_token")
    if not supplied or not hmac.compare_digest(supplied, expected):
        return False
    cidrs = [c.strip() for c in os.environ.get("ALLOWED_CIDRS", "").split(",") if c.strip()]
    if cidrs:
        src = ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp", "")
        try:
            ip = ipaddress.ip_address(src)
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(c) for c in cidrs)
    return True


def clean_name(raw: str) -> str:
    """Names typed on a keypad: letters, spaces, apostrophes and hyphens only; '0' skips."""
    raw = raw.strip()
    if raw in ("", "0"):
        return ""
    return re.sub(r"[^A-Za-z' -]", "", raw)[:40].strip().title()


def crop_menu(lang: str) -> str:
    items = [f"{i}.{c['short']}" for i, c in enumerate(catalog.commodities(), start=1)]
    lines = [" ".join(items[i:i + 2]) for i in range(0, len(items), 2)]
    return i18n.t("pick_crop", lang) + "\n" + "\n".join(lines)


def pick(options: list, choice: str):
    if not choice.isdigit():
        return None
    idx = int(choice) - 1
    return options[idx] if 0 <= idx < len(options) else None


def crop_by_keyword(word: str) -> dict | None:
    w = word.strip().lower()
    for c in catalog.commodities():
        if w in (c["code"].lower(), c["name"].lower(), c["short"].lower()) or c["name"].lower().startswith(w):
            return c
    return None


def price_lines(crop: dict, farmer: dict | None) -> tuple[str, list[str]]:
    """Return (header date, rows) for the 3 markets nearest the farmer."""
    mk = catalog.markets()
    home = mk.get((farmer or {}).get("market", ""))
    codes = [m.code for m, _ in catalog.nearest_markets(home.lat, home.lon, 3)] if home else DEFAULT_MARKETS
    prices = repo.get_prices(crop["code"], codes)
    rows, latest = [], ""
    for code in codes:
        p = prices.get(code)
        if not p:
            continue
        latest = max(latest, p["d"])
        chg = codec.pct(codec.change_tenths(p["p"], p.get("prev") or None))
        rows.append(f"{mk[code].name} {codec.kwacha_per_bag(p['p'])[1:]} {chg}")
    stamp = date.fromisoformat(latest).strftime("%d%b") if latest else ""
    return stamp, rows


# ---------------------------------------------------------------- menu state machine
def route(parts: list[str], ctx: dict) -> tuple[str, str]:
    lang, farmer = ctx["lang"], ctx["farmer"]
    crops = catalog.commodities()
    if not parts:
        return "CON", i18n.t("main", lang)
    top, n = parts[0], len(parts)

    if top == "1":  # check prices
        if n == 1:
            return "CON", crop_menu(lang)
        crop = pick(crops, parts[1])
        if not crop:
            return "END", i18n.t("invalid", lang)
        metrics.add_metric(name="PriceLookups", unit=MetricUnit.Count, value=1)
        stamp, rows = price_lines(crop, farmer)
        if not rows:
            return "END", i18n.t("no_prices", lang, crop=crop["name"])
        hdr = i18n.t("prices_hdr", lang, crop=crop["name"].upper()) + f" {stamp}"
        return "END", "\n".join([hdr, *rows, i18n.t("sms_hint", lang, code=crop["short"].upper())])

    home = catalog.markets().get((farmer or {}).get("market", ""))
    if top in ("2", "3") and not home:
        return "END", i18n.t("need_register", lang)

    if top == "2":  # sell my crop (FR2): crop -> unit -> quantity -> available-from -> confirm
        if n == 1:
            return "CON", crop_menu(lang)
        crop = pick(crops, parts[1])
        if not crop:
            return "END", i18n.t("invalid", lang)
        if n == 2:
            return "CON", i18n.t("unit_menu", lang, crop=crop["name"])
        unit = UNITS.get(parts[2])
        if not unit:
            return "END", i18n.t("invalid", lang)
        if n == 3:
            return "CON", i18n.t("qty", lang, unit=unit["label"], crop=crop["name"])
        if not parts[3].isdigit() or not 0 < int(parts[3]) * unit["kg"] <= MAX_QTY_KG:
            return "END", i18n.t("invalid", lang)
        qty = int(parts[3]) * unit["kg"]
        if n == 4:
            return "CON", i18n.t("avail_menu", lang)
        if parts[4] not in AVAILABLE_IN_DAYS:
            return "END", i18n.t("invalid", lang)
        available = date.today() + timedelta(days=AVAILABLE_IN_DAYS[parts[4]])
        if n == 5:
            return "CON", i18n.t("confirm_sell", lang, qty=qty, crop=crop["name"], market=home.name, when=available.strftime("%d%b"))
        if parts[5] != "1":
            return "END", i18n.t("cancelled", lang)
        cell = geo.cell(home.lat, home.lon)
        item = repo.put_supply(cell, crop["code"], ctx["pid"], ctx["phone"], qty, home.code, home.lat, home.lon, unit["code"], available.isoformat())
        bus.publish_event("ListingCreated", {"pk": f"SUPPLY#{cell}", "sk": item["SK"]}, ctx["cid"])
        metrics.add_metric(name="ListingsCreated", unit=MetricUnit.Count, value=1)
        return "END", i18n.t("listed", lang, qty=qty, crop=crop["name"])

    if top == "3":  # price alerts (FR7): crop -> direction -> price
        if n == 1:
            return "CON", crop_menu(lang)
        crop = pick(crops, parts[1])
        if not crop:
            return "END", i18n.t("invalid", lang)
        if n == 2:
            return "CON", i18n.t("alert_dir", lang, crop=crop["name"], market=home.name)
        direction = {"1": "up", "2": "down"}.get(parts[2])
        if not direction:
            return "END", i18n.t("invalid", lang)
        if n == 3:
            return "CON", i18n.t("alert_amt", lang)
        if not parts[3].isdigit() or not 0 < int(parts[3]) <= MAX_PRICE_K:
            return "END", i18n.t("invalid", lang)
        amount_k = int(parts[3])
        threshold = round(amount_k * 100 / codec.BAG_KG)  # ngwee per kg
        repo.put_alert(crop["code"], home.code, ctx["pid"], ctx["phone"], threshold, direction, lang)
        word = i18n.t("above" if direction == "up" else "below", lang)
        return "END", i18n.t("alert_set", lang, crop=crop["name"], dir=word, amt=amount_k, market=home.name)

    if top == "4":  # language
        if n == 1:
            return "CON", i18n.t("lang_menu", lang)
        new = pick(list(i18n.LANGS), parts[1])
        if not new:
            return "END", i18n.t("invalid", lang)
        if farmer:
            repo.set_farmer_lang(ctx["pid"], new)
        else:
            repo.put_farmer(ctx["pid"], ctx["phone"], market="", lang=new)
        return "END", i18n.t("lang_set", new)

    if top == "5":  # register
        provs = catalog.provinces()
        if n == 1:
            return "CON", i18n.t("pick_prov", lang) + "\n" + "\n".join(f"{i}.{p['name']}" for i, p in enumerate(provs, 1))
        prov = pick(provs, parts[1])
        if not prov:
            return "END", i18n.t("invalid", lang)
        mkts = catalog.markets_in_province(prov["code"])
        if n == 2:
            return "CON", i18n.t("pick_market", lang) + "\n" + "\n".join(f"{i}.{m.name}" for i, m in enumerate(mkts, 1))
        market = pick(mkts, parts[2])
        if not market:
            return "END", i18n.t("invalid", lang)
        if n == 3:
            return "CON", i18n.t("ask_name", lang)
        name = clean_name(parts[3])
        repo.put_farmer(ctx["pid"], ctx["phone"], market=market.code, lang=lang, name=name)
        metrics.add_metric(name="FarmersRegistered", unit=MetricUnit.Count, value=1)
        return "END", i18n.t("registered", lang, name=(name + " ") if name else "", market=market.name)

    return "END", i18n.t("invalid", lang)


def handle_ussd(form: dict[str, str]) -> str:
    session_id = form.get("sessionId", "")
    phone = privacy.normalise_phone(form.get("phoneNumber", ""))
    raw = form.get("text", "") or ""
    parts = [p for p in raw.split("*")] if raw else []
    pid = privacy.phone_id(phone)
    logger.append_keys(correlation_id=session_id, pid=pid)

    farmer = repo.get_farmer(pid)
    lang = (farmer or {}).get("lang") or "en"
    sess = repo.get_session(session_id)

    if sess is None:
        offer = repo.get_resume(pid) if not parts else None
        if offer:
            repo.put_session(session_id, offer=offer, prefix="", drop=0, decided=False)
            metrics.add_metric(name="UssdResumeOffered", unit=MetricUnit.Count, value=1)
            return text.ussd("CON", i18n.t("resume", lang))
        sess = {"prefix": "", "drop": 0, "decided": True}
        repo.put_session(session_id, **sess)
        metrics.add_metric(name="UssdSessions", unit=MetricUnit.Count, value=1)
    elif not sess.get("decided", True):
        if not parts:
            return text.ussd("CON", i18n.t("resume", lang))
        accept = parts[0] == "1"
        sess = {"prefix": sess["offer"] if accept else "", "drop": 1, "decided": True}
        repo.put_session(session_id, **sess)

    prefix = sess.get("prefix") or ""
    effective = (prefix.split("*") if prefix else []) + parts[int(sess.get("drop", 0)):]
    ctx = {"lang": lang, "farmer": farmer, "pid": pid, "phone": phone, "cid": session_id}
    kind, body = route(effective, ctx)
    if kind == "CON" and effective:
        repo.put_resume(pid, "*".join(effective))
    elif kind == "END":
        repo.clear_resume(pid)
    return text.ussd(kind, body)


def handle_sms(form: dict[str, str]) -> None:
    """Inbound SMS keywords: PRICE <crop> | STOP | anything else -> help."""
    phone = privacy.normalise_phone(form.get("from", ""))
    pid = privacy.phone_id(phone)
    logger.append_keys(pid=pid)
    words = (form.get("text") or "").strip().split()
    farmer = repo.get_farmer(pid)
    lang = (farmer or {}).get("lang") or "en"
    cmd = words[0].upper() if words else ""
    if cmd == "PRICE" and len(words) > 1 and (crop := crop_by_keyword(words[1])):
        stamp, rows = price_lines(crop, farmer)
        body = i18n.t("sms_price", lang, crop=crop["name"].upper(), date=stamp, rows="; ".join(rows)) if rows else i18n.t("no_prices", lang, crop=crop["name"])
        metrics.add_metric(name="PriceLookups", unit=MetricUnit.Count, value=1)
    elif cmd == "STOP":
        repo.delete_farmer(pid)
        body = i18n.t("sms_stop", lang)
    else:
        body = i18n.t("sms_help", lang)
    sms.send(phone, body)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict, context) -> dict:
    obs.maybe_inject_fault()
    if not _authorised(event):
        logger.warning("callback.unauthorised")
        return _resp(403, "Forbidden")
    form = _form(event)
    path = event.get("resource") or event.get("path") or ""
    try:
        if path.endswith("/sms"):
            handle_sms(form)
            return _resp(200, "")
        return _resp(200, handle_ussd(form))
    except ValueError:
        logger.exception("callback.bad_request")
        return _resp(200, text.ussd("END", i18n.t("invalid"))) if not path.endswith("/sms") else _resp(400, "Bad request")
