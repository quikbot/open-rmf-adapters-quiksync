"""Tests for LiftSessionManager — adapter-side per-lift occupant lock."""

from __future__ import annotations

import threading

import pytest

from lift_adapter_quiksync.session_manager import LiftSessionManager


# ----- try_acquire -----


def test_acquire_free_lift_succeeds():
    sm = LiftSessionManager()
    assert sm.try_acquire("lift_alpha", "rmf:robot-1") is True
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


def test_same_session_retry_succeeds():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Same session retries (e.g. RMF resends after a timeout) — accept.
    assert sm.try_acquire("lift_alpha", "rmf:robot-1") is True


def test_conflicting_session_is_rejected():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Different session attempts to acquire the same lift — reject.
    assert sm.try_acquire("lift_alpha", "rmf:robot-2") is False


def test_empty_session_id_is_rejected():
    sm = LiftSessionManager()
    assert sm.try_acquire("lift_alpha", "") is False
    # And the lift should remain free.
    assert sm.current_holder("lift_alpha") is None


def test_different_lifts_are_independent():
    sm = LiftSessionManager()
    sm.try_acquire("lift_a", "rmf:r1")
    # A second lift can be acquired by a different session.
    assert sm.try_acquire("lift_b", "rmf:r2") is True
    assert sm.current_holder("lift_a") == "rmf:r1"
    assert sm.current_holder("lift_b") == "rmf:r2"


# ----- release -----


def test_release_by_holder_succeeds():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    assert sm.release("lift_alpha", "rmf:robot-1") is True
    # After release, the lift is free for the same session to re-acquire.
    assert sm.try_acquire("lift_alpha", "rmf:robot-1") is True


def test_release_by_non_holder_is_rejected():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    assert sm.release("lift_alpha", "rmf:robot-2") is False
    # The original holder is still in place.
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


def test_release_on_free_lift_is_idempotent():
    """Releasing a lift that was never acquired returns True (already
    free) — END_SESSION should be tolerant of no-op cases."""
    sm = LiftSessionManager()
    assert sm.release("lift_alpha", "rmf:robot-1") is True


# ----- observe_server_state -----


def test_server_state_matching_us_keeps_request():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    sm.observe_server_state("lift_alpha", "rmf:robot-1")
    # We still hold the lift on both views.
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


def test_server_state_other_session_clears_our_request():
    """If the server says someone else holds the lift, our request was
    superseded — clear it so the next acquire from a fresh session
    isn't blocked by stale state."""
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    sm.observe_server_state("lift_alpha", "rmf:robot-2")
    # current_holder reflects what the SERVER says (priority over request view)
    assert sm.current_holder("lift_alpha") == "rmf:robot-2"
    # A fresh session_id should now be able to attempt acquire (will
    # still fail because server says someone holds it, but at least
    # we've cleared our stale request).
    assert sm.try_acquire("lift_alpha", "rmf:robot-3") is False


def test_server_state_free_after_our_request_clears_request():
    """If the server reports free + we had a pending request, our
    request didn't win (or expired) — clear it."""
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    sm.observe_server_state("lift_alpha", "")
    # current_holder should reflect free.
    assert sm.current_holder("lift_alpha") is None
    # Re-acquire should succeed since both views are now clean.
    assert sm.try_acquire("lift_alpha", "rmf:robot-1") is True


def test_server_view_wins_over_request_view():
    """current_holder prioritises server state — that's the source of truth."""
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Even if our request hasn't been confirmed, server view dominates.
    sm.observe_server_state("lift_alpha", "rmf:robot-other")
    assert sm.current_holder("lift_alpha") == "rmf:robot-other"


def test_acquire_after_server_confirms_us_succeeds():
    """If the server pushes a state showing we hold the lift, a fresh
    acquire by the same session re-confirms (idempotent)."""
    sm = LiftSessionManager()
    sm.observe_server_state("lift_alpha", "rmf:robot-1")
    assert sm.try_acquire("lift_alpha", "rmf:robot-1") is True


# ----- threading -----


def test_concurrent_acquires_serialise():
    """Two threads racing to acquire the same lift — only one wins.

    Run many trials to catch a race that wins probabilistically."""
    successes = []
    lock = threading.Lock()

    def worker(sm: LiftSessionManager, session_id: str) -> None:
        acquired = sm.try_acquire("lift_alpha", session_id)
        with lock:
            successes.append((session_id, acquired))

    for _ in range(20):
        sm = LiftSessionManager()
        t1 = threading.Thread(target=worker, args=(sm, "rmf:robot-1"))
        t2 = threading.Thread(target=worker, args=(sm, "rmf:robot-2"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        successes_for_trial = [acq for _, acq in successes[-2:]]
        # Exactly one should succeed.
        assert successes_for_trial.count(True) == 1
        assert successes_for_trial.count(False) == 1


# ----- current_holder -----


def test_current_holder_returns_none_for_unknown_lift():
    sm = LiftSessionManager()
    assert sm.current_holder("never-seen") is None


def test_current_holder_after_request_only():
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Before any state-push, we report what we requested.
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


# ----- TTL eviction -----


def test_ttl_disabled_by_default():
    """ttl_seconds=0 → eviction disabled; entries persist indefinitely."""
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Manually rewind the request time to simulate a stale entry.
    sm._requested_at["lift_alpha"] = sm._requested_at["lift_alpha"] - 99999
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


def test_ttl_evicts_stale_request_on_try_acquire():
    """A stale `_requested` entry is dropped before deciding the acquire."""
    sm = LiftSessionManager(ttl_seconds=1.0)
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    # Backdate so the entry is older than the TTL.
    sm._requested_at["lift_alpha"] -= 5.0
    # A different session can now acquire — the stale request was evicted.
    assert sm.try_acquire("lift_alpha", "rmf:robot-2") is True
    assert sm.current_holder("lift_alpha") == "rmf:robot-2"


def test_ttl_evicts_stale_request_on_observe_server_state():
    """`observe_server_state` evicts a stale `_requested` even when the
    server view has no change to report."""
    sm = LiftSessionManager(ttl_seconds=1.0)
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    sm._requested_at["lift_alpha"] -= 5.0
    # Server view stays empty → without TTL eviction the local request
    # would be cleared anyway, so this exercises the "lock is held
    # server-side" path: server says held by robot-1 too.
    sm.observe_server_state("lift_alpha", "rmf:robot-1")
    assert "lift_alpha" not in sm._requested
    assert "lift_alpha" not in sm._requested_at


def test_ttl_refreshes_on_same_session_retry():
    """Same-session retry refreshes the timestamp so the entry isn't
    evicted while the fleet is still active."""
    sm = LiftSessionManager(ttl_seconds=1.0)
    sm.try_acquire("lift_alpha", "rmf:robot-1")
    sm._requested_at["lift_alpha"] -= 0.6  # 0.6s ago
    sm.try_acquire("lift_alpha", "rmf:robot-1")  # refresh
    sm._requested_at["lift_alpha"] -= 0.6  # but only 0.6s since refresh
    # Total elapsed since first acquire is 1.2s but TTL is 1.0s; the
    # mid-way refresh means we're still inside TTL relative to the
    # latest activity, so the request stands.
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"
