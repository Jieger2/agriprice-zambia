"""Static string tables. Menu vocabulary is small and fixed, so no machine translation.

NOTE: Bemba (bem) and Nyanja (nya) strings are drafts and must be reviewed by
native speakers before any field pilot (see docs/i18n-review.md).
Missing keys fall back to English.
"""

from __future__ import annotations

LANGS = ("en", "bem", "nya")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "main": "AgriPrice Zambia\n1.Check prices\n2.Sell my crop\n3.Price alerts\n4.Language\n5.Register",
        "resume": "Continue where you left off?\n1.Yes\n2.No",
        "pick_crop": "Select crop:",
        "prices_hdr": "{crop} (K/50kg)",
        "no_prices": "No recent {crop} prices yet. Try again later.",
        "sms_hint": "SMS: PRICE {code}",
        "qty": "Quantity of {crop} in kg:",
        "confirm_sell": "Sell {qty}kg {crop} at {market}?\n1.Yes\n2.No",
        "listed": "Listed {qty}kg {crop}. Buyers' offers will come by SMS.",
        "cancelled": "Cancelled.",
        "alert_amt": "Alert me when {crop} at {market} is above K per 50kg:",
        "alert_set": "Alert set: {crop} above K{amt}/50kg at {market}.",
        "lang_menu": "Language:\n1.English\n2.Bemba\n3.Nyanja",
        "lang_set": "Language saved.",
        "pick_prov": "Province:",
        "pick_market": "Nearest market:",
        "registered": "Registered at {market}. Dial again for prices.",
        "need_register": "Please register first (option 5).",
        "invalid": "Invalid choice. Please dial again.",
        "more": "0.More",
        "sms_match_farmer": "AgriPrice: buyer wants {qty}kg {crop} near {market}, up to {price}/50kg. Call {phone}",
        "sms_match_buyer": "AgriPrice: {n} seller(s) of {crop} near you. Nearest {qty}kg at {market} ({km}km). Call {phone}",
        "sms_alert": "AgriPrice ALERT: {crop} at {market} now {price}/50kg ({chg}). Your target {target}.",
        "sms_price": "{crop} K/50kg {date}: {rows}",
        "sms_help": "AgriPrice: send PRICE MAIZE, PRICE SOYA, PRICE BEANS... or STOP to delete your data.",
        "sms_stop": "AgriPrice: your data has been deleted. Dial again to re-register.",
    },
    "bem": {
        "main": "AgriPrice Zambia\n1.Imitengo\n2.Shitisha\n3.Ukwishibishiwa\n4.Ululimi\n5.Ilembesheni",
        "pick_crop": "Sala icilimwa:",
        "lang_set": "Ululimi lwasungwa.",
        "invalid": "Mwalufyanya. Esheni na kabili.",
    },
    "nya": {
        "main": "AgriPrice Zambia\n1.Mitengo\n2.Gulitsa\n3.Zidziwitso\n4.Chiyankhulo\n5.Lembetsani",
        "pick_crop": "Sankhani mbewu:",
        "lang_set": "Chiyankhulo chasungidwa.",
        "invalid": "Mwasankha molakwika. Yesaninso.",
    },
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    template = table.get(key) or STRINGS["en"][key]
    return template.format(**kwargs) if kwargs else template
