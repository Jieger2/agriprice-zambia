"""Runtime configuration. Secrets come from SSM Parameter Store SecureString only.

Parameter names (created by `python scripts/deploy.py secrets`, never by IaC):
  /agriprice/<env>/at/username        Africa's Talking username ("sandbox" in sandbox)
  /agriprice/<env>/at/api_key         Africa's Talking API key
  /agriprice/<env>/at/callback_token  shared secret appended to the USSD/SMS callback URL
  /agriprice/<env>/privacy/pepper     HMAC key used to pseudonymise phone numbers
"""

from __future__ import annotations

import os
from functools import lru_cache

ENV = os.environ.get("APP_ENV", "dev")
TABLE_NAME = os.environ.get("TABLE_NAME", "agriprice-dev")
PARAM_PREFIX = os.environ.get("PARAM_PREFIX", f"/agriprice/{ENV}")


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"missing environment variable {name}")
    return val


@lru_cache(maxsize=32)
def _cached(name: str) -> str:
    from aws_lambda_powertools.utilities import parameters

    return parameters.get_parameter(f"{PARAM_PREFIX}/{name}", decrypt=True, max_age=300)


def secret(name: str, default: str | None = None) -> str:
    """Fetch a SecureString parameter (cached). Local/test override: SECRET_<NAME> env var."""
    override = os.environ.get("SECRET_" + name.upper().replace("/", "_"))
    if override is not None:
        return override
    try:
        return _cached(name)
    except Exception:
        if default is not None:
            return default
        raise
