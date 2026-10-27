import pytest

from agricommon import catalog, codec, geo, i18n, privacy, text


# ---------------- geo
def test_geohash_known_value():
    assert geo.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_geohash_roundtrip_and_bounds():
    gh = geo.encode(-12.9587, 28.6366, 7)
    lat, lon = geo.decode(gh)
    assert abs(lat + 12.9587) < 0.01 and abs(lon - 28.6366) < 0.01
    with pytest.raises(ValueError):
        geo.bounds("a!")
    with pytest.raises(ValueError):
        geo.encode(100, 0)


def test_neighbours_cover_nine_cells():
    cell = geo.cell(-15.4167, 28.2833)
    n = geo.neighbours(cell)
    assert len(n) == 9 and n[0] == cell and len(set(n)) == 9


def test_haversine_lusaka_ndola():
    km = geo.haversine_km(-15.4167, 28.2833, -12.9587, 28.6366)
    assert 260 < km < 290


# ---------------- catalog
@pytest.mark.parametrize("raw,code", [
    ("Maize (white)", "MZE"), ("Maize meal (white, roller)", "MML"), ("Groundnuts (shelled)", "GNT"),
    ("Soybeans", "SOY"), ("Beans (sugar)", "BNS"), ("Cassava (dry)", "CAS"), ("Bread", None),
])
def test_match_commodity(raw, code):
    assert catalog.match_commodity(raw) == code


@pytest.mark.parametrize("unit,kg", [("KG", 1), ("50 KG", 50), ("25 kg", 25), ("Bag (50 KG)", 50), ("Loaf", None), ("0 KG", None)])
def test_unit_to_kg(unit, kg):
    assert catalog.unit_to_kg(unit) == kg


def test_to_ngwee_per_kg():
    assert catalog.to_ngwee_per_kg(285, "50 KG") == 570
    assert catalog.to_ngwee_per_kg(10, "KG", "USD") is None
    assert catalog.to_ngwee_per_kg(-1, "KG") is None


def test_province_and_market_matching():
    assert catalog.match_province("North Western") == "NW"
    assert catalog.match_province("Copperbelt") == "CB"
    assert catalog.match_province("Atlantis") is None
    assert catalog.match_market("ndola").code == "NDL"
    assert catalog.match_market("Nowhere") is None
    assert catalog.market_code_for("Nakonde Border") == "NAK"
    assert catalog.market_code_for("X") == "XXX"


def test_nearest_markets_and_province():
    near = catalog.nearest_markets(-12.95, 28.63, 3)
    assert near[0][0].code == "NDL" and len(near) == 3
    assert {m.code for m in catalog.markets_in_province("CB")} == {"NDL", "KTW", "CHG"}
    assert catalog.markets()["LSK"].geohash.startswith("k")
    assert catalog.commodity("MZE")["name"] == "Maize" and catalog.commodity("XXX") is None


# ---------------- codec: byte budget
def test_compact_price_record_within_budget():
    rec = codec.price_record("MZE", "NDL", 570, "2026-10-26", 552)
    assert rec == {"c": "MZE", "m": "NDL", "p": 28500, "d": 20752, "t": 33}
    assert len(codec.dumps(rec).encode()) <= 64  # NFR1


def test_codec_helpers():
    assert codec.from_day_number(codec.day_number("2026-10-26")).isoformat() == "2026-10-26"
    assert codec.change_tenths(110, 100) == 100 and codec.change_tenths(5, None) == 0
    assert codec.kwacha_per_bag(570) == "K285"
    assert codec.kwacha_per_bag(100) == "K50"
    assert codec.pct(0) == "=" and codec.pct(32) == "+3.2%" and codec.pct(-150) == "-15%"
    assert codec.etag("abc") == codec.etag("abc") != codec.etag("abd")


# ---------------- text budgets (contract tests)
def test_gsm7_and_fit():
    assert text.to_gsm7("price – “ok”") == 'price - "ok"'
    assert text.is_gsm7("Hello K285") and not text.is_gsm7("Zoł")
    long = "\n".join([f"line {i} xxxxxxxxxxxxxxxxxxxx" for i in range(20)])
    assert len(text.ussd("CON", long)) <= text.USSD_MAX
    assert len(text.sms("x" * 400)) == text.SMS_MAX
    with pytest.raises(ValueError):
        text.ussd("BAD", "x")


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_every_menu_string_fits_ussd(lang):
    for key in i18n.STRINGS["en"]:
        rendered = i18n.t(key, lang, **{k: "Kapiri Mposhi" for k in ("crop", "market", "code", "phone", "price", "chg", "target", "date", "rows", "qty", "amt", "n", "km")})
        assert text.is_gsm7(text.to_gsm7(rendered))
        if not key.startswith("sms_"):
            assert len("CON " + rendered) <= text.USSD_MAX, key


def test_i18n_fallback():
    assert i18n.t("qty", "bem", crop="Maize").startswith("Quantity")
    assert i18n.t("main", "zz") == i18n.t("main", "en")


# ---------------- privacy
@pytest.mark.parametrize("raw", ["0977123456", "977123456", "+260977123456", "260977123456", "+260 977-123-456"])
def test_normalise_phone(raw):
    assert privacy.normalise_phone(raw) == "+260977123456"


def test_phone_helpers():
    with pytest.raises(ValueError):
        privacy.normalise_phone("abc")
    a, b = privacy.phone_id("0977123456"), privacy.phone_id("+260977123456")
    assert a == b and len(a) == 20
    assert privacy.mask("0977123456") == "+260977***456"
