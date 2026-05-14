"""Adapter-side per-lift session-occupant lock.

The QuikSync server owns the authoritative session lock via a
Hazelcast IMap (`open-rmf:lift-sessions`) per the v2 connector spec.
This module is **defense-in-depth on top of that**, not a replacement.

Motivation (lifted from the Octa `lci-rmf-adapter` reference pattern):
- The server enforces session uniqueness at the IMap level — concurrent
  `AGV_MODE` requests from different `session_id`s correctly produce
  one 202 + one 409.
- Even so, the adapter ROS subscriber may receive a conflicting
  `LiftRequest` from RMF (e.g. a second fleet adapter on the same
  rmf-web instance dispatches to the same lift while a session is
  active). If we forward every request blindly, the server will fan
  out a 409 — fine in steady state, but in flight-time the rmf
  planner has no fast feedback, and the noisy 409s flag in
  observability.
- A small adapter-side `try_acquire` / `release` pair lets us
  short-circuit obvious conflicts: when our last-seen state frame
  shows the lift held by session A, and RMF sends an AGV_MODE for
  session B, reject locally without a round-trip.

The manager is the source of truth for **what RMF most recently asked
for**; the server is the source of truth for **what's actually
locked**. The two views are reconciled by `observe_server_state(...)`,
which the lift handle calls on every state-push frame.

Thread-safe via `threading.RLock` — `try_acquire` / `release` are
called from the rclpy subscriber thread; `observe_server_state` is
called from the asyncio state-pump thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("lift_adapter_quiksync.session_manager")


class LiftSessionManager:
    """Per-lift, in-process session-occupant map.

    One manager per node; tracks state for every lift the node owns.

    Two pieces of state per lift:
    - `_requested[lift]`: the `session_id` most recently passed to
      `try_acquire(lift, session_id)`. Set on RMF AGV_MODE; cleared on
      END_SESSION or successful release.
    - `_server[lift]`: the `session_id` most recently observed in a
      server state-push frame (empty string = lift free, per the wire
      contract). Set by `observe_server_state(...)`.

    Acquisition logic:
    - `try_acquire(lift, session_id)` returns True if:
      (a) we have no record of either requested or server holder (free), or
      (b) the requested session matches our last known requested (same
          fleet/robot retrying), or
      (c) the server-observed holder matches the requesting session_id
          (we already hold it, server-confirmed).
    - Returns False otherwise — another session holds (or has requested)
      this lift.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requested: dict[str, str] = {}
        self._server: dict[str, str] = {}

    def try_acquire(self, lift: str, session_id: str) -> bool:
        """Attempt to claim `lift` for `session_id` on the adapter side.

        Returns True if the claim is consistent with current state
        (free / same session retrying / server confirms us holding).
        Returns False if a different session already holds or has
        requested this lift.

        Caller (lift handle) should NOT POST the AGV_MODE request to
        the server when this returns False — the server would just 409
        anyway, and we save a round-trip.
        """
        if not session_id:
            log.warning("try_acquire called with empty session_id; rejecting")
            return False
        with self._lock:
            current_request = self._requested.get(lift, "")
            current_server = self._server.get(lift, "")
            # Free → accept
            if not current_request and not current_server:
                self._requested[lift] = session_id
                return True
            # Same session retrying → accept
            if current_request == session_id:
                return True
            # Server confirms we hold → accept (sync our request view too)
            if current_server == session_id:
                self._requested[lift] = session_id
                return True
            log.info(
                "try_acquire(lift=%s, session_id=%s) rejected: held by "
                "requested=%r / server=%r",
                lift, session_id, current_request, current_server,
            )
            return False

    def release(self, lift: str, session_id: str) -> bool:
        """Release the lift if held by `session_id`.

        Returns True if the release succeeded (or the lift was already
        free). Returns False if a different session holds the lift —
        END_SESSION on someone else's lift would fail on the server
        side too.
        """
        with self._lock:
            current_request = self._requested.get(lift, "")
            current_server = self._server.get(lift, "")
            if not current_request and not current_server:
                return True  # already free
            if current_request == session_id or current_server == session_id:
                self._requested.pop(lift, None)
                # Leave _server alone — it tracks what the server says,
                # and will sync via the next state-push frame.
                return True
            log.info(
                "release(lift=%s, session_id=%s) rejected: held by "
                "requested=%r / server=%r",
                lift, session_id, current_request, current_server,
            )
            return False

    def observe_server_state(self, lift: str, server_session_id: str) -> None:
        """Update the manager's view of who the server says holds the
        lift. Called on every state-push frame.

        `server_session_id` is the `session_id` field of the LiftState
        frame; empty string indicates the server-side lock is free.

        If the server view doesn't match our request view, our request
        was either superseded (different fleet won) or expired
        (server-side TTL). Clear our request view so the next
        `try_acquire` from a fresh session sees a clean slate.
        """
        with self._lock:
            self._server[lift] = server_session_id or ""
            current_request = self._requested.get(lift, "")
            if (
                current_request
                and server_session_id
                and current_request != server_session_id
            ):
                log.info(
                    "observe_server_state(lift=%s): server holder %r differs "
                    "from our request %r — superseded; clearing our request",
                    lift, server_session_id, current_request,
                )
                self._requested.pop(lift, None)
            elif current_request and not server_session_id:
                # Server view is free; our request didn't win or expired.
                log.info(
                    "observe_server_state(lift=%s): server reports free, "
                    "but we had requested %r; clearing our request",
                    lift, current_request,
                )
                self._requested.pop(lift, None)

    def current_holder(self, lift: str) -> Optional[str]:
        """Read the most authoritative holder of the lift.

        Server view wins over request view (the server is the source
        of truth); empty string from the server means "free"; if we
        have no record at all, returns None.
        """
        with self._lock:
            if lift in self._server:
                holder = self._server[lift]
                return holder if holder else None
            if lift in self._requested:
                holder = self._requested[lift]
                return holder if holder else None
            return None
