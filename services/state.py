"""
services/state.py
=================
Tiny JSON-file state store. Holds:

  * topics          : {display_name: message_thread_id, ...}
                      So we don't recreate the "Harsh Braking" topic
                      every reboot.
  * seen_event_ids  : rolling de-dup set so the webhook path and the
                      poller fallback never post the same event twice.
                      Capped at MAX_SEEN entries (FIFO) so the file
                      doesn't grow forever.
  * last_poll_time  : ISO timestamp — the poller resumes from here on
                      next run instead of re-scanning the full window.

Why a file and not Postgres? The bot is single-process by design (one
Railway service, one webhook receiver), state is small, and this keeps
the deployment zero-dependency beyond Python + aiogram. If you ever
scale horizontally, swap this module for a tiny Redis-backed one.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

MAX_SEEN = 5000


class State:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data: Dict = {
            "topics":          {},     # {display_name: thread_id}
            "seen_event_ids":  [],     # ordered list, newest at end
            "last_poll_time":  None,   # ISO Zulu
        }
        self._load()

    # ---------- disk I/O -------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Shallow merge so missing keys fall back to defaults.
            self._data.update(raw)
            if not isinstance(self._data.get("seen_event_ids"), list):
                self._data["seen_event_ids"] = []
            if not isinstance(self._data.get("topics"), dict):
                self._data["topics"] = {}
        except FileNotFoundError:
            logger.info("State file %s not found; starting fresh.", self.path)
        except Exception as e:
            logger.warning("State load failed (%s). Starting fresh.", e)

    def _save(self) -> None:
        # Atomic write — never leave a half-written file if the process
        # dies mid-flush (e.g. Railway redeploy).
        dir_ = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".state.", dir=dir_)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass
            raise

    # ---------- topics ---------------------------------------------
    def get_topic_id(self, name: str) -> Optional[int]:
        v = self._data["topics"].get(name)
        return int(v) if v is not None else None

    def set_topic_id(self, name: str, thread_id: int) -> None:
        self._data["topics"][name] = int(thread_id)
        self._save()

    def all_topics(self) -> Dict[str, int]:
        return {k: int(v) for k, v in self._data["topics"].items()}

    def forget_topic(self, name: str) -> None:
        self._data["topics"].pop(name, None)
        self._save()

    # ---------- event dedup ----------------------------------------
    def is_seen(self, event_id: str) -> bool:
        return event_id in self._data["seen_event_ids"]

    def mark_seen(self, event_id: str) -> None:
        if event_id in self._data["seen_event_ids"]:
            return
        self._data["seen_event_ids"].append(event_id)
        # Keep newest MAX_SEEN only.
        if len(self._data["seen_event_ids"]) > MAX_SEEN:
            self._data["seen_event_ids"] = self._data["seen_event_ids"][-MAX_SEEN:]
        self._save()

    # ---------- poller cursor --------------------------------------
    def get_last_poll_time(self) -> Optional[str]:
        return self._data.get("last_poll_time")

    def set_last_poll_time(self, iso_z: Optional[str]) -> None:
        self._data["last_poll_time"] = iso_z
        self._save()

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
