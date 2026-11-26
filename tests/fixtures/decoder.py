"""A stationary decoder with an independently signalled game completion."""
import json
import time
from environment import write_json


def observe(root):
    started = root / "decoding.json"
    last = json.loads(started.read_text()) if started.exists() else None
    finished = (root / "decoded").exists()
    claimed = (root / "claimed").exists()
    if last:
        last = dict(last, status="completed" if finished else "running")
    rest = root / "rest"
    if rest.exists():
        arrival = rest.read_text()
        last = dict(id="second-arrival" if arrival == "arrived again" else "arrival", session="terminal", action="arrive",
                    status="completed" if arrival.startswith("arrived") else "failed")
        if arrival in ("external stop", "deliberate stop"):
            last.update(id="final-stop",action="follow",status="cancelled",interruption={"confirmed":arrival=="deliberate stop"})
    revision = "claimed" if claimed else "decoded" if finished else "busy" if last else "idle"
    return dict(ready=not claimed, reason="goal_completed" if claimed else None,
                session="terminal", revision=revision, observed_at=time.time(), valid_for=30,
                goal=("Rest here again" if (root/"again").exists() else "Rest here once") if rest.exists()
                     else "Decode the message and collect it", facts={"decoded":finished},
                completion_checks=[{"field":"rested","met":False}] if (root/"unsettled").exists() else [],
                blocked_capabilities=[dict(id="input",reason="busy",resumes_when="input released",wake_on=[]) ] if (root/"locked").exists() else [],
                actions=[{"id":"collect" if finished else "decode"}], last_result=last)


def act(root, intent):
    if intent["action"] == "decode":
        write_json(root / "decoding.json", intent)
        return dict(intent, status="accepted")
    (root / "claimed").touch()
    return dict(intent, status="completed")


def cancel(root, parent, request):
    (root / "cancelled").touch()
    return dict(request, status="completed")
