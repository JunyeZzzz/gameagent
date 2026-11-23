"""Unit-cost routes on the traversable map exposed by a game adapter."""
from collections import deque
import json
import math


def route_options(start, neighbors, landmarks):
    """Map each label to distance and first action, or None when unreachable.

    Nodes are hashable positions or poses. neighbors(node) yields (action ID,
    next node). Each landmark names a collection of acceptable arrival nodes.
    An adapter can describe a place to stand or a pose facing an interactable.
    This read-only search neither chooses a landmark nor performs an action.
    """
    goals = {label: tuple(nodes) for label, nodes in landmarks.items()}
    remaining = {node for nodes in goals.values() for node in nodes}
    reached = {start: (0, None)}
    remaining.discard(start)
    queue = deque([start])
    while queue and remaining:
        node = queue.popleft()
        distance, first = reached[node]
        for action, child in neighbors(node):
            if child in reached:
                continue
            reached[child] = (distance + 1, action if distance == 0 else first)
            remaining.discard(child)
            queue.append(child)
    options = {}
    for label, nodes in goals.items():
        choices = (reached[node] for node in nodes if node in reached)
        best = min(choices, key=lambda value: value[0], default=None)
        options[label] = {"distance": best[0], "first_action": best[1]} if best else None
    return options


def route_actions(routes, actions):
    """Bind one already available movement to a destination in this revision."""
    if not isinstance(routes, dict):
        return []
    native_ids = {action["id"] for action in actions}
    result = []
    for target, route in routes.items():
        if not isinstance(target, str) or not isinstance(route, dict):
            continue
        primitive, distance = route.get("first_action"), route.get("distance")
        if not isinstance(primitive, str) or primitive not in native_ids:
            continue
        if type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0:
            continue
        identity = "route:" + json.dumps([target, primitive], ensure_ascii=False, separators=(",", ":"))
        if identity in native_ids:
            continue
        result.append({"id": identity, "description": f"向 {target} 按当前路线执行一步 {primitive}",
                       "target": target, "primitive": primitive})
    return result
