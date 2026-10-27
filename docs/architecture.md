# Architecture

## Services

| # | Service | Trigger | Writes | Stack |
|---|---|---|---|---|
| 1 | USSD/SMS Gateway | API Gateway (Africa's Talking callbacks) | sessions, farmers, listings, alerts; `ListingCreated` events | `agriprice-<env>-ussd-gateway` |
| 2 | Farmer & Listings API | API Gateway + Cognito | farmers, listings, demand; `ListingCreated`/`DemandPosted`; field reports to S3 inbox | `agriprice-<env>-listings-api` |
| 3 | Price Ingestion | EventBridge schedule, every 6 h | S3 raw zone, current prices, SQS `price-normalised`, Athena `INSERT INTO` curated Parquet | `agriprice-<env>-ingestion` |
| 4 | Matching | SQS `match-requests` (from the EventBridge bus) | SMS to buyers and farmers | `agriprice-<env>-matching` |
| 5 | Alerts | SQS `price-normalised` | SMS, `ALERTLOG` dedupe items | `agriprice-<env>-alerts` |
| 6 | Analytics API | API Gateway; nightly schedule | cached series (`SERIES#`) | `agriprice-<env>-analytics-api` |

Each service has its own template, IAM role, alarms, log group (14-day retention) and copy of the shared layer, so redeploying one never touches another. Shared resources live in the `network`, `data`, `web` and `observability` stacks and reach the services through CloudFormation exports.

## Asynchronous paths

- **Ingestion → Alerts:** SQS `price-normalised`, batch 10, partial-batch failure reporting, DLQ after 3 attempts, alarm on message age above 10 minutes (the alert SLO).
- **Listings/USSD → Matching:** EventBridge bus `agriprice-<env>`, then a rule, then SQS `match-requests`, with a DLQ. Adding a consumer later only needs a new rule.
- Events carry **keys only**, never phone numbers. Consumers re-read the item.
- Consumers are idempotent. Alerts takes a conditional `ALERTLOG` write before sending and releases it if the send fails, which also caps alerts at one SMS per farmer, crop and day.

## Data model (DynamoDB single table)

| Entity | PK | SK | Notes |
|---|---|---|---|
| Farmer | `FARMER#<pid>` | `PROFILE` | `pid` = HMAC-SHA256(phone, pepper)[:20] |
| Current price | `PRICE#<crop>#<market>` | `CURRENT` | conditional update: only newer observations win |
| Listing (supply) | `SUPPLY#<geohash3>` | `<crop>#<ts>#<pid>` | TTL 30 days |
| Demand | `DEMAND#<geohash3>` | `<crop>#<ts>#<id>` | TTL 30 days; `GSI1PK=OWNER#<sub>` for "my demand" |
| Alert | `ALERT#<crop>#<market>` | `<pid>#<dir>#<threshold>` | |
| Alert log | `ALERTLOG#<pid>#<crop>` | `<date>` | TTL 2 days |
| USSD session / resume | `SESSION#<id>` / `RESUME#<pid>` | `STATE` / `LAST` | TTL 180 s: the stateless tier |
| Analytics cache | `SERIES#<crop>#<province>` | `ALL` | rebuilt nightly |
| Watermark | `SOURCE#<id>` | `WATERMARK` | prevents re-ingesting rows |

**Why DynamoDB rather than RDS/PostGIS:** every access pattern is a key lookup or a prefix query. It is Multi-AZ by default, scales to zero cost when idle, and its auto-scaling is easy to demonstrate. Multi-AZ RDS would cost two always-on instances.

**Geospatial queries:** supply and demand are partitioned by a precision-3 geohash cell (about 156 × 156 km). A search reads the cell and its eight neighbours (9 queries, covering any radius up to 150 km) and then filters by haversine distance. *Change from the thesis:* the thesis proposed precision 4. Precision 3 keeps every search at 9 key lookups for the brief's 50–150 km buyer radius, where precision 4 would need up to 49. At pilot and 10× volumes each cell holds only tens of items. If volume grows, raise `CELL_PRECISION` in `shared/agricommon/geo.py`.

## Data lake

```
s3://agriprice-<env>-lake-<acct>/
  seed/                  synthetic CSV (demo source)
  inbox/field/<date>/    officer field reports (JSON), moved to processed/ after ingestion
  raw/ingest_date=YYYY-MM-DD/<source>-<run>.json.gz     raw zone (JSON lines, gzip)
  curated/province=XX/month=YYYY-MM/*.parquet           written by Athena INSERT INTO
  athena-results/        7-day lifecycle
```

The `raw_prices` table uses partition projection, so no crawler is needed. `curated_prices` partitions are registered automatically by `INSERT INTO`. The Athena workgroup enforces a 1 GB scan limit per query.

## Region

The system deploys to **eu-north-1 (Stockholm)**. The thesis preferred af-south-1 (Cape Town) for latency, but that Region needs an account opt-in, and eu-north-1 is enabled by default with prices close to us-east-1. A Zambia–Stockholm round trip of roughly 200–250 ms still fits the USSD latency SLO, which allows 1,000 ms server-side and seconds of operator timeout. The farmer-facing web traffic is served from CloudFront edge locations in Africa (PriceClass_200). The Region is a single deploy-time setting (`AWS_REGION`), so moving to Cape Town later needs no code changes.

## Network

- A VPC across two AZs, with two private subnets (Lambda) and two public subnets (NAT only).
- **fck-nat** runs on a t4g.nano in an Auto Scaling group. A static ENI carries the private routes, so a replacement instance takes over without route changes.
- S3 and DynamoDB gateway endpoints are free, so the busiest traffic never passes through the NAT.
- The Lambda security group allows HTTPS egress only and no ingress.
- **Trade-off:** the NAT is a single instance in AZ-a. The farmer price-lookup path (API Gateway → Lambda → DynamoDB through the gateway endpoint) does **not** depend on it, so price lookups survive the loss of either AZ. Outbound internet (ingestion sources, SMS, SSM, SQS, EventBridge) pauses until the ASG replaces the instance, and that work is queued and retried.

## Low-bandwidth design

| Channel | Budget | How |
|---|---|---|
| USSD screen | ≤ 182 chars | numbered menus, GSM-7 only; every string is contract-tested in `tests/unit/test_shared.py` |
| SMS | ≤ 160 GSM-7 chars | templates trimmed by `text.sms()` |
| JSON price record | ≤ 64 bytes | `{"c":"MZE","m":"NDL","p":28500,"d":20752,"t":33}` = 48 B (verbose form 175 B) |
| HTTP | | ETag/304, gzip ≥ 1 KB, CloudFront edge cache for public prices (5 min), same-origin (no CORS preflight) |
| Web app | ~45 KB total | no framework or chart library, service-worker app shell, offline outbox with idempotency keys |
| Offline | | USSD resume within 3 minutes; SMS keywords need no session |
