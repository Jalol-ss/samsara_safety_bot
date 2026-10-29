"""
engine/normalize.py
===================
Normalize events from Samsara into a single internal shape, no matter
whether they came in through the webhook POST body or the
`/fleet/safety-events` polling response.

Normalized event:
    {
      "id":           str   — unique event id (for dedup)
      "type":         str   — behavior label, e.g. "HarshBrake"
      "topic_name":   str   — display name of the Telegram topic
      "happened_at":  datetime (UTC) — when the event occurred
      "vehicle_id":   str | None
      "vehicle_name": str   — "077" or "077 Gerald Matthews"
      "unit_number":  str   — just the numeric unit, best-effort parse
      "driver_name":  str
      "location":     str   — pretty address, e.g. "I 75, Richmond, KY"
      "lat":          float | None
      "lon":          float | None
      "speed_mph":    float | None
      "media_urls":   list[str]
      "dashboard_url":str   — "Open in Samsara" deep link if known
      "raw":          dict  — original payload for debugging
    }

Samsara's webhook v2 payloads and the REST feed are *almost* identical
but not quite — this module hides those differences so the rest of the
bot only ever sees the normalized dict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import EVENT_TOPIC_NAMES


_UNIT_RE = re.compile(r"(\d{2,8})")


def _parse_dt(v: Any) -> datetime:
    """Accept ISO strings in common Samsara shapes; default to 'now'."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        # Milliseconds since epoch (Samsara webhooks sometimes use this).
        if v > 10_000_000_000:
            v = v / 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(v, str) and v:
        s = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _best_label(behavior_labels: Any, fallback: str = "") -> str:
    """Samsara returns one or more labels per event — pick a stable
    one to drive routing. We prefer the first with a `name`, falling
    back to any raw string we can find."""
    if isinstance(behavior_labels, list):
        for lbl in behavior_labels:
            if isinstance(lbl, dict) and lbl.get("name"):
                return str(lbl["name"])
            if isinstance(lbl, str) and lbl:
                return lbl
    if isinstance(behavior_labels, dict):
        n = behavior_labels.get("name")
        if n: return str(n)
    if isinstance(behavior_labels, str):
        return behavior_labels
    return fallback or ""


def _topic_name_for(event_type: str) -> str:
    """Map Samsara's label to a Telegram topic display name."""
    if event_type in EVENT_TOPIC_NAMES:
        return EVENT_TOPIC_NAMES[event_type]
    # Try case-insensitive lookup — Samsara is a bit inconsistent.
    for k, v in EVENT_TOPIC_NAMES.items():
        if k.lower() == event_type.lower():
            return v
    return EVENT_TOPIC_NAMES.get("_fallback", "Other Safety Events")


def _unit_from_name(name: str) -> str:
    m = _UNIT_RE.search(name or "")
    return m.group(1) if m else (name or "")


def _mph(val: Any) -> Optional[float]:
    """Samsara returns speed in meters/second in most payloads."""
    try:
        if val is None: return None
        return round(float(val) * 2.236936, 1)
    except (TypeError, ValueError):
        return None


def normalize_safety_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one raw Samsara event dict into our internal shape.

    Handles both:
      • REST feed row (`/fleet/safety-events` data[] element)
      • Webhook body shape (payload["event"] or top-level event dict)
    """
    # Webhook pushes wrap the event under keys like "event" or "data".
    e = payload
    if "event" in e and isinstance(e["event"], dict):
        e = e["event"]
    elif "data" in e and isinstance(e["data"], dict):
        e = e["data"]

    # ---- identifiers ------
    ev_id = (str(e.get("id") or e.get("eventId")
                 or e.get("safetyEventId") or "")).strip()

    # ---- vehicle / driver ------
    v = e.get("vehicle") or {}
    d = e.get("driver")  or {}
    vehicle_id   = str(v.get("id")) if v.get("id") else None
    vehicle_name = v.get("name") or e.get("vehicleName") or ""
    driver_name  = d.get("name")  or e.get("driverName")  or ""

    # ---- behavior label ------
    event_type = _best_label(
        e.get("behaviorLabels") or e.get("behaviors") or e.get("eventType"),
        fallback=str(e.get("type") or ""),
    )

    # ---- location ------
    loc = e.get("location") or {}
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    pretty = (loc.get("formattedLocation")
              or loc.get("reverseGeo", {}).get("formattedLocation")
              or e.get("address") or "")

    # ---- media + deep link ------
    media_urls: List[str] = []
    for key in ("mediaUrls", "assets", "videoUrls"):
        v2 = e.get(key)
        if isinstance(v2, list):
            for item in v2:
                if isinstance(item, str):
                    media_urls.append(item)
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("downloadUrl")
                    if u: media_urls.append(u)

    dashboard_url = (e.get("dashboardUrl")
                     or e.get("samsaraLink")
                     or e.get("url") or "")

    return {
        "id":           ev_id,
        "type":         event_type,
        "topic_name":   _topic_name_for(event_type),
        "happened_at":  _parse_dt(e.get("happenedAtTime") or e.get("time")
                                  or e.get("startTime")),
        "vehicle_id":   vehicle_id,
        "vehicle_name": vehicle_name,
        "unit_number":  _unit_from_name(vehicle_name),
        "driver_name":  driver_name,
        "location":     pretty,
        "lat":          float(lat) if lat is not None else None,
        "lon":          float(lon) if lon is not None else None,
        "speed_mph":    _mph(e.get("speed") or e.get("vehicleSpeed")),
        "media_urls":   media_urls,
        "dashboard_url": dashboard_url,
        "raw":          payload,
    }
