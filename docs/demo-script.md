# Demonstration script (for the marker)

## 0. Local walkthrough (no AWS)
Run `python scripts/local_server.py`, then open http://localhost:8080 and http://localhost:8080/simulator.html side by side.
1. **Simulator, phone +260977123456:** register with `5`, `2`, `1` and a name. Then sell: `2`, `1` (maize), `2` (50 kg bags), `10`, `1` (available now), `1` (confirm).
2. **Web app, signed in as Buyer:** on Buy, post demand for maize near Kitwe within 80 km. Within a couple of seconds, both phones receive a match SMS. Open *My demand* to see the matched seller.
3. **Simulator:** set an alert with `3`, `1`, `1` (above), `350`.
4. **Signed in as Extension officer:** submit a field price of maize at Ndola, K400 per 50 kg.
5. **Signed in as Administrator:** on Price sources, click **Run ingestion now**. The price changes, Alerts sends the farmer an SMS, and Trends refreshes.
6. **Admin:** add a market (for example Nakonde, MUC, -9.33, 32.75) and a crop. Both appear in every menu immediately. On Users & roles, invite a user and tick their roles.

## 1. Clean-account deployment (100% IaC)
```bash
python scripts/deploy.py bootstrap
python scripts/deploy.py deploy --env dev
python scripts/deploy.py run-pipeline --env dev
```
Show the CloudFormation console: bootstrap, network, data, six service stacks, web and observability.

## 2. Core journey
Use the Africa's Talking simulator, or `python scripts/ussd_cli.py --url "<ussd_callback from outputs>"`. Register (5 → province → market → name), check prices (1 → 1), then list a crop (2 → crop → unit → quantity → available-from → 1).

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
