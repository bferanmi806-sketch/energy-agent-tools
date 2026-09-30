"""Fixed-argv executable fixture for adapter tests."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "result"
    if mode == "no-read":
        time.sleep(20)
        return 0
    payload = json.loads(sys.stdin.read() or "{}")
    if mode == "result":
        print(
            json.dumps(
                {
                    "data": {"received": payload, "credential": os.environ.get("FIXTURE_SECRET")},
                    "kind": "calculated",
                    "unit": "kWh",
                    "source": "fixture",
                }
            )
        )
        return 0
    if mode == "echo":
        print(json.dumps(payload))
        return 0
    if mode == "fail":
        print("fixture secret=do-not-leak", file=sys.stderr)
        return 17
    if mode == "invalid":
        print("not-json")
        return 0
    if mode == "huge":
        print(json.dumps({"huge": "x" * 100_000}))
        return 0
    if mode == "sleep":
        time.sleep(float(payload.get("seconds", 5)))
        print(json.dumps({"ok": True}))
        return 0
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
