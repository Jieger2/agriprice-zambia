"""Price-source configuration (FR6 + FR10).

Built-in sources come from the deployment (SOURCES_JSON). Administrators add,
disable or re-point sources from the Admin page; their list is stored as
CONFIG#sources and merged over the built-ins by `id` at every ingestion run.
"""

from __future__ import annotations

import json
import os
import re

TYPES = ("wfp_csv", "wfp_csv_s3", "s3_inbox")
DEFAULTS = [
    {"id": "seed", "type": "wfp_csv_s3", "key": "seed/zambia_sample_prices.csv", "label": "Synthetic demo prices (WFP CSV format)"},
    {"id": "field", "type": "s3_inbox", "prefix": "inbox/field/", "label": "Extension officer field reports"},
]
_ID = re.compile(r"^[a-z0-9_-]{2,30}$")


def builtin() -> list[dict]:
    raw = os.environ.get("SOURCES_JSON")
    return json.loads(raw) if raw else [dict(s) for s in DEFAULTS]


def validate(doc: dict) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("sources", []), list):
        raise ValueError("expected {\"sources\": [...]}")
    out = []
    seen = set()
    for s in doc.get("sources", []):
        sid = str(s.get("id", "")).strip().lower()
        if not _ID.match(sid):
            raise ValueError(f"source id '{sid}': use 2-30 lowercase letters, digits, - or _")
        if sid in seen:
            raise ValueError(f"duplicate source id '{sid}'")
        seen.add(sid)
        enabled = bool(s.get("enabled", True))
        kind = s.get("type")
        if kind is None and not enabled:  # disabling a built-in only needs the id
            out.append({"id": sid, "enabled": False})
            continue
        if kind not in TYPES:
            raise ValueError(f"source {sid}: type must be one of {', '.join(TYPES)}")
        row = {"id": sid, "type": kind, "enabled": enabled, "label": str(s.get("label", ""))[:80]}
        if kind == "wfp_csv":
            url = str(s.get("url", "")).strip()
            if not url.startswith("https://") or len(url) > 500:
                raise ValueError(f"source {sid}: url must start with https://")
            row["url"] = url
        elif kind == "wfp_csv_s3":
            key = str(s.get("key", "")).strip()
            if not key.startswith("seed/"):
                raise ValueError(f"source {sid}: S3 key must be under seed/")
            row["key"] = key
        else:
            prefix = str(s.get("prefix", "inbox/field/")).strip()
            if not prefix.startswith("inbox/"):
                raise ValueError(f"source {sid}: inbox prefix must start with inbox/")
            row["prefix"] = prefix
        out.append(row)
    return {"sources": out}


def effective(admin_doc: dict | None) -> list[dict]:
    """Built-ins overridden by admin entries with the same id; disabled entries removed."""
    merged = {s["id"]: {**s, "enabled": s.get("enabled", True)} for s in builtin()}
    for s in (admin_doc or {}).get("sources", []):
        merged[s["id"]] = {**merged.get(s["id"], {}), **s}
    return [s for s in merged.values() if s.get("enabled", True) and s.get("type")]
