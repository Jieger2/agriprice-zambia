#!/usr/bin/env python3
"""Cross-platform deployment driver (Windows, macOS, Linux, GitHub Actions).

Usage:
  python scripts/deploy.py bootstrap                 # once per AWS account (OIDC role, artifacts bucket, budget)
  python scripts/deploy.py deploy --env dev          # every stack, in dependency order
  python scripts/deploy.py deploy --env dev --only ussd_gateway,listings_api
  python scripts/deploy.py secrets --env dev         # create/rotate SecureString parameters
  python scripts/deploy.py outputs --env dev         # print URLs (USSD callback, web app, dashboard)
  python scripts/deploy.py run-pipeline --env dev    # run ingestion now, then the analytics refresh
  python scripts/deploy.py demo-rollback --env prod  # canary deploy of a faulty version (see docs/demo-script.md)
  python scripts/deploy.py add-user --env dev --email you@example.com --role admins   # invite the first admin
  python scripts/deploy.py doctor                    # check tools, credentials and Region before deploying
  python scripts/deploy.py diagnose --env dev        # stack status, failure reasons, NAT launch attempts
  python scripts/deploy.py destroy --env dev

Environment variables (optional):
  AWS_REGION       default eu-north-1
  AT_USERNAME      Africa's Talking username (default "sandbox")
  AT_API_KEY       Africa's Talking API key (sandbox or live)
  SMS_MODE         log | outbox | sandbox | live (default: sandbox if AT_API_KEY is stored, else outbox)
  WFP_CSV_URL      HTTPS link to the WFP Zambia food-price CSV on HDX (adds a live source)
  ALERT_EMAIL      default agrizambia5@gmail.com
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1"
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "agrizambia5@gmail.com")
SERVICES = ["ussd_gateway", "listings_api", "ingestion", "matching", "alerts", "analytics_api"]
ORDER = ["network", "data", *SERVICES, "web", "observability"]


def log(msg: str) -> None:
    print(f"[agriprice] {msg}", flush=True)


def stack_name(env: str, part: str) -> str:
    return f"agriprice-{env}-{part.replace('_', '-')}"


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def outputs(name: str, region: str = REGION) -> dict[str, str]:
    try:
        stack = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=name)["Stacks"][0]
    except ClientError:
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def sam(*args: str) -> None:
    exe = shutil.which("sam") or shutil.which("sam.cmd")
    if not exe:
        sys.exit("AWS SAM CLI not found. Install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html")
    cmd = [exe, *args]
    log("$ sam " + " ".join(args))
    subprocess.run(cmd, check=True, cwd=ROOT)


def bootstrap_outputs() -> dict[str, str]:
    out = outputs("agriprice-bootstrap")
    if not out:
        sys.exit("Run `python scripts/deploy.py bootstrap` first.")
    return out


def sam_deploy(template: str, name: str, params: dict[str, str], boot: dict[str, str], iam_named: bool = False) -> None:
    overrides = [f"{k}={v}" for k, v in params.items()]
    args = [
        "deploy", "--template-file", template, "--stack-name", name, "--region", REGION,
        "--s3-bucket", boot["ArtifactsBucketName"], "--s3-prefix", name,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", *(["CAPABILITY_NAMED_IAM"] if iam_named else []),
        "--role-arn", boot["CfnExecutionRoleArn"],
        "--no-fail-on-empty-changeset", "--no-confirm-changeset",
        "--tags", "project=agriprice", f"stack={name}",
    ]
    if overrides:
        args += ["--parameter-overrides", *overrides]
    sam(*args)


# ---------------------------------------------------------------- secrets
def param_exists(ssm, name: str) -> bool:
    try:
        ssm.get_parameter(Name=name)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            return False
        raise


def ensure_secrets(env: str, rotate: bool = False) -> bool:
    """Create missing SecureString parameters. Returns True if an AT API key is stored.

    The privacy pepper is never rotated automatically: changing it would orphan every
    farmer record (their keys are derived from it). --rotate only renews the callback token.
    """
    ssm = boto3.client("ssm", region_name=REGION)
    prefix = f"/agriprice/{env}"

    def put(name: str, value: str) -> None:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True, Tier="Standard")
        log(f"stored {name}")

    if not param_exists(ssm, f"{prefix}/privacy/pepper"):
        put(f"{prefix}/privacy/pepper", secrets.token_urlsafe(32))
    if rotate or not param_exists(ssm, f"{prefix}/at/callback_token"):
        put(f"{prefix}/at/callback_token", secrets.token_urlsafe(24))
    if "AT_USERNAME" in os.environ or not param_exists(ssm, f"{prefix}/at/username"):
        put(f"{prefix}/at/username", os.environ.get("AT_USERNAME", "sandbox"))
    if os.environ.get("AT_API_KEY"):
        put(f"{prefix}/at/api_key", os.environ["AT_API_KEY"])
    return param_exists(ssm, f"{prefix}/at/api_key")


# ---------------------------------------------------------------- deploy
def clear_failed_network(env: str) -> None:
    """A network stack that failed or got stuck creating must be deleted before it can be created again."""
    name = stack_name(env, "network")
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
    except ClientError:
        return
    if status in ("DELETE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"):
        log(f"{name} is {status}: waiting for AWS to finish (a few minutes)...")
        waiter = "stack_delete_complete" if status == "DELETE_IN_PROGRESS" else "stack_rollback_complete"
        try:
            cfn.get_waiter(waiter).wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        except Exception as exc:  # noqa: BLE001 - the waiter raises once the stack is gone; re-check below
            log(f"  waiter ended: {type(exc).__name__}")
        try:
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except ClientError:
            log(f"{name} deleted")
            return
    if status in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED", "CREATE_IN_PROGRESS", "DELETE_FAILED"):
        log(f"{name} is {status}: deleting it so it can be created again (takes a few minutes)...")
        cfn.delete_stack(StackName=name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        log(f"{name} deleted")


def sources_json() -> str:
    src = [{"id": "seed", "type": "wfp_csv_s3", "key": "seed/zambia_sample_prices.csv"}, {"id": "field", "type": "s3_inbox", "prefix": "inbox/field/"}]
    if os.environ.get("WFP_CSV_URL"):
        src.insert(0, {"id": "wfp_hdx", "type": "wfp_csv", "url": os.environ["WFP_CSV_URL"]})
    # base64 without padding: survives the quote-stripping of sam.cmd on Windows and has no '='
    return "b64:" + base64.urlsafe_b64encode(json.dumps(src, separators=(",", ":")).encode()).decode().rstrip("=")


def upload_seed(env: str) -> None:
    bucket = outputs(stack_name(env, "data")).get("LakeBucketName")
    path = ROOT / "data" / "zambia_sample_prices.csv"
    if bucket and path.exists():
        boto3.client("s3", region_name=REGION).upload_file(str(path), bucket, "seed/zambia_sample_prices.csv")
        log(f"uploaded seed data to s3://{bucket}/seed/")


def publish_web(env: str) -> None:
    web = outputs(stack_name(env, "web"))
    data = outputs(stack_name(env, "data"))
    if not web:
        return
    cfg = {"env": env, "region": REGION, "cognitoDomain": data.get("UserPoolDomain", ""), "clientId": web.get("WebClientId", "")}
    s3 = boto3.client("s3", region_name=REGION)
    bucket = web["WebBucketName"]
    for f in (ROOT / "web").rglob("*"):
        if f.is_file() and f.name not in ("config.js", "simulator.html"):  # the phone simulator is local-only
            key = f.relative_to(ROOT / "web").as_posix()
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if f.suffix == ".webmanifest":
                ctype = "application/manifest+json"
            cache = "no-cache" if f.name in ("index.html", "sw.js") else "public, max-age=86400"
            s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": cache})
    s3.put_object(Bucket=bucket, Key="config.js", Body=f"window.AGRI={json.dumps(cfg)};".encode(), ContentType="application/javascript", CacheControl="no-cache")
    boto3.client("cloudfront").create_invalidation(
        DistributionId=web["DistributionId"],
        InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": str(time.time())},
    )
    log(f"web app published: {web['SiteUrl']}")


def deploy(env: str, only: list[str] | None, sms_mode: str | None, chaos: str = "0", deployment_type: str | None = None) -> None:
    boot = bootstrap_outputs()
    has_key = ensure_secrets(env)
    sms_mode = sms_mode or os.environ.get("SMS_MODE") or ("sandbox" if has_key else "outbox")
    dtype = deployment_type or ("Canary10Percent5Minutes" if env == "prod" else "AllAtOnce")
    parts = only or ORDER
    for part in ORDER:
        if part not in parts:
            continue
        name = stack_name(env, part)
        if part == "network":
            clear_failed_network(env)
            params = {"Env": env}
            if os.environ.get("NAT_INSTANCE_TYPE"):
                params["NatInstanceType"] = os.environ["NAT_INSTANCE_TYPE"]
            sam_deploy("infra/network.yaml", name, params, boot)
        elif part == "data":
            first_env = not outputs(stack_name("dev" if env == "prod" else "prod", "data"))
            sam_deploy("infra/data.yaml", name, {
                "Env": env, "AlertEmail": ALERT_EMAIL,
                "CreateTrail": "true" if first_env else "false",
                "CreateApiGatewayAccount": "true" if first_env else "false",
            }, boot)
            upload_seed(env)
        elif part in SERVICES:
            build_dir = f".aws-sam/build/{part}"
            sam("build", "--template-file", f"services/{part}/template.yaml", "--build-dir", build_dir, "--cached", "--cache-dir", f".aws-sam/cache/{part}")
            params = {"Env": env, "DeploymentType": dtype}
            if part in ("ussd_gateway", "matching", "alerts"):
                params["SmsMode"] = sms_mode
            if part in ("ussd_gateway", "listings_api", "analytics_api"):
                params["ChaosErrorRate"] = chaos
            if part == "ingestion":
                params["SourcesJson"] = sources_json()
            sam_deploy(f"{build_dir}/template.yaml", name, params, boot)
        elif part == "web":
            sam_deploy("infra/web.yaml", name, {"Env": env}, boot)
            publish_web(env)
        elif part == "observability":
            sam_deploy("infra/observability.yaml", name, {"Env": env}, boot)
    show_outputs(env)


def show_outputs(env: str) -> dict:
    ssm = boto3.client("ssm", region_name=REGION)
    try:
        token = ssm.get_parameter(Name=f"/agriprice/{env}/at/callback_token", WithDecryption=True)["Parameter"]["Value"]
    except ClientError:
        token = "<run secrets>"
    ussd = outputs(stack_name(env, "ussd_gateway"))
    info = {
        "web_app": outputs(stack_name(env, "web")).get("SiteUrl"),
        "ussd_callback": f"{ussd.get('UssdCallbackUrl')}?k={token}" if ussd else None,
        "sms_callback": f"{ussd.get('SmsCallbackUrl')}?k={token}" if ussd else None,
        "listings_api": outputs(stack_name(env, "listings_api")).get("ApiUrl"),
        "dashboard": outputs(stack_name(env, "observability")).get("DashboardUrl"),
    }
    out_file = ROOT / f".deploy-outputs-{env}.json"
    out_file.write_text(json.dumps(info, indent=2))
    log(f"outputs written to {out_file.name} (git-ignored: it contains the callback token)")
    for k, v in info.items():
        shown = v if k not in ("ussd_callback", "sms_callback") or not v else v.split("?")[0] + "?k=***"
        print(f"  {k:15} {shown or 'not deployed'}")
    if not info["web_app"]:
        log(f"Nothing is deployed in {REGION} for env '{env}' yet: run  python scripts/deploy.py deploy --env {env}  and wait for it to finish.")
    return info


def run_pipeline(env: str) -> None:
    lam = boto3.client("lambda", region_name=REGION)
    for fn, payload in ((f"agriprice-{env}-ingestion", {"lookback_days": 3650}), (f"agriprice-{env}-analytics-api", {"action": "refresh"})):
        log(f"invoking {fn}")
        resp = lam.invoke(FunctionName=f"{fn}:live", Payload=json.dumps(payload).encode())
        body = resp["Payload"].read().decode()
        log(f"  -> {resp.get('FunctionError', 'OK')} {body[:300]}")
        if fn.endswith("ingestion"):
            log("waiting 45 s for the Athena curation query to finish...")
            time.sleep(45)


def bootstrap() -> None:
    region = REGION
    exe_ok = shutil.which("sam") or shutil.which("sam.cmd")
    if not exe_ok:
        sys.exit("Install AWS SAM CLI first.")
    iam = boto3.client("iam")
    github = True
    try:
        has_oidc = any(p["Arn"].endswith("token.actions.githubusercontent.com") for p in iam.list_open_id_connect_providers()["OpenIDConnectProviderList"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
            raise
        github, has_oidc = False, False
        log("NOTE: this account blocks IAM OpenID Connect (AWS-managed service control policy).")
        log("      Skipping the GitHub Actions deploy role: deploy from this laptop with deploy.py instead.")
    subprocess.run([exe_ok, "deploy", "--template-file", "infra/bootstrap.yaml", "--stack-name", "agriprice-bootstrap", "--region", region,
                    "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
                    "--parameter-overrides", f"CreateOidcProvider={'false' if has_oidc else 'true'}", f"CreateGitHubRole={'true' if github else 'false'}"],
                   check=True, cwd=ROOT)
    # AWS Budgets is global, so the budget stack lives in the deployment Region (some accounts may only use one Region).
    budget = subprocess.run([exe_ok, "deploy", "--template-file", "infra/budget.yaml", "--stack-name", "agriprice-budget", "--region", region,
                             "--no-fail-on-empty-changeset", "--parameter-overrides", f"AlertEmail={ALERT_EMAIL}"], cwd=ROOT)
    if budget.returncode != 0:
        log("WARNING: the $20 budget could not be created here. Set a spend limit or budget in the AWS console instead; deployment can continue.")
    out = outputs("agriprice-bootstrap", region)
    log("Bootstrap complete. Next: python scripts/deploy.py deploy --env dev")
    if out.get("GitHubDeployRoleArn"):
        log("For GitHub deployments, add this as repository variable AWS_DEPLOY_ROLE_ARN:")
        print("  " + out["GitHubDeployRoleArn"])
        log("Also add repository variable AWS_REGION = " + region)


def destroy(env: str) -> None:
    cfn = boto3.client("cloudformation", region_name=REGION)
    for part in reversed(ORDER):
        name = stack_name(env, part)
        if not outputs(name) and part not in ("observability",):
            try:
                cfn.describe_stacks(StackName=name)
            except ClientError:
                continue
        log(f"deleting {name}")
        if part == "web":
            bucket = outputs(name).get("WebBucketName")
            if bucket:
                b = boto3.resource("s3", region_name=REGION).Bucket(bucket)
                b.objects.all().delete()
        cfn.delete_stack(StackName=name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=name)
    log("Retained on purpose (delete manually if you really want to): DynamoDB table, lake and trail buckets.")


def doctor() -> None:
    """Check everything a first deployment needs, with a fix for each problem."""
    ok = True
    if sys.version_info[:2] not in ((3, 12), (3, 13)):
        log(f"WARN Python {sys.version.split()[0]}: use 3.12 (Lambda runtime) to avoid build differences")
    for tool, hint in (("sam", "install the AWS SAM CLI: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"),
                       ("aws", "install the AWS CLI v2 and run: aws configure")):
        if shutil.which(tool):
            log(f"OK   {tool} found")
        else:
            ok = False
            log(f"FAIL {tool} not found - {hint}")
    try:
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
        log(f"OK   AWS account {ident['Account']} as {ident['Arn'].split(':')[-1]}")
        if ident["Arn"].endswith(":root"):
            log("WARN signed in with root credentials: create an IAM user with AdministratorAccess and use its access key instead")
    except Exception as e:  # noqa: BLE001 - report any credential problem plainly
        ok = False
        log(f"FAIL no working AWS credentials ({type(e).__name__}) - run: aws configure  (Region: {REGION})")
    log(f"OK   Region {REGION}" if REGION else "FAIL no Region set")
    log("Ready to deploy." if ok else "Fix the FAIL lines above, then run doctor again.")
    if not ok:
        sys.exit(1)


def diagnose(env: str) -> None:
    """Everything needed to troubleshoot a deploy without the AWS console."""
    cfn = boto3.client("cloudformation", region_name=REGION)
    log(f"Region {REGION}, account {account_id()}")
    names = ["agriprice-bootstrap", "agriprice-budget", *(stack_name(env, p) for p in ORDER)]
    for name in names:
        try:
            st = cfn.describe_stacks(StackName=name)["Stacks"][0]
        except ClientError:
            print(f"  {name:38} not created")
            continue
        status = st["StackStatus"]
        print(f"  {name:38} {status}  {st.get('StackStatusReason', '')}")
        if not status.endswith("_COMPLETE") or "ROLLBACK" in status or "FAILED" in status:
            events = cfn.describe_stack_events(StackName=name)["StackEvents"][:40]
            for ev in events:
                if "FAILED" in ev["ResourceStatus"] or "IN_PROGRESS" in ev["ResourceStatus"] and ev.get("ResourceStatusReason"):
                    print(f"      {ev['Timestamp']:%H:%M:%S} {ev['LogicalResourceId']:24} {ev['ResourceStatus']:22} {ev.get('ResourceStatusReason', '')[:300]}")
    asg = boto3.client("autoscaling", region_name=REGION)
    groups = [g for g in asg.describe_auto_scaling_groups()["AutoScalingGroups"] if g["AutoScalingGroupName"].startswith(f"agriprice-{env}-network")]
    for g in groups:
        print(f"  NAT group {g['AutoScalingGroupName']}: {len(g['Instances'])} instance(s) {[i['LifecycleState'] for i in g['Instances']]}")
        for a in asg.describe_scaling_activities(AutoScalingGroupName=g["AutoScalingGroupName"], MaxRecords=5)["Activities"]:
            print(f"      {a['StartTime']:%H:%M:%S} {a['StatusCode']:12} {a.get('StatusMessage', '')[:400]}")
    if not groups:
        print("  NAT group: none found")


def add_user(env: str, email: str, role: str) -> None:
    """Invite a web user (Cognito emails a temporary password) and put them in a role group."""
    pool = outputs(stack_name(env, "data")).get("UserPoolId")
    if not pool:
        sys.exit("The data stack is not deployed yet: run deploy first.")
    idp = boto3.client("cognito-idp", region_name=REGION)
    try:
        idp.admin_create_user(UserPoolId=pool, Username=email, UserAttributes=[
            {"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}], DesiredDeliveryMediums=["EMAIL"])
        log(f"invited {email}: Cognito has emailed a temporary password")
    except idp.exceptions.UsernameExistsException:
        log(f"{email} already exists")
    idp.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=role)
    log(f"{email} is now in '{role}'. Sign in at the web URL from: python scripts/deploy.py outputs --env {env}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["doctor", "diagnose", "bootstrap", "deploy", "secrets", "outputs", "run-pipeline", "add-user", "demo-rollback", "destroy"])
    ap.add_argument("--env", default="dev", choices=["dev", "prod"])
    ap.add_argument("--only", help="comma-separated parts: " + ",".join(ORDER))
    ap.add_argument("--sms-mode", choices=["log", "outbox", "sandbox", "live"])
    ap.add_argument("--rotate", action="store_true", help="secrets: regenerate pepper and callback token")
    ap.add_argument("--rate", default="0.5", help="demo-rollback: fraction of requests that fail")
    ap.add_argument("--email", help="add-user: the person's email address")
    ap.add_argument("--role", default="admins", choices=["admins", "officers", "buyers"], help="add-user: role group")
    a = ap.parse_args()
    if a.command == "doctor":
        doctor()
    elif a.command == "diagnose":
        diagnose(a.env)
    elif a.command == "add-user":
        if not a.email:
            ap.error("add-user needs --email")
        add_user(a.env, a.email, a.role)
    elif a.command == "bootstrap":
        bootstrap()
    elif a.command == "deploy":
        deploy(a.env, a.only.split(",") if a.only else None, a.sms_mode)
    elif a.command == "secrets":
        ensure_secrets(a.env, rotate=a.rotate)
    elif a.command == "outputs":
        show_outputs(a.env)
    elif a.command == "run-pipeline":
        run_pipeline(a.env)
    elif a.command == "demo-rollback":
        log("Deploying a deliberately faulty USSD gateway version with a 10% canary.")
        log("While it deploys, generate traffic: k6 run load/ussd_load.js (see docs/demo-script.md).")
        deploy(a.env, ["ussd_gateway"], a.sms_mode, chaos=a.rate, deployment_type="Canary10Percent5Minutes")
    elif a.command == "destroy":
        destroy(a.env)


if __name__ == "__main__":
    main()
