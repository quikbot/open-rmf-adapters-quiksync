"""Open-RMF `RobotCallbacks` factories.

Builds the three callables EasyFullControl needs to dispatch commands
into a QuikSync-managed robot:

- `navigate(destination, execution)` — forward Open-RMF's planner output to
  `MOVE` (or `DOCK` when `destination.dock` is set) via
  `POST /api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/navigate`.
- `stop()` — cancel the in-flight command via
  `POST .../robots/{robot}/stop`.
- `action_executor(category, description, execution)` — opaque
  passthrough to `POST .../robots/{robot}/perform_action` for tasks
  outside Open-RMF's built-in vocabulary (cleaning, charging extensions,
  etc.). Category resolution is the server's responsibility; the
  adapter forwards opaquely.

Each factory takes the HTTP client and identity parameters (fleet,
robot name, robot handle, execution-id factory) and returns the
callable that `RobotCallbacks` consumes. Factoring this way keeps the
logic testable without importing `rmf_adapter` — the tests inject a
fake `Destination` via `sys.modules`.

Completion semantics: Open-RMF tracks task completion via the `current_activity`
field on `EasyRobotUpdateHandle.update()`. The navigate / action callbacks
set the handle's current activity to the `execution.identifier` so that
state pushes from `state_pump.py` carry the correlation back to Open-RMF.
The QuikSync server side flips the `task_id`'s status when the underlying
command finishes; state-pump-driven `FleetState` frames then reflect
the change and Open-RMF sees the activity clear.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional

from quiksync_client import QuikSyncClientError, QuikSyncConnectionError, QuikSyncHttpClient, QuikSyncServerError

from .robot_handle import RobotHandle

log = logging.getLogger("fleet_adapter_quiksync.callbacks")


def _new_execution_id() -> str:
    return str(uuid.uuid4())


def _extract_destination(destination: Any) -> Optional[dict[str, Any]]:
    """Translate the rmf_adapter Destination into the wire shape expected by
    the QuikSync navigate endpoint.

    Defensive: returns None if the destination is unusable. EasyFullControl
    binds vary slightly across rmf_adapter versions — try the documented
    attribute path first, then fall back through known alternatives.
    """
    try:
        map_name = getattr(destination, "map", None)
        if not isinstance(map_name, str) or not map_name:
            return None

        position = getattr(destination, "position", None)
        if position is None:
            return None
        # rmf_adapter Vector3d-style (.x/.y/.yaw) is the documented shape.
        # Fall back to indexed access for (x, y) since they're load-bearing;
        # for yaw, default to 0.0 if neither attribute nor index 2 is present
        # so a future 2-element point doesn't silently drop the navigate.
        x = float(getattr(position, "x", None) if hasattr(position, "x") else position[0])
        y = float(getattr(position, "y", None) if hasattr(position, "y") else position[1])
        if hasattr(position, "yaw"):
            yaw = float(getattr(position, "yaw"))
        else:
            try:
                yaw = float(position[2])
            except (IndexError, TypeError):
                yaw = 0.0
    except (AttributeError, IndexError, TypeError, ValueError):
        return None

    return {"x": x, "y": y, "yaw": yaw, "map_name": map_name}


def _dock_name(destination: Any) -> Optional[str]:
    """Extract a non-empty dock name from the destination, or None.

    Open-RMF's `Destination.dock` is an empty string when there's no dock; the
    QuikSync endpoint expects null/omitted in that case to dispatch a MOVE
    rather than a DOCK.
    """
    dock = getattr(destination, "dock", None)
    if isinstance(dock, str) and dock:
        return dock
    return None


def _speed_limit(destination: Any) -> Optional[float]:
    """Extract a positive speed limit, or None.

    Open-RMF passes 0.0 to mean "no limit"; we forward only positive values.
    """
    limit = getattr(destination, "speed_limit", None)
    if isinstance(limit, (int, float)) and limit > 0:
        return float(limit)
    return None


def _activity_identifier(execution: Any) -> Any:
    """Best-effort extraction of an ActivityIdentifier from the execution.

    Different rmf_adapter versions expose this differently. We try the
    common attribute names; falling through returns None which means the
    next state push carries no current_activity (Open-RMF can still see the
    robot move; it just can't correlate it back to a specific dispatch).
    """
    for attr in ("identifier", "activity", "activity_identifier"):
        ident = getattr(execution, attr, None)
        if ident is not None:
            return ident
    return None


def make_navigate_callback(
    http: QuikSyncHttpClient,
    fleet: str,
    robot: str,
    handle: RobotHandle,
    execution_id_factory: Callable[[], str] = _new_execution_id,
    namespace: Optional[str] = None,
) -> Callable[[Any, Any], None]:
    """Build the `RobotCallbacks.navigate` callable for one robot.

    Returns a function with the signature `navigate(destination, execution)`
    that EasyFullControl will invoke when the Open-RMF planner has a target for
    this robot. The function is fire-and-forget: completion is observed via
    the WSS state stream pushing the matching `task_id` into RobotHandle.
    """

    def navigate(destination: Any, execution: Any) -> None:
        body = _extract_destination(destination)
        if body is None:
            log.warning(
                "navigate(%s/%s): unusable Destination (%r); ignoring",
                fleet, robot, destination,
            )
            return
        execution_id = execution_id_factory()
        try:
            response = http.post_navigate(
                fleet=fleet,
                robot=robot,
                execution_id=execution_id,
                destination=body,
                dock_name=_dock_name(destination),
                speed_limit=_speed_limit(destination),
                namespace=namespace,
            )
        except QuikSyncClientError as e:
            log.error(
                "navigate(%s/%s) failed: HTTP %s %s — %s",
                fleet, robot, e.status, e.error_code, e.body,
            )
            return
        except (QuikSyncServerError, QuikSyncConnectionError) as e:
            log.error("navigate(%s/%s) failed transport/5xx: %s", fleet, robot, e)
            return

        task_id = response.get("task_id") if isinstance(response, dict) else None
        log.info(
            "navigate(%s/%s) dispatched: execution_id=%s task_id=%s",
            fleet, robot, execution_id, task_id,
        )
        handle.set_current_activity(_activity_identifier(execution))

    return navigate


def make_stop_callback(
    http: QuikSyncHttpClient,
    fleet: str,
    robot: str,
    handle: RobotHandle,
    execution_id_factory: Callable[[], str] = _new_execution_id,
    namespace: Optional[str] = None,
) -> Callable[[Any], None]:
    """Build the `RobotCallbacks.stop` callable for one robot.

    Returns a function with the signature `stop(activity)` that
    EasyFullControl invokes when Open-RMF needs the robot to halt. The
    `activity` argument is the `ActivityIdentifier` of the activity RMF
    wants stopped; the QuikSync server is the authoritative scheduler
    and treats `/stop` as idempotent per the QuikSync adapter API
    contract, so we forward unconditionally rather than gating on a
    local activity-match check (the fleet_adapter_template does the
    local gate because the robot is the authority there; here the
    server is). Safe to call repeatedly with the same or different
    activity identifiers.
    """

    def stop(activity: Any) -> None:
        execution_id = execution_id_factory()
        try:
            http.post_stop(fleet=fleet, robot=robot, execution_id=execution_id, namespace=namespace)
        except QuikSyncClientError as e:
            log.error(
                "stop(%s/%s) failed: HTTP %s %s — %s",
                fleet, robot, e.status, e.error_code, e.body,
            )
            return
        except (QuikSyncServerError, QuikSyncConnectionError) as e:
            log.error("stop(%s/%s) failed transport/5xx: %s", fleet, robot, e)
            return
        log.info(
            "stop(%s/%s) dispatched: execution_id=%s activity=%r",
            fleet, robot, execution_id, activity,
        )
        # `stop` doesn't set current_activity — handle stays on whatever it
        # was; the resulting state pump frame will clear it when the server
        # cancels the underlying command.

    return stop


def make_action_executor(
    http: QuikSyncHttpClient,
    fleet: str,
    robot: str,
    handle: RobotHandle,
    execution_id_factory: Callable[[], str] = _new_execution_id,
    namespace: Optional[str] = None,
) -> Callable[[str, Any, Any], None]:
    """Build the `RobotCallbacks.action_executor` callable for one robot.

    Returns a function with the signature `action_executor(category, description, execution)`
    that EasyFullControl invokes for `perform_action` task phases. Category
    resolution is the server's responsibility; the adapter forwards opaquely.

    Unknown categories surface as 400 at the server; we log + return without
    raising so Open-RMF can decide whether to retry / fail the phase.
    """

    def action_executor(category: str, description: Any, execution: Any) -> None:
        execution_id = execution_id_factory()
        try:
            http.post_perform_action(
                fleet=fleet,
                robot=robot,
                execution_id=execution_id,
                category=category,
                description=description,
                namespace=namespace,
            )
        except QuikSyncClientError as e:
            log.error(
                "action_executor(%s/%s, category=%s) failed: HTTP %s %s — %s",
                fleet, robot, category, e.status, e.error_code, e.body,
            )
            return
        except (QuikSyncServerError, QuikSyncConnectionError) as e:
            log.error(
                "action_executor(%s/%s, category=%s) failed transport/5xx: %s",
                fleet, robot, category, e,
            )
            return
        log.info(
            "action_executor(%s/%s) dispatched: category=%s execution_id=%s",
            fleet, robot, category, execution_id,
        )
        handle.set_current_activity(_activity_identifier(execution))

    return action_executor
