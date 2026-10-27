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
  python scripts/deploy.py destroy --env dev

Environment variables (optional):
  AWS_REGION       default eu-north-1
  AT_USERNAME      Africa's Talking username (default "sandbox")
  AT_API_KEY       Africa's Talking API key (sandbox or live)
  SMS_MODE         log | sandbox | live (default: sandbox if AT_API_KEY is stored, else log)
  WFP_CSV_URL      HTTPS link to the WFP Zambia food-price CSV on HDX (adds a live source)
  ALERT_EMAIL      default agrizambia5@gmail.com
"""

from __future__ import annotations

import argparse
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
FCK_NAT_OWNER = "568608671756"
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
def fck_nat_ami() -> str:
    ec2 = boto3.client("ec2", region_name=REGION)
    images = ec2.describe_images(Owners=[FCK_NAT_OWNER], Filters=[{"Name": "name", "Values": ["fck-nat-al2023-*-arm64-ebs"]}, {"Name": "architecture", "Values": ["arm64"]}])["Images"]
    if not images:
        log("WARNING: no fck-nat AMI in this Region - deploying without NAT (ingestion from the internet and SMS will not work)")
        return ""
    return sorted(images, key=lambda i: i["CreationDate"])[-1]["ImageId"]


def sources_json() -> str:
    src = [{"id": "seed", "type": "wfp_csv_s3", "key": "seed/zambia_sample_prices.csv"}, {"id": "field", "type": "s3_inbox", "prefix": "inbox/field/"}]
    if os.environ.get("WFP_CSV_URL"):
        src.insert(0, {"id": "wfp_hdx", "type": "wfp_csv", "url": os.environ["WFP_CSV_URL"]})
    return json.dumps(src, separators=(",", ":"))


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
        if f.is_file() and f.name != "config.js":
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
    sms_mode = sms_mode or os.environ.get("SMS_MODE") or ("sandbox" if has_key else "log")
    dtype = deployment_type or ("Canary10Percent5Minutes" if env == "prod" else "AllAtOnce")
    parts = only or ORDER
    for part in ORDER:
        if part not in parts:
            continue
        name = stack_name(env, part)
        if part == "network":
            sam_deploy("infra/network.yaml", name, {"Env": env, "FckNatAmiId": fck_nat_ami()}, boot)
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
        print(f"  {k:15} {v if k not in ('ussd_callback', 'sms_callback') else (v or '').split('?')[0] + '?k=***'}")
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
    has_oidc = any(p["Arn"].endswith("token.actions.githubusercontent.com") for p in iam.list_open_id_connect_providers()["OpenIDConnectProviderList"])
    subprocess.run([exe_ok, "deploy", "--template-file", "infra/bootstrap.yaml", "--stack-name", "agriprice-bootstrap", "--region", region,
                    "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
                    "--parameter-overrides", f"CreateOidcProvider={'false' if has_oidc else 'true'}"], check=True, cwd=ROOT)
    subprocess.run([exe_ok, "deploy", "--template-file", "infra/budget.yaml", "--stack-name", "agriprice-budget", "--region", "us-east-1",
                    "--no-fail-on-empty-changeset", "--parameter-overrides", f"AlertEmail={ALERT_EMAIL}"], check=True, cwd=ROOT)
    out = outputs("agriprice-bootstrap", region)
    log("Bootstrap complete. Add this as a GitHub repository variable named AWS_DEPLOY_ROLE_ARN:")
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["bootstrap", "deploy", "secrets", "outputs", "run-pipeline", "demo-rollback", "destroy"])
    ap.add_argument("--env", default="dev", choices=["dev", "prod"])
    ap.add_argument("--only", help="comma-separated parts: " + ",".join(ORDER))
    ap.add_argument("--sms-mode", choices=["log", "sandbox", "live"])
    ap.add_argument("--rotate", action="store_true", help="secrets: regenerate pepper and callback token")
    ap.add_argument("--rate", default="0.5", help="demo-rollback: fraction of requests that fail")
    a = ap.parse_args()
    if a.command == "bootstrap":
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
