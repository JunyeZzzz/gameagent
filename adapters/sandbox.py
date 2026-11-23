"""Tiny local world. Only this adapter knows what advancing a marker means."""
import json
import time

from environment import write_json


def world(root):
    return json.loads((root / "fixture.json").read_text(encoding="utf-8"))


def observe(root):
    state = world(root)
    position = state["position"]
    target = state["destination"]
    reason = "paused" if state.get("paused") else "goal_completed" if position == target else None
    return {"ready": reason is None, "reason": reason,
            "session": state["session"], "revision": str(state["revision"]),
            "goal": f"Move the marker to position {target}.",
            "observed_at": time.time(), "valid_for": 30, "facts": {"position": position, "destination": target},
            "actions": [] if position == target else [{"id": "advance", "description": "Move one step toward the destination"}],
            "last_result": state.get("last_result")}


def act(root, intent):
    state = world(root)
    receipt = {"id": intent["id"], "session": intent["session"]}
    if (state.get("paused") or state["session"] != intent["session"] or
            str(state["revision"]) != intent["revision"] or intent["action"] != "advance" or
            state["position"] == state["destination"]):
        return {**receipt, "status": "rejected", "reason": "world_changed"}
    state["position"] += 1 if state["position"] < state["destination"] else -1
    state["revision"] += 1
    receipt.update(status="completed", position=state["position"])
    state["last_result"] = receipt
    write_json(root / "fixture.json", state)
    return receipt
