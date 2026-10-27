# SLIs and SLOs: core user journey

**Journey:** a farmer dials the USSD code, selects a commodity and receives the current regional price.

| SLI | Measurement (CloudWatch) | SLO (28-day window) | Alarm |
|---|---|---|---|
| Availability | `1 - 5XXError/Count` on API `agriprice-<env>-ussd` | ≥ 99.5% (error budget ≈ 300 failed requests per 60 k) | `ussd-api-5xx` (> 2% in 3 of 5 min) |
| Latency | Lambda `Duration` p95 / p99 for `ussd-gateway` (server side) | 95% ≤ 1,000 ms; 99% ≤ 2,500 ms | `ussd-latency-p95` (> 1,500 ms for 10 min) |
| Freshness | `IngestionFailures` = 0 per 6-hour run | ≥ 99% of runs succeed | `ingestion-source-failures`, `ingestion-dlq-not-empty` |
| Alert timeliness | `ApproximateAgeOfOldestMessage` on `price-normalised` | 99% of alerts handed to SMS within 10 min | `alerts-backlog` (> 600 s) |

**Error-budget policy:** if more than half the budget is spent in the first 14 days of a window, the team pauses feature work for the next sprint and works on reliability. The dashboard widget *SLO – USSD availability* shows the indicator against the 99.5% line.

**Load-test evidence:** `load/ussd_load.js` encodes the availability and latency SLOs as k6 thresholds (`http_req_failed < 0.5%`, `p(95) < 1000 ms`).
