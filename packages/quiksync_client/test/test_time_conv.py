"""Tests for millis_to_time_parts — wire-millis ↔ ROS Time conversion."""

from __future__ import annotations

import pytest

from quiksync_client.time_conv import millis_to_time_parts


# ----- examples from issue #17 -----


def test_zero():
    assert millis_to_time_parts(0) == (0, 0)


def test_one_second_with_subsecond():
    assert millis_to_time_parts(1234) == (1, 234_000_000)


def test_realistic_epoch_ms():
    """A live timestamp captured from the staging discovery probe."""
    assert millis_to_time_parts(1778760087657) == (1778760087, 657_000_000)


# ----- boundaries -----


def test_exact_second():
    """Whole seconds → nanosec = 0."""
    assert millis_to_time_parts(5000) == (5, 0)


def test_999ms_subsecond():
    """The maximum subsecond before rolling over."""
    assert millis_to_time_parts(999) == (0, 999_000_000)


def test_one_millisecond():
    assert millis_to_time_parts(1) == (0, 1_000_000)


def test_one_second_one_ms():
    assert millis_to_time_parts(1001) == (1, 1_000_000)


# ----- nanosec invariant -----


@pytest.mark.parametrize("millis", [0, 1, 999, 1000, 1234, 9999, 1778760087657])
def test_nanosec_always_within_billion(millis):
    """nanosec must always be < 1_000_000_000 — that's the
    builtin_interfaces/Time invariant. If it overflows, sec was wrong."""
    sec, nanosec = millis_to_time_parts(millis)
    assert 0 <= nanosec < 1_000_000_000
    assert sec >= 0


# ----- type guard -----


def test_rejects_float():
    """Floats would silently truncate — make the rejection explicit."""
    with pytest.raises(TypeError, match="int"):
        millis_to_time_parts(1234.5)  # type: ignore[arg-type]


def test_rejects_string():
    with pytest.raises(TypeError, match="int"):
        millis_to_time_parts("1234")  # type: ignore[arg-type]


def test_rejects_bool():
    """bool is an int subclass — True would silently become (0, 1_000_000).
    Reject explicitly so a JSON-shape bug surfaces at the boundary."""
    with pytest.raises(TypeError, match="bool"):
        millis_to_time_parts(True)  # type: ignore[arg-type]


def test_rejects_none():
    with pytest.raises(TypeError):
        millis_to_time_parts(None)  # type: ignore[arg-type]


# ----- spread compatibility with ROS Time msg constructors -----


def test_tuple_unpacks_to_named_args():
    """The tuple must unpack cleanly so adapter code can write
    `Time(sec=sec, nanosec=nanosec)` without ceremony."""
    sec, nanosec = millis_to_time_parts(1234)
    # Simulate the constructor call shape.
    fake_msg = {"sec": sec, "nanosec": nanosec}
    assert fake_msg == {"sec": 1, "nanosec": 234_000_000}
