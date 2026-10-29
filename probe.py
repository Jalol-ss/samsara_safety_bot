"""
probe.py — Samsara access check
================================
Run this once before deploying. It confirms that your SAMSARA_API_TOKEN
has the scopes required by the Safety Bot:

  1) Read Safety Events & Scores   → GET /fleet/safety-events
  2) Read Webhooks (write optional)→ GET /webhooks
  3) Read Vehicle Stats (optional, for driver/vehicle labels)
                                   → GET /fleet/vehicles

Usage:
    python probe.py

Output: a checklist with ✅ / ❌ and a short hint for each failure.
Exit code 0 = all required passed; 1 = at least one required failed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(".env")

from services.samsara import SamsaraClient, SamsaraError  # noqa: E402


def _p(ok: bool, label: str, detail: str = "") -> None:
    tag = "✅" if ok else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))


async def _check_safety_events(c: SamsaraClient) -> bool:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    try:
        page = await c.list_safety_events(start_time=start, end_time=end, limit=1)
        rows = page.get("data", [])
        _p(True, "Safety Events (/fleet/safety-events)",
           f"returned {len(rows)} row(s) in the last hour")
        return True
    except SamsaraError as e:
        _p(False, "Safety Events (/fleet/safety-events)", str(e))
        if "403" in str(e):
            print("     Fix: in Samsara dashboard → Settings → API Tokens, "
                  "edit this token and enable 'Read Safety Events & Scores'.")
        return False


async def _check_webhooks(c: SamsaraClient) -> bool:
    try:
        hooks = await c.list_webhooks()
        _p(True, "Webhooks (/webhooks)", f"{len(hooks)} already registered")
        return True
    except SamsaraError as e:
        _p(False, "Webhooks (/webhooks)", str(e))
        if "403" in str(e):
            print("     Fix: enable 'Read Webhooks' and 'Write Webhooks' scopes "
                  "on your API token.")
        return False


async def _check_vehicles(c: SamsaraClient) -> bool:
    """Not strictly required — only used for prettier vehicle/driver labels."""
    try:
        data = await c._request("GET", "/fleet/vehicles", params={"limit": 1})
        rows = data.get("data", [])
        _p(True, "Vehicles (/fleet/vehicles)",
           f"accessible ({len(rows)} sample row)")
        return True
    except SamsaraError as e:
        _p(False, "Vehicles (/fleet/vehicles)", str(e))
        print("     (Optional) Fix: enable 'Read Vehicle Stats' for nicer "
              "unit labels. Bot will still work without this.")
        return False


async def main() -> int:
    token = os.getenv("SAMSARA_API_TOKEN", "")
    if not token:
        print("❌ SAMSARA_API_TOKEN is not set in .env — nothing to probe.")
        return 1

    print(f"Probing Samsara with token …{token[-6:]} (last 6 chars shown)\n")

    c = SamsaraClient()
    try:
        ok_events = await _check_safety_events(c)
        ok_hooks  = await _check_webhooks(c)
        _ok_veh   = await _check_vehicles(c)   # advisory only
    finally:
        await c.close()

    print()
    required_ok = ok_events and ok_hooks
    if required_ok:
        print("All required checks passed. You can deploy.")
        return 0
    print("One or more REQUIRED checks failed. Fix the scopes above and re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
