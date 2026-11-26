"""Unit-cost routes on the traversable map exposed by a game adapter."""
from collections import deque
import json
import math


def route_options(start, neighbors, landmarks, *, with_path=False):
    """Map each label to distance and first action, or None when unreachable.

    Nodes are hashable positions or poses. neighbors(node) yields (action ID,
    next node). Each landmark names a collection of acceptable arrival nodes.
    An adapter can describe a place to stand or a pose facing an interactable.
    With path enabled, each reachable result also includes its steps and arrival node.
    """
    goals = {label: tuple(nodes) for label, nodes in landmarks.items()}
    remaining = {node for nodes in goals.values() for node in nodes}
    reached = {start: (0, None)}
    parents = {} if with_path else None
    remaining.discard(start)
    queue = deque([start])
    while queue and remaining:
        node = queue.popleft()
        distance, first = reached[node]
        for action, child in neighbors(node):
            if child in reached:
                continue
            reached[child] = (distance + 1, action if distance == 0 else first)
            if parents is not None:
                parents[child] = (node, action)
            remaining.discard(child)
            queue.append(child)
    options = {}
    for label, nodes in goals.items():
        available = ((reached[node][0], node) for node in nodes if node in reached)
        nearest = min(available, key=lambda entry: entry[0], default=None)
        if nearest is None:
            options[label] = None
            continue
        _, arrival = nearest
        distance, first = reached[arrival]
        route = {"distance": distance, "first_action": first}
        if parents is not None:
            steps = []
            cursor = arrival
            while cursor != start:
                cursor, action = parents[cursor]
                steps.append(action)
            route.update(steps=tuple(reversed(steps)), arrival=arrival)
        options[label] = route
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
        result.append({"id": identity, "numbered": True,
                       "description": f"向 {target} 沿当前路线走，剩余 {distance:g} 步，先执行 {primitive}",
                       "target": target, "primitive": primitive})
    return result
