#!/usr/bin/env python3
"""Read the mock SMS outbox of a deployed environment (SMS_MODE=outbox, the default until an
Africa's Talking key is stored).

  python scripts/sms_outbox.py --env dev --phone 0977123456
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    ap.add_argument("--phone", required=True)
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    os.environ.setdefault("AWS_REGION", "eu-north-1")
    os.environ["TABLE_NAME"] = f"agriprice-{a.env}"
    from agricommon import privacy, repo

    for m in repo.outbox(privacy.normalise_phone(a.phone), a.limit):
        print(f"{datetime.fromtimestamp(m['ts']):%Y-%m-%d %H:%M}  {m['body']}")


if __name__ == "__main__":
    main()
