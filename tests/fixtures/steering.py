"""A running actuator with a separately acknowledged course adjustment."""
import json
import time
from environment import write_json


def observe(root):
    drive = root / "drive.json"
    control = root / "control.json"
    goal = (root / "goal.txt").read_text() if (root / "goal.txt").exists() else "Start moving"
    parent = json.loads(drive.read_bytes()) if drive.exists() else None
    if parent and parent["status"] == "accepted" and not (root / "hold_start").exists():
        parent["status"] = "running"
    review = json.loads(control.read_bytes()) if control.exists() else None
    if review and (root / (review["id"] + ".ack")).exists():
        review["status"] = "completed"
    accepted = review and review["status"] == "accepted"
    settling = review and review["status"] == "completed" and (root / "settling").exists()
    action = "review:steer" if parent else "drive"
    situation = root / "situation.json"
    return dict(ready=True, session="track", revision="waiting" if accepted or settling else action,
                observed_at=time.time(), valid_for=30, goal=goal, goal_id=goal,
                **({"situation": json.loads(situation.read_bytes())} if situation.exists() else {}),
                **({"direct_user_action": action} if goal == "Set course now" and parent
                   and parent["status"] == "running" and not accepted and not settling else {}),
                actions=[] if accepted or settling else [{"id":action}], last_result=parent, last_review=review,
                ongoing=dict(id=parent["id"], trigger="settling" if settling else "moving",
                             control_effect_pending=bool(settling)) if parent else None)


def act(root, request):
    receipt = dict(request, status="accepted" if (root / "hold_start").exists() else "running")
    write_json(root / "drive.json", receipt)
    return receipt


def review(root, request):
    receipt = dict(request, status="accepted")
    write_json(root / "control.json", receipt)
    return receipt
