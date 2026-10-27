# Security

## Controls mapped to the brief

| Requirement | Implementation | Evidence |
|---|---|---|
| No credentials in Git (automated) | gitleaks over the full history in CI (`secret-scan` job) and as a pre-commit hook; `.gitignore` excludes `.env`, `samconfig.toml`, `.deploy-outputs-*` | CI log |
| Secrets in Parameter Store | `/agriprice/<env>/at/{username,api_key,callback_token}`, `/agriprice/<env>/privacy/pepper` are SecureString (standard tier, free). They are created by `scripts/deploy.py`, never by IaC (CloudFormation cannot create SecureStrings) and never logged | SSM console |
| No AWS keys in CI | GitHub OIDC → `agriprice-github-deploy` role, limited to the `main` branch and GitHub environments of this repository | `infra/bootstrap.yaml` |
| Least privilege | one role per function, scoped to exact ARNs, with DynamoDB `LeadingKeys` conditions (table below) | templates, IAM Access Analyzer |
| Encryption at rest | DynamoDB SSE (KMS), S3 SSE-KMS (lake) / SSE-S3, SQS SSE-SQS, SSM SecureString (KMS), encrypted EBS on the NAT | Checkov |
| Encryption in transit | TLS only: API Gateway, CloudFront (`redirect-to-https`/`https-only`), buckets deny `aws:SecureTransport=false`, Lambda egress 443 only | templates |
| Private subnets | all six functions run in private subnets in 2 AZs; only API Gateway and CloudFront are public | `infra/network.yaml` |
| Authentication | Cognito user pool (groups `buyers`, `officers`, `admins`), hosted UI with code + PKCE; API Gateway Cognito authorizer, with the group checked again in code. USSD/SMS callbacks are checked with a constant-time comparison of a secret token, plus an optional source-IP allowlist (`AllowedCidrs`) | `listings_app._require`, `ussd_app._authorised` |
| CloudTrail | multi-region trail with log-file validation, in an encrypted, versioned bucket | `infra/data.yaml` |
| Privacy (DPA 2021) | phone numbers are HMAC-pseudonymised in keys and logs, never returned unmasked by any API, and deleted by SMS `STOP` | `shared/agricommon/privacy.py` |

## Permission justification (every permission)

| Role | Action(s) | Resource / condition | Why |
|---|---|---|---|
| ussd-gateway | dynamodb Get/BatchGet/Put/Update/DeleteItem | table; `LeadingKeys` ∈ FARMER#, SESSION#, RESUME#, PRICE#, SUPPLY#, ALERT# | read prices; persist session, resume point, registration, listing, alert; delete profile on STOP |
| ussd-gateway | events:PutEvents | project event bus | publish `ListingCreated` |
| ussd-gateway | ssm:GetParameter | `/agriprice/<env>/at/*`, `/privacy/pepper` | callback token, SMS credentials, pseudonymisation key |
| listings-api | dynamodb Get/BatchGet/Put/Update/Query | table; `LeadingKeys` ∈ FARMER#, PRICE#, SUPPLY#, DEMAND# | public prices, register farmers, listings, demand, nearby supply |
| listings-api | dynamodb:Query | GSI1; `LeadingKeys` ∈ OWNER# | "my demand" |
| listings-api | dynamodb Get/Put/Update/DeleteItem | idempotency table | Idempotency-Key records |
| listings-api | events:PutEvents | event bus | `ListingCreated`, `DemandPosted` |
| listings-api | s3:PutObject | `lake/inbox/field/*` | officer field price reports |
| listings-api | ssm:GetParameter | `/privacy/pepper` | pseudonymise phone numbers |
| ingestion | dynamodb Get/Put/UpdateItem | table; `LeadingKeys` ∈ PRICE#, SOURCE# | current prices, watermarks |
| ingestion | sqs:SendMessage | `price-normalised` | async path to Alerts |
| ingestion | s3 List / Get/Put/Delete | lake bucket; prefixes seed, raw, inbox, processed, curated, athena-results | read sources, write raw zone, archive inbox; Athena `INSERT INTO` runs with this role, so it writes curated and athena-results |
| ingestion | athena Start/GetQueryExecution | project workgroup | curation query |
| ingestion | glue Get*/CreatePartition/BatchCreatePartition | project database and tables | `INSERT INTO` registers new partitions |
| matching | dynamodb GetItem/Query | table; `LeadingKeys` ∈ SUPPLY#, DEMAND#, FARMER# | read the event's item and counterparties |
| matching / alerts | ssm:GetParameter | `/at/*` | SMS credentials |
| matching / alerts | sqs Receive/Delete/GetQueueAttributes | own queue (added by SAM) | event source mapping |
| alerts | dynamodb:Query | `LeadingKeys` ∈ ALERT# | subscriptions for a changed price |
| alerts | dynamodb Put/DeleteItem | `LeadingKeys` ∈ ALERTLOG# | dedupe and daily cap |
| analytics-api | dynamodb Get/PutItem | `LeadingKeys` ∈ SERIES# | series cache |
| analytics-api | athena Start/Get/GetResults/Stop | project workgroup | nightly aggregation |
| analytics-api | glue Get* (read only) | database, `curated_prices` | query planning |
| analytics-api | s3 Get curated/*, Put athena-results/*, List | lake | read Parquet, write results |
| all functions | VPC ENI management, CloudWatch Logs, X-Ray (`AWSLambdaVPCAccessExecutionRole`, tracing policy added by SAM) | | required by Lambda in a VPC with tracing |
| github-deploy | cloudformation:* | `agriprice-*` stacks only | drive deployments |
| github-deploy | iam:PassRole | `agriprice-cfn-execution` only | CloudFormation acts with the execution role, never the CI identity |
| github-deploy | s3 (artifacts, web, lake/seed), cloudfront:CreateInvalidation, ec2:DescribeImages, lambda:InvokeFunction `agriprice-*`, ssm Get/Put `/agriprice/*` | | upload artifacts, publish site, find the fck-nat AMI, post-deploy pipeline run, create missing secrets |
| cfn-execution | PowerUserAccess + IAM restricted to `role/agriprice-*` | | CloudFormation must create many resource types; IAM is the escalation path, so it is fenced to project-named roles |
| fck-nat instance | ec2:AttachNetworkInterface, ModifyNetworkInterfaceAttribute | the static NAT ENI | self-healing NAT |

## Accepted Checkov findings

Each finding listed in `.checkov.yaml` is a deliberate cost or complexity trade-off:

- **Customer-managed KMS keys** cost $1 per key per month. AWS-managed keys still encrypt the data at rest.
- **WAF** costs from about $5 a month. API Gateway throttling and Cognito cover the pilot instead.
- **S3 and CloudFront access logs:** CloudTrail already records API activity.
- **NAT public IP:** NAT cannot work without one.
- **TLS 1.2 minimum at CloudFront** needs a custom domain and an ACM certificate.
- **SNS encryption:** CloudWatch alarms cannot publish to an SNS topic that uses the `aws/sns` managed key.

Every other check passes (152 checks at the time of writing).
