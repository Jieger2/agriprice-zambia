# Demonstration script (for the marker)

## 1. Clean-account deployment (100% IaC)
```bash
python scripts/deploy.py bootstrap
python scripts/deploy.py deploy --env dev
python scripts/deploy.py run-pipeline --env dev
```
Show the CloudFormation console: bootstrap, network, data, six service stacks, web and observability.

## 2. Core journey
Use the Africa's Talking simulator, or `python scripts/ussd_cli.py --url "<ussd_callback from outputs>"`. Register (5 → province → market), check prices (1 → 1), then list a crop (2 → 1 → 500 → 1).

## 3. Asynchronous path and tracing
After listing a crop, open X-Ray → Service map: API Gateway → ussd-gateway → EventBridge → SQS → matching. Copy the `correlation_id` from the logs into the dashboard's *Trace one request* widget to see every service that handled it.

## 4. Auto-scaling (demonstrated)
```bash
k6 run -e OUTPUTS=.deploy-outputs-dev.json load/prices_load.js
```
On the dashboard widget *SATURATION – DynamoDB capacity*, provisioned RCU climbs from 2 towards ~30 as consumed capacity exceeds 70%. Screenshot it, along with Application Auto Scaling → Activity history for the table.

## 5. Automated rollback (demonstrated)
```bash
python scripts/deploy.py deploy --env prod          # healthy baseline
python scripts/deploy.py demo-rollback --env prod   # deploys a version where 50% of requests fail
k6 run -e OUTPUTS=.deploy-outputs-prod.json load/ussd_load.js   # in a second terminal, while the canary runs
```
CodeDeploy shifts 10% of traffic and the `ussd-gateway-errors` alarm fires. CodeDeploy rolls back, and CloudFormation rolls the stack back to the previous parameters. Show the CodeDeploy deployment (*Stopped – rolled back*), the alarm history, and that the `live` alias points at the previous version.

## 6. Security evidence
CI run: gitleaks, Checkov, coverage report. The CloudTrail trail. IAM Access Analyzer. `docs/security.md` covers the justification of every permission.

## 7. Cost
Cost Explorer grouped by service and by tag `stack`, alongside `docs/cost.md`.
