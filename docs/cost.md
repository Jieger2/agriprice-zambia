# Cost

The estimates use published on-demand prices from the thesis (us-east-1, October 2026). eu-north-1 (Stockholm, the deployment Region) is priced close to us-east-1; af-south-1 would be dearer. **Replace them with Cost Explorer actuals** (group by Service, then by tag `stack`) once the system runs.

## Pilot (500 farmers, ~60 k API requests/month)

| Item | USD/month |
|---|---|
| NAT instance t4g.micro + 8 GB gp3 + public IPv4 | 10.42 |
| API Gateway REST (0.06 M requests) | 0.21 |
| Lambda arm64 (always-free tier) | 0.00 |
| DynamoDB provisioned 2–40 RCU/WCU (within 25 free at idle) | 0.00 |
| S3 (~2.5 GB) | 0.11 |
| Athena (3,000 queries × 10 MB minimum) | 0.15 |
| CloudWatch custom metrics | 1.50 |
| CloudTrail bucket | 0.02 |
| **Total** | **≈ 12.40** |

## 10× load

The total rises to about **$22.36**. API Gateway and log ingestion grow; Lambda stays inside the free tier. At this scale switch DynamoDB to **on-demand** (about $1.69 a month at 1.5 M writes and 6 M reads). The table stays provisioned for the project because auto-scaling must be demonstrated.

## Optimisations implemented

| # | Optimisation | Where | Before → after |
|---|---|---|---|
| O1 | NAT instance + free gateway endpoints instead of 2 NAT Gateways | `infra/network.yaml` | $73.00 → $10.42 |
| O2 | Athena over partitioned Parquet + nightly pre-aggregation (users never trigger scans) + 1 GB scan cap | `infra/data.yaml`, `analytics_app.py` | ~$29.30 → $0.15 (pilot) |
| O3 | Parameter Store SecureString instead of Secrets Manager | `config.py` | $2.40 → $0 |
| O4 | 14-day log retention, INFO level, 1% debug sampling | every service template | ≈ $19 → $5.21 at 10× |
| O5 | arm64 (Graviton) Lambdas | every service template | 20% lower GB-s price |
| O6 | CloudFront edge cache for public prices (5 min) + ETag/304 | `infra/web.yaml`, `listings_app.py` | fewer Lambda/DynamoDB calls; fewer bytes on 2G |

**Measuring O2:** run the same aggregation against `raw_prices` (JSON) and `curated_prices` (Parquet). Compare *Data scanned* in the Athena console, and record both in the final report.

**Free plan guard rails:** a $20 budget with email alerts (`infra/budget.yaml`), and the Athena scan cap. `python scripts/deploy.py destroy --env dev` tears down a demo environment.
