# Runbooks

All alarms email **agrizambia5@gmail.com** through SNS topic `agriprice-<env>-alarms`. Start every investigation from the dashboard `agriprice-<env>`.

## errors
*Lambda errors (> 2 per minute) or API 5xx (> 2% in 3 of 5 minutes).*
1. Was there a deployment in the last 30 minutes? Check the CodeDeploy console. During a canary the alarm rolls back automatically; confirm that the `live` alias points at the previous version.
2. Open X-Ray → Traces, filter `service("agriprice-<env>-…") AND error = true`, and open one trace.
3. Logs Insights: `fields @timestamp, message, correlation_id | filter level="ERROR" | sort @timestamp desc`.
4. If this is a bad release outside a canary, revert the commit and push. CI redeploys, or run `python scripts/deploy.py deploy --env <env> --only <service>`.

## latency
*USSD p95 > 1,500 ms for 10 minutes.*
1. Is the cause cold starts? Logs Insights: `filter cold_start=1 | stats count() by bin(5m)`.
2. Check the DynamoDB latency and throttle widgets.
3. Short-term mitigation: add `ProvisionedConcurrencyConfig: {ProvisionedConcurrentExecutions: 1}` to the USSD function (this costs money), then remove it when the cause is fixed.

## dlq
*A message landed in `price-normalised-dlq` or `match-requests-dlq`.*
1. Inspect it: SQS console → Send and receive messages → Poll.
2. Find the error with its `correlation_id` in Logs Insights.
3. Fix the cause, then use **Start DLQ redrive** in the console. Consumers are idempotent, so a redrive is safe.

## ingestion
*`IngestionFailures` > 0 or a message in `ingestion-dlq`.*
1. Logs: `filter message="ingest.source_failed"` shows the source and the stack trace.
2. Usual causes: the source URL changed (update `WFP_CSV_URL`), the CSV columns changed (update `normalise_wfp_row` and add a test), or the NAT instance is unhealthy (check the ASG; it self-heals).
3. Run again after the fix: `python scripts/deploy.py run-pipeline --env <env>`.

## backlog
*Price changes are waiting more than 10 minutes for the Alerts service.*
1. Check Alerts errors and throttles, and the account `ConcurrentExecutions` (new accounts can have a low limit).
2. Check whether the SMS provider is failing: `filter message like /sms/`.
3. Once the cause is fixed, the backlog drains by itself in batches of 10.

## throttling
*DynamoDB throttled requests for 5 minutes.*
1. Check that auto-scaling is at its maximum (dashboard: provisioned vs consumed).
2. Raise `MaxCapacity` in `infra/data.yaml` and redeploy `data`, or switch the table to on-demand (see docs/cost.md, which projects on-demand as cheaper at 10× load).

## budget
*AWS Budgets: forecast above $20, or actual above 25% / 50%.*
1. Open Cost Explorer, group by **Service** and then by tag `stack`.
2. Usual suspects: CloudWatch Logs ingestion (check log levels), the NAT instance type, or Athena (the workgroup caps each query at 1 GB).
