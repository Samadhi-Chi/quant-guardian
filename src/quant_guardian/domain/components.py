from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ComponentState(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    IDLE = "idle"
    UNKNOWN = "unknown"
    RECOVERING = "recovering"


STATE_RANK = {
    ComponentState.UNKNOWN: 0,
    ComponentState.IDLE: 1,
    ComponentState.HEALTHY: 2,
    ComponentState.RECOVERING: 3,
    ComponentState.WARNING: 4,
    ComponentState.CRITICAL: 5,
}


@dataclass(frozen=True, slots=True)
class ComponentNode:
    id: str
    name: str
    state: ComponentState
    reason: str
    observed_at: datetime
    priority: str = "normal"
    metrics: dict[str, Any] = field(default_factory=dict)
    children: tuple[ComponentNode, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "reason": self.reason,
            "observed_at": self.observed_at.isoformat(),
            "priority": self.priority,
            "metrics": dict(self.metrics),
            "children": [child.to_dict() for child in self.children],
        }


def aggregate_state(
    children: Iterable[ComponentNode],
    *,
    default: ComponentState = ComponentState.UNKNOWN,
) -> ComponentState:
    values = tuple(children)
    if not values:
        return default
    actionable = [child.state for child in values if child.state is not ComponentState.IDLE]
    if not actionable:
        return ComponentState.IDLE
    return max(actionable, key=lambda value: STATE_RANK[value])


def component_from_dict(value: dict[str, Any], *, fallback_id: str = "unknown") -> ComponentNode:
    raw_state = str(value.get("state", "unknown"))
    try:
        state = ComponentState(raw_state)
    except ValueError:
        state = ComponentState.UNKNOWN
    observed = value.get("observed_at")
    try:
        at = datetime.fromisoformat(str(observed))
    except (TypeError, ValueError):
        at = datetime.now().astimezone()
    raw_children = value.get("children")
    children = tuple(
        component_from_dict(item)
        for item in (raw_children if isinstance(raw_children, list) else [])
        if isinstance(item, dict)
    )
    return ComponentNode(
        id=str(value.get("id") or fallback_id),
        name=str(value.get("name") or fallback_id),
        state=state,
        reason=str(value.get("reason") or ""),
        observed_at=at,
        priority=str(value.get("priority") or "normal"),
        metrics=dict(value.get("metrics") or {}),
        children=children,
    )
