"""Present Gymnasium Taxi-v4 as a replayable GameAgent environment."""

import json
import time

import gymnasium as gym

from environment import write_json
from navigation import route_options


ACTIONS = ("south", "north", "east", "west", "pickup", "dropoff")
STATIONS = ("R", "G", "Y", "B")
MOVES = (("south", 1, 0), ("north", -1, 0), ("east", 0, 1), ("west", 0, -1))
GOAL = "到乘客所在站点接人，再送到车上乘客当前指定的站点并下客。"


def read_episode(root):
    return json.loads((root / "episode.json").read_text(encoding="utf-8"))


def restore(root):
    run = read_episode(root)
    settings = {key: run[key] for key in ("is_rainy", "rainy_probability",
                                         "fickle_passenger", "fickle_probability") if key in run}
    env = gym.make("Taxi-v4", **settings)
    try:
        state, _ = env.reset(seed=run["seed"])
        for record in run["steps"]:
            state, reward, terminated, truncated, _ = env.step(ACTIONS.index(record["action"]))
            actual = (int(state), int(reward), bool(terminated), bool(truncated))
            expected = tuple(record[key] for key in ("native_state", "reward", "terminated", "truncated"))
            if actual != expected:
                raise ValueError("Taxi native replay diverged from the action journal")
        return run, env, state
    except BaseException:
        env.close()
        raise


def view(native, state):
    row, column, passenger, destination = native.decode(state)
    return {
        "taxi_rc": [int(row), int(column)],
        "passenger": "onboard" if passenger == 4 else STATIONS[passenger],
        "destination": STATIONS[destination],
    }


def neighbors(native, point):
    row, column = point
    for action, dr, dc in MOVES:
        next_row, next_column = row + dr, column + dc
        if not (0 <= next_row < 5 and 0 <= next_column < 5):
            continue
        if dc and native.desc[row + 1, 2 * max(column, next_column)] == b"|":
            continue
        yield action, (next_row, next_column)


def remaining_goal(facts):
    return facts["destination"] if facts["passenger"] == "onboard" else facts["passenger"]


def choices(native, facts):
    point = tuple(facts["taxi_rc"])
    actions = [{"id": action, "description": f"向 {action} 开一格，正常车道为 {target}"}
               for action, target in neighbors(native, point)]
    station = remaining_goal(facts)
    if point == native.locs[STATIONS.index(station)]:
        action = "dropoff" if facts["passenger"] == "onboard" else "pickup"
        actions.append({"id": action, "description": f"在 {station} 站执行 {action}"})
    return actions


def end_reason(run):
    last = (run["steps"] or [{}])[-1]
    if last.get("terminated"):
        return "goal_completed" if last["reward"] > 0 else "native_end"
    if last.get("truncated"):
        return "native_truncated"
    if len(run["steps"]) >= run.get("limit", 60):
        return "test_step_limit"
    return None


def observe(root):
    run, env, state = restore(root)
    try:
        native = env.unwrapped
        facts = view(native, state)
        stations = dict(zip(STATIONS, native.locs))
        revision = str(len(run["steps"]))
        reason = end_reason(run)
        result = {
            "ready": reason is None, "reason": reason, "session": run["name"], "revision": revision,
            "goal": GOAL, "observed_at": time.time(), "valid_for": 30,
            "facts": dict(facts, step=len(run["steps"]), stations_rc=stations,
                          map_rows=[b"".join(row).decode("ascii") for row in native.desc],
                          map_legend="坐标是从 0 开始的 [行, 列]，上北下南，竖线阻断东西通行。",
                          is_rainy=run.get("is_rainy", False)),
            "actions": choices(native, facts) if reason is None else [],
            "last_result": run.get("uncommitted_receipt") or (run["steps"] or [None])[-1],
        }
        if reason is None and run.get("routes", True):
            station = remaining_goal(facts)
            label = ("送客 " if facts["passenger"] == "onboard" else "接客 ") + station
            result["routes"] = route_options(tuple(facts["taxi_rc"]), lambda point: neighbors(native, point),
                                             {label: (stations[station],)})
            # Opt-in: drive to the station in one decision; the runtime replans after every native step.
            result["travel"] = run.get("travel") is True
        return result
    finally:
        env.close()


def reconcile_pending(root, intent):
    run = read_episode(root)
    if intent["session"] != run["name"]:
        return None
    existing = next((step for step in reversed(run["steps"]) if step["id"] == intent["id"]), None)
    if existing:
        return existing
    if intent["revision"] != str(len(run["steps"])):
        return None
    proof = dict(id=intent["id"], session=run["name"], action=intent["action"],
                 status="failed", reason="action_not_committed")
    run["uncommitted_receipt"] = proof
    write_json(root / "episode.json", run)
    return proof


def act(root, intent):
    run, env, state = restore(root)
    try:
        action = intent["action"]
        existing = next((step for step in reversed(run["steps"]) if step["id"] == intent["id"]), None)
        if existing and intent["session"] == run["name"]:
            return existing
        before = view(env.unwrapped, state)
        identity = dict(id=intent["id"], session=intent["session"], action=action)
        if (end_reason(run) or (intent["session"], intent["revision"]) != (run["name"], str(len(run["steps"])))
                or action not in {candidate["id"] for candidate in choices(env.unwrapped, before)}):
            return dict(identity, status="rejected", reason="world_changed")
        state, reward, terminated, truncated, _ = env.step(ACTIONS.index(action))
        receipt = dict(identity, status="completed", before=before, after=view(env.unwrapped, state),
                       native_state=int(state), reward=int(reward),
                       terminated=bool(terminated), truncated=bool(truncated))
        run["steps"].append(receipt)
        run.pop("uncommitted_receipt", None)
        write_json(root / "episode.json", run)
        return receipt
    finally:
        env.close()
