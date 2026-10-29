"""Ingestion and Analytics."""

import gzip
import json

import boto3
import pytest
from conftest import rest_event

from agricommon import repo

CSV = """date,admin1,admin2,market,latitude,longitude,category,commodity,unit,priceflag,pricetype,currency,price,usdprice
#date,#adm1+name,#adm2+name,#loc+market+name,#geo+lat,#geo+lon,#item+type,#item+name,#item+unit,#item+price+flag,#item+price+type,#currency,#value,#value+usd
2026-08-15,Copperbelt,Ndola,Ndola,-12.96,28.64,cereals and tubers,Maize (white),KG,actual,Retail,ZMW,5.50,0.2
2026-09-15,Copperbelt,Ndola,Ndola,-12.96,28.64,cereals and tubers,Maize (white),KG,actual,Retail,ZMW,5.70,0.2
2026-09-15,Copperbelt,Ndola,Ndola,-12.96,28.64,cereals and tubers,Maize (white),50 KG,actual,Wholesale,ZMW,270,10
2026-09-15,Eastern,Lundazi,Lundazi,-12.29,33.17,cereals and tubers,Maize (white),KG,actual,Retail,ZMW,4.10,0.2
2026-09-15,Lusaka,Lusaka,Lusaka,-15.41,28.28,miscellaneous food,Bread,Loaf,actual,Retail,ZMW,25,1
2026-09-15,Atlantis,X,Nowhere,-15.41,28.28,cereals,Maize (white),KG,actual,Retail,ZMW,4.10,0.2
bad-date,Lusaka,Lusaka,Lusaka,-15.41,28.28,cereals,Maize (white),KG,actual,Retail,ZMW,4.10,0.2
"""


@pytest.fixture
def ingestion(aws, monkeypatch):
    import importlib

    import ingestion_app

    mod = importlib.reload(ingestion_app)
    monkeypatch.setattr(mod, "start_curation", lambda day, run: "qid-1")
    return mod


# ---------------- ingestion
def test_parse_and_normalise():
    from ingestion_app import latest_per_key, parse_wfp_csv

    rows = list(parse_wfp_csv(CSV, "wfp"))
    assert len(rows) == 4  # HXL row, bread, unknown province and bad date dropped
    lundazi = [r for r in rows if r["market"] == "LUN"][0]
    assert lundazi["province"] == "EAS" and lundazi["price_ngwee_kg"] == 410
    latest = latest_per_key(rows)
    ndola = latest[("MZE", "NDL")]
    assert ndola["obs_date"] == "2026-09-15" and ndola["ptype"] == "Wholesale" and ndola["price_ngwee_kg"] == 540


def test_run_source_from_s3_with_watermark(ingestion, aws):
    s3 = boto3.client("s3")
    s3.put_object(Bucket="agriprice-test-lake", Key="seed/wfp.csv", Body=CSV.encode())
    src = {"id": "seed", "type": "wfp_csv_s3", "key": "seed/wfp.csv"}
    res = ingestion.run_source(src, "agriprice-test-lake", "2026-10-27", "run1", 3650)
    assert res.pop("months") and res == {"source": "seed", "rows": 4, "changes": 2}
    assert repo.get_price("MZE", "NDL")["p"] == 540
    raw = s3.list_objects_v2(Bucket="agriprice-test-lake", Prefix="raw/ingest_date=2026-10-27/")["Contents"]
    line = gzip.decompress(s3.get_object(Bucket="agriprice-test-lake", Key=raw[0]["Key"])["Body"].read()).splitlines()[0]
    assert json.loads(line)["run_id"] == "run1"
    msgs = boto3.client("sqs").receive_message(QueueUrl=aws["queue_url"], MaxNumberOfMessages=10)["Messages"]
    assert {json.loads(m["Body"])["m"] for m in msgs} == {"NDL", "LUN"}
    again = ingestion.run_source(src, "agriprice-test-lake", "2026-10-27", "run2", 3650)
    assert again["rows"] == 0  # watermark prevents re-ingesting


def test_inbox_source_archives_reports(ingestion):
    s3 = boto3.client("s3")
    s3.put_object(Bucket="agriprice-test-lake", Key="inbox/field/2026-10-20/a.json", Body=json.dumps({"c": "SOY", "m": "CPT", "p": 1450, "d": "2026-10-20"}).encode())
    s3.put_object(Bucket="agriprice-test-lake", Key="inbox/field/2026-10-20/b.json", Body=b"{broken")
    res = ingestion.run_source({"id": "field", "type": "s3_inbox"}, "agriprice-test-lake", "2026-10-27", "r", 30)
    assert res["rows"] == 1 and repo.get_price("SOY", "CPT")["p"] == 1450
    left = s3.list_objects_v2(Bucket="agriprice-test-lake", Prefix="inbox/").get("KeyCount")
    assert left == 0


def test_handler_runs_all_sources_and_reports_failure(ingestion, ctx, monkeypatch):
    boto3.client("s3").put_object(Bucket="agriprice-test-lake", Key="seed/wfp.csv", Body=CSV.encode())
    monkeypatch.setenv("SOURCES_JSON", json.dumps([{"id": "seed", "type": "wfp_csv_s3", "key": "seed/wfp.csv"}, {"id": "bad", "type": "nope"}]))
    out = ingestion.handler({"lookback_days": 3650}, ctx)
    assert out["results"][0]["rows"] == 4 and out["results"][-1] == {"athena_query": "qid-1"}
    monkeypatch.setenv("SOURCES_JSON", json.dumps([{"id": "bad", "type": "nope"}]))
    with pytest.raises(RuntimeError):
        ingestion.handler({}, ctx)


def test_http_source_rejects_plain_http():
    from ingestion_app import _http_text

    with pytest.raises(ValueError):
        _http_text("http://example.com/x.csv")


def test_start_curation_sql(aws, monkeypatch):
    import ingestion_app

    captured = {}

    class FakeAthena:
        def start_query_execution(self, QueryString, WorkGroup):
            captured["sql"] = QueryString
            return {"QueryExecutionId": "q"}

    monkeypatch.setattr(ingestion_app.boto3, "client", lambda name: FakeAthena())
    assert ingestion_app.start_curation("2026-10-27", "run1") == "q"
    assert "INSERT INTO" in captured["sql"] and "run_id = 'run1'" in captured["sql"]


# ---------------- analytics
def test_month_maths_and_gap_fill():
    from analytics_app import fill_gaps, month_add

    assert month_add("2025-11", 3) == "2026-02" and month_add("2026-01", -1) == "2025-12"
    assert fill_gaps([("2026-01", 10), ("2026-03", 30)]) == [("2026-01", 10), ("2026-02", 10), ("2026-03", 30)]
    assert fill_gaps([]) == []


def test_seasonal_index_and_forecast():
    from analytics_app import build_payload, holt_winters, seasonal_index

    # synthetic maize-like seasonality: cheap after harvest (May-Jul), dear in lean season (Jan-Feb)
    pattern = [130, 135, 120, 105, 85, 80, 80, 85, 95, 105, 115, 125]
    series = [(f"{2023 + i // 12}-{i % 12 + 1:02d}", pattern[i % 12] * 5) for i in range(36)]
    si = seasonal_index(series)
    assert si[1] > 120 and si[5] < 85
    fc = holt_winters([float(v) for _, v in series], horizon=3)
    assert len(fc) == 3 and fc[0] > fc[2] * 0.9
    assert holt_winters([5.0, 6.0]) == [6.0, 6.0, 6.0]
    assert holt_winters([float(x) for x in range(14)], horizon=2) == [2.0, 3.0]
    assert holt_winters([]) == [] and seasonal_index([]) == [0] * 12
    payload = build_payload([(m, v, v - 10, v + 10) for m, v in series])
    assert payload["s"][0] == ["202301", 650, 640, 660] and payload["f"][0][0] == "202601"


def test_group_rows_builds_national_series():
    from analytics_app import group_rows

    rows = [
        {"commodity": "MZE", "province": "CB", "month": "2026-09", "avgp": "600", "minp": "550", "maxp": "650", "n": "3"},
        {"commodity": "MZE", "province": "EAS", "month": "2026-09", "avgp": "400", "minp": "380", "maxp": "420", "n": "1"},
    ]
    g = group_rows(rows)
    assert g[("MZE", "ZM")] == [("2026-09", 550, 380, 650)]
    assert g[("MZE", "CB")] == [("2026-09", 600, 550, 650)]


@pytest.fixture
def analytics(aws):
    import importlib

    import analytics_app

    return importlib.reload(analytics_app)


def test_refresh_and_series_api(analytics, ctx, monkeypatch):
    rows = [{"commodity": "MZE", "province": "CB", "month": f"2026-0{m}", "avgp": str(500 + m), "minp": "490", "maxp": "520", "n": "2"} for m in range(1, 10)]
    monkeypatch.setattr(analytics, "run_athena", lambda sql: rows)
    assert analytics.handler({"action": "refresh"}, ctx) == {"series": 2, "rows": 9}
    resp = analytics.handler(rest_event("GET", "/analytics/series", query={"c": "MZE", "r": "CB"}), ctx)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200 and body["s"][0] == ["202601", 501, 490, 520] and len(body["f"]) == 3
    etag = resp["multiValueHeaders"]["ETag"][0] if "multiValueHeaders" in resp and resp["multiValueHeaders"].get("ETag") else resp["headers"]["ETag"]
    assert analytics.handler(rest_event("GET", "/analytics/series", query={"c": "MZE", "r": "CB"}, headers={"If-None-Match": etag}), ctx)["statusCode"] == 304
    assert analytics.handler(rest_event("GET", "/analytics/series", query={"c": "SOY"}), ctx)["statusCode"] == 404
    assert analytics.handler(rest_event("GET", "/analytics/series", query={"c": "XXX"}), ctx)["statusCode"] == 400
    assert analytics.handler(rest_event("GET", "/analytics/series", query={"c": "MZE", "r": "QQ"}), ctx)["statusCode"] == 400


def test_run_athena_polls_and_parses(analytics, monkeypatch):
    class FakePaginator:
        def paginate(self, QueryExecutionId):
            yield {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "a"}, {"VarCharValue": "b"}]}, {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "2"}]}]}}

    class FakeAthena:
        calls = 0

        def start_query_execution(self, **kw):
            return {"QueryExecutionId": "q"}

        def get_query_execution(self, QueryExecutionId):
            FakeAthena.calls += 1
            state = "RUNNING" if FakeAthena.calls < 2 else "SUCCEEDED"
            return {"QueryExecution": {"Status": {"State": state}}}

        def get_paginator(self, name):
            return FakePaginator()

    monkeypatch.setattr(analytics.boto3, "client", lambda name: FakeAthena())
    monkeypatch.setattr(analytics.time, "sleep", lambda s: None)
    assert analytics.run_athena("SELECT 1") == [{"a": "1", "b": "2"}]


def test_backfill_curates_in_chunks_under_athena_partition_limit(aws, monkeypatch):
    """HIVE_TOO_MANY_OPEN_PARTITIONS: one INSERT may write <= 100 partitions (province x month)."""
    import ingestion_app

    sqls, states = [], iter(["SUCCEEDED"] * 50)

    class FakeAthena:
        def start_query_execution(self, QueryString, WorkGroup):
            sqls.append(QueryString)
            return {"QueryExecutionId": f"q{len(sqls)}"}

        def get_query_execution(self, QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": next(states)}}}

    monkeypatch.setattr(ingestion_app.boto3, "client", lambda name: FakeAthena())
    months = {f"{y}-{m:02d}" for y in range(2003, 2025) for m in range(1, 13)}  # 264 months
    out = ingestion_app.curate("2026-10-29", "run1", months, None)
    assert len(sqls) == 30 and all(r["state"] == "SUCCEEDED" for r in out)
    assert all(s.count("'20") <= ingestion_app.MONTHS_PER_QUERY + 1 for s in sqls)  # 9 months + the ingest date
    assert out[0]["months"] == "2003-01..2003-09"
    sqls.clear()
    assert ingestion_app.curate("2026-10-29", "run2", {"2026-09", "2026-10"}, None) == [{"athena_query": "q1"}]
    assert "IN (" not in sqls[0]  # normal runs: one query, no month filter
