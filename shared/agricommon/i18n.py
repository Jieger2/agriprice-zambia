"""Static string tables. Menu vocabulary is small and fixed, so no machine translation.

NOTE: Bemba (bem) and Nyanja (nya) strings are drafts and must be reviewed by
native speakers before any field pilot (see docs/i18n-review.md).
Missing keys fall back to English.
"""

from __future__ import annotations

LANGS = ("en", "bem", "nya")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "main": "AgriPrice Zambia\n1.Check prices\n2.Sell my crop\n3.Price alerts\n4.Language\n5.Register\n6.My account\n7.Buy crops",
        "resume": "Continue where you left off?\n1.Yes\n2.No",
        "pick_crop": "Select crop:",
        "prices_hdr": "{crop} (K/50kg)",
        "no_prices": "No recent {crop} prices yet. Try again later.",
        "sms_hint": "SMS: PRICE {code}",
        "unit_menu": "Count your {crop} in:\n1.Kilograms\n2.50kg bags",
        "qty": "How many {unit} of {crop}?",
        "avail_menu": "Available from:\n1.Now\n2.In 2 weeks\n3.In 1 month",
        "confirm_sell": "Sell {qty}kg {crop} at {market} from {when}?\n1.Yes\n2.No",
        "listed": "Listed {qty}kg {crop}. Buyers' offers will come by SMS.",
        "cancelled": "Cancelled.",
        "alert_dir": "Alert me when {crop} at {market} goes:\n1.Above a price\n2.Below a price",
        "alert_amt": "Price in K per 50kg:",
        "alert_set": "Alert set: {crop} {dir} K{amt}/50kg at {market}.",
        "above": "above",
        "below": "below",
        "lang_menu": "Language:\n1.English\n2.Bemba\n3.Nyanja",
        "lang_set": "Language saved.",
        "pick_prov": "Province:",
        "pick_market": "Nearest market:",
        "ask_name": "Your name (0 to skip):",
        "registered": "Registered {name}at {market}. Dial again for prices.",
        "need_register": "Please register first (option 5).",
        "my_menu": "My account:\n1.My listings ({l})\n2.My alerts ({a})\n3.My buy requests ({d})",
        "pick_cancel_listing": "Cancel which listing?",
        "pick_remove_alert": "Remove which alert?",
        "pick_close_request": "Close which request?",
        "none_listings": "You have no active listings.",
        "none_alerts": "You have no price alerts.",
        "none_requests": "You have no open buy requests.",
        "listing_cancelled": "Listing cancelled: {what}.",
        "alert_removed": "Alert removed: {what}.",
        "request_closed": "Buy request closed: {what}.",
        "buy_qty": "How many kg of {crop} do you want?",
        "buy_price": "Highest price you will pay, K per 50kg:",
        "buy_radius": "Search for sellers within:\n1.25km\n2.50km\n3.100km",
        "buy_confirm": "Buy {qty}kg {crop} up to K{price}/50kg within {km}km of {market}?\n1.Yes\n2.No",
        "buy_posted": "Request posted. Matching sellers will be sent to you by SMS.",
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
        "main": "AgriPrice Zambia\n1.Imitengo\n2.Shitisha\n3.Ukwishibishiwa\n4.Ululimi\n5.Ilembesheni\n6.My account\n7.Buy crops",
        "pick_crop": "Sala icilimwa:",
        "lang_set": "Ululimi lwasungwa.",
        "invalid": "Mwalufyanya. Esheni na kabili.",
    },
    "nya": {
        "main": "AgriPrice Zambia\n1.Mitengo\n2.Gulitsa\n3.Zidziwitso\n4.Chiyankhulo\n5.Lembetsani\n6.My account\n7.Buy crops",
        "pick_crop": "Sankhani mbewu:",
        "lang_set": "Chiyankhulo chasungidwa.",
        "invalid": "Mwasankha molakwika. Yesaninso.",
    },
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    template = table.get(key) or STRINGS["en"][key]
    return template.format(**kwargs) if kwargs else template
