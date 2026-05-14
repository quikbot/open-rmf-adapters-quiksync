"""Time conversion between unix epoch milliseconds (JSON wire shape) and
the `(sec, nanosec)` pair used by ROS 2 `builtin_interfaces/Time`.

The QuikSync Open-RMF Connector emits state-frame timestamps as integer
unix epoch milliseconds, matching the rmf-web JSON convention
(`RobotState.unix_millis_time`, `TaskState.unix_millis_*`, etc.). The
ROS messages on the other side of an adapter
(`rmf_door_msgs/DoorState`, `rmf_lift_msgs/LiftState`, …) expect the
value as a `builtin_interfaces/Time` struct with separate `sec` +
`nanosec` fields.

This module keeps the translation in a single place so each adapter
package can apply it identically:

```python
from quiksync_client.time_conv import millis_to_time_parts

sec, nanosec = millis_to_time_parts(json_frame["door_time"])
ros_msg.door_time = Time(sec=sec, nanosec=nanosec)
```

The helper deliberately returns a `tuple[int, int]` rather than
constructing a `builtin_interfaces.msg.Time` directly — that keeps
`quiksync_client` free of any ROS imports, so the package remains
unit-testable on a Python install without `rmf_ros2`. ROS msg
construction is one line at the adapter's call site.
"""

from __future__ import annotations


_MS_PER_SEC = 1_000
_NS_PER_MS = 1_000_000


def millis_to_time_parts(millis: int) -> tuple[int, int]:
    """Split a unix-epoch-millis integer into `(sec, nanosec)` parts.

    The returned tuple is spread directly into
    `builtin_interfaces.msg.Time(sec=sec, nanosec=nanosec)` at the
    ROS-msg construction site. Negative inputs (pre-epoch timestamps)
    are not expected from the QuikSync server but are handled
    consistently anyway — `sec` floor-divides and `nanosec` carries the
    sub-second remainder.

    Examples:
        >>> millis_to_time_parts(0)
        (0, 0)
        >>> millis_to_time_parts(1234)
        (1, 234000000)
        >>> millis_to_time_parts(1778760087657)
        (1778760087, 657000000)
    """
    if not isinstance(millis, int) or isinstance(millis, bool):
        # bool is an int subclass; guard against accidental True/False.
        raise TypeError(
            f"millis must be int (not bool); got {type(millis).__name__}"
        )
    sec = millis // _MS_PER_SEC
    nanosec = (millis % _MS_PER_SEC) * _NS_PER_MS
    return sec, nanosec
