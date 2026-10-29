"""
config.py — Samsara Safety Bot
================================
Env-var-first config. Every setting has a sensible default so the bot
boots even when half the vars are unset; the missing pieces just
disable the matching feature (webhook server won't start without
BOT_TOKEN_SAFETY, dashcam attach won't fire without SAMSARA_API_TOKEN,
etc.).

Keep this file dependency-free so it's safe to import from anywhere.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the same directory as this file, regardless of cwd.
load_dotenv(Path(__file__).resolve().parent / ".env")

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _csv(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


# ---------- Telegram ------------------------------------------------
BOT_TOKEN_SAFETY     = os.getenv("BOT_TOKEN_SAFETY", "")
SAFETY_GROUP_CHAT_ID = _int("SAFETY_GROUP_CHAT_ID", 0)

# ---------- Samsara -------------------------------------------------
SAMSARA_API_TOKEN       = os.getenv("SAMSARA_API_TOKEN", "")
SAMSARA_BASE_URL        = os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com")
SAMSARA_WEBHOOK_SECRET  = os.getenv("SAMSARA_WEBHOOK_SECRET", "")

# ---------- Webhook server -----------------------------------------
# Public HTTPS URL used when registering the webhook with Samsara.
# The receiving path is always /webhook/samsara.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")
PORT            = _int("PORT", 8080)

# ---------- Polling fallback ---------------------------------------
ENABLE_SAFETY_POLLER   = _bool("ENABLE_SAFETY_POLLER", False)
POLL_INTERVAL_SECONDS  = _int("POLL_INTERVAL_SECONDS", 60)

# ---------- Event scope --------------------------------------------
# Empty list = forward every event type Samsara sends.
SAFETY_EVENT_TYPES = _csv("SAFETY_EVENT_TYPES")

# ---------- Dashcam video ------------------------------------------
ATTACH_DASHCAM_VIDEO    = _bool("ATTACH_DASHCAM_VIDEO", True)
VIDEO_LOOKBACK_SECONDS  = _int("VIDEO_LOOKBACK_SECONDS", 15)
VIDEO_LOOKAHEAD_SECONDS = _int("VIDEO_LOOKAHEAD_SECONDS", 15)
VIDEO_POLL_SECONDS      = _int("VIDEO_POLL_SECONDS", 90)

# ---------- Topic names (Samsara event-type → Telegram topic name) --
# These are the DEFAULT display names for auto-created topics. One
# topic per event type. The bot creates any topic that doesn't exist
# yet and remembers its message_thread_id in state_file.
#
# The mapping also decides which event names collapse into one topic.
# Right-hand values must be identical for events you want to merge.
#
# Add new rows here when Samsara ships new event labels.
EVENT_TOPIC_NAMES: dict[str, str] = {
    # Hardware (gateway/accelerometer) ----
    "HarshBrake":                  "Harsh Braking",
    "Braking":                     "Harsh Braking",
    "HarshTurn":                   "Harsh Turn",
    "HarshAcceleration":           "Harsh Acceleration",
    "Crash":                       "Crash",
    # Dashcam AI --------------------------
    "NearCollision":               "Near Collision",
    "FollowingDistance":           "Following Distance",
    "RollingStop":                 "Rolling Stop",
    "RanRedLight":                 "Ran Red Light",
    "DistractedDriving":           "Distracted Driving",
    "NoSeatbelt":                  "No Seatbelt",
    "PassengerDetected":           "Passenger Detected",
    "PedestrianCollisionWarning":  "Pedestrian Collision Warning",
    # Speeding ---------------------------
    "SevereSpeeding":              "Severe Speeding",
    "Speeding":                    "Severe Speeding",
    # Catch-all fallback (any unknown behaviorLabel ends up here)
    "_fallback":                   "Other Safety Events",
}

# ---------- Local state --------------------------------------------
# Where we persist the topic-id map + last-seen event cursor.
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# ---------- Logging ------------------------------------------------
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
