"""
services/samsara.py
===================
Thin async Samsara client scoped to what the Safety Bot needs:

  * list_safety_events()        — poll /fleet/safety-events (fallback mode)
  * list_webhooks() / create_webhook() / delete_webhook()
                                 — manage the push integration
  * request_media() / poll_media() — pull dashcam clip when the event
                                     payload doesn't include a mediaUrl

The rest of the Samsara surface (fuel, odo, trailers…) lives in the PM
bot's `services/real_samsara.py`. If you ever outgrow the duplication,
promote both files into a shared package — but for now keeping them
separate means neither bot can accidentally break the other.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from config import SAMSARA_API_TOKEN, SAMSARA_BASE_URL

logger = logging.getLogger(__name__)


class SamsaraError(RuntimeError):
    """Raised when Samsara returns a non-2xx and we can't recover."""


def _iso_z(ts: datetime) -> str:
    """Samsara wants ISO-8601 Zulu, e.g. 2026-10-29T12:34:56Z."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SamsaraClient:
    """Reusable async client. Holds a single aiohttp.ClientSession on
    the first call, reuses it for the lifetime of the process."""

    def __init__(self, token: Optional[str] = None,
                 base_url: Optional[str] = None) -> None:
        self._token = (token or SAMSARA_API_TOKEN or "").strip()
        self._base = (base_url or SAMSARA_BASE_URL).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    # ---------- plumbing -------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept":        "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=45),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *,
                       params: Optional[Dict[str, Any]] = None,
                       json_body: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
        if not self._token:
            raise SamsaraError("SAMSARA_API_TOKEN is not set")

        url = f"{self._base}{path}"
        session = await self._get_session()
        async with session.request(method, url, params=params,
                                   json=json_body) as r:
            text = await r.text()
            if r.status == 401:
                raise SamsaraError(
                    "401 Unauthorized — SAMSARA_API_TOKEN rejected."
                )
            if r.status == 403:
                raise SamsaraError(
                    f"403 Forbidden on {path} — token lacks required scope. "
                    "In Samsara dashboard, edit the API token and enable "
                    "'Read Safety Events & Scores' (Safety & Cameras group)."
                )
            if r.status >= 400:
                raise SamsaraError(
                    f"Samsara {method} {path} failed ({r.status}): {text[:400]}"
                )
            try:
                return json.loads(text) if text else {}
            except json.JSONDecodeError:
                raise SamsaraError(
                    f"Samsara {method} {path} returned non-JSON: {text[:200]}"
                )

    # ---------- safety events (polling) ----------------------------

    async def list_safety_events(self, *,
                                 start_time: datetime,
                                 end_time: datetime,
                                 limit: int = 100,
                                 after: Optional[str] = None
                                 ) -> Dict[str, Any]:
        """Return `/fleet/safety-events` for the given window.

        Response shape (abridged):
          {
            "data": [{
              "id": "<event-id>",
              "happenedAtTime": "ISO",
              "vehicle": {"id":"...", "name":"..."},
              "driver":  {"id":"...", "name":"..."},
              "behaviorLabels": [{"id":"...", "name":"HarshBrake"}, ...],
              "location": {"latitude":..,"longitude":..,"formattedLocation":".."},
              "coachingStatus": "...",
              "mediaUrls": ["https://..."]            # may be absent
            }, ...],
            "pagination": {"endCursor": "...", "hasNextPage": true}
          }
        """
        params: Dict[str, Any] = {
            "startTime": _iso_z(start_time),
            "endTime":   _iso_z(end_time),
            "limit":     int(limit),
        }
        if after:
            params["after"] = after
        return await self._request("GET", "/fleet/safety-events", params=params)

    async def iter_safety_events(self, *,
                                 start_time: datetime,
                                 end_time: datetime,
                                 page_size: int = 100):
        """Async generator — walks every page of the safety-events feed."""
        after: Optional[str] = None
        while True:
            page = await self.list_safety_events(
                start_time=start_time, end_time=end_time,
                limit=page_size, after=after,
            )
            for row in (page.get("data") or []):
                yield row
            pag = page.get("pagination") or {}
            if not pag.get("hasNextPage"):
                return
            after = pag.get("endCursor")
            if not after:
                return

    # ---------- webhooks CRUD --------------------------------------

    async def list_webhooks(self) -> List[Dict[str, Any]]:
        res = await self._request("GET", "/webhooks")
        return (res.get("data") or []) if isinstance(res, dict) else []

    async def create_webhook(self, *, name: str, url: str,
                             secret: Optional[str] = None,
                             event_types: Optional[List[str]] = None
                             ) -> Dict[str, Any]:
        """Register a push webhook. Samsara will POST events to `url`.

        If `secret` is set, Samsara signs every POST with HMAC-SHA256
        in the `X-Samsara-Signature` header — see services/webhook.py
        for verification. The dashcam-detected events route through
        Alert Configurations; the hardware events (HarshBrake, Crash,
        etc.) also stream here if you create a matching Alert
        Configuration pointing to this webhook id.
        """
        body: Dict[str, Any] = {"name": name, "url": url}
        if secret:
            body["secret"] = secret
        if event_types:
            body["eventTypes"] = event_types
        res = await self._request("POST", "/webhooks", json_body=body)
        return (res.get("data") or {}) if isinstance(res, dict) else {}

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_id}")

    # ---------- dashcam media retrieval ----------------------------

    async def request_media(self, *,
                            vehicle_id: str,
                            happened_at: datetime,
                            lookback_s: int = 15,
                            lookahead_s: int = 15,
                            media_type: str = "video",
                            inputs: Optional[List[str]] = None
                            ) -> str:
        """Kick off a dashcam retrieval around `happened_at` and return
        the retrievalId. Caller then polls `poll_media(retrievalId)`."""
        inputs = inputs or ["dashcamRoadFacing", "dashcamDriverFacing"]
        start = happened_at - timedelta(seconds=lookback_s)
        end   = happened_at + timedelta(seconds=lookahead_s)
        body = {
            "vehicleId": str(vehicle_id),
            "startTime": _iso_z(start),
            "endTime":   _iso_z(end),
            "mediaType": media_type,
            "inputs":    inputs,
        }
        res = await self._request("POST", "/cameras/media/retrieval",
                                  json_body=body)
        data = res.get("data") or {}
        rid = data.get("retrievalId") or data.get("id")
        if not rid:
            raise SamsaraError(f"Media retrieval returned no id: {res}")
        return str(rid)

    async def poll_media(self, retrieval_id: str) -> Dict[str, Any]:
        """One-shot GET; caller is responsible for its own polling loop
        so this stays simple and testable."""
        return await self._request("GET", "/cameras/media/retrieval",
                                   params={"retrievalId": retrieval_id})
