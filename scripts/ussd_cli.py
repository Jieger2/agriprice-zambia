#!/usr/bin/env python3
"""Interactive USSD simulator - behaves like a feature phone dialling the service.

  python scripts/ussd_cli.py                                   # against scripts/local_server.py
  python scripts/ussd_cli.py --url "https://.../live/ussd?k=TOKEN" --phone 0977123456
"""

from __future__ import annotations

import argparse
import urllib.parse
import urllib.request
import uuid


def post(url: str, session: str, phone: str, text: str) -> str:
    data = urllib.parse.urlencode({"sessionId": session, "serviceCode": "*384*1#", "phoneNumber": phone, "text": text}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:  # noqa: S310
        return r.read().decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080/ussd")
    ap.add_argument("--phone", default="0977123456")
    a = ap.parse_args()
    while True:
        session, inputs = uuid.uuid4().hex, []
        print("\n--- dialling *384*1# ---")
        while True:
            reply = post(a.url, session, a.phone, "*".join(inputs))
            kind, body = reply[:3], reply[4:]
            print("+" + "-" * 30 + "+")
            for line in body.split("\n"):
                print(f"| {line:<29}|")
            print("+" + "-" * 30 + f"+  ({len(reply)}/182 chars)")
            if kind == "END":
                break
            choice = input("> ").strip()
            if choice.lower() in ("q", "quit"):
                return
            inputs.append(choice)
        if input("Dial again? [Y/n] ").strip().lower() == "n":
            return


if __name__ == "__main__":
    main()
