"""The last accepted task interpretation for one game, goal and instruction."""

import json
import math
import time

from environment import write_json


FIELDS = {"intent": 160, "expected": 160, "revisit": 160, "finish_when": 160}


def strategy(value):
    if not isinstance(value, dict):
        return None
    brief = {}
    for key, limit in FIELDS.items():
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            return None
        brief[key] = text.strip()
    return brief


class TaskState:
    def __init__(self, path):
        self.path = path
        self.record = None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (isinstance(record, dict) and record.get("version") == 1
                and isinstance(record.get("scope"), list) and len(record["scope"]) == 3
                and strategy(record.get("brief"))):
            deadline = record.get("review_at")
            if deadline is None or type(deadline) in (int, float) and math.isfinite(deadline):
                record["brief"] = strategy(record["brief"])
                self.record = record

    def has_deadline(self):
        return self.record is not None and self.record.get("review_at") is not None

    def select(self, scope):
        if self.record and self.record["scope"] != list(scope):
            self.path.unlink(missing_ok=True)
            self.record = None

    def due(self):
        return self.has_deadline() and time.time() >= self.record["review_at"]

    def view(self):
        if self.record is None:
            return None
        now = time.time()
        deadline = self.record.get("review_at")
        view = {**self.record["brief"], "review_due": deadline is not None and now >= deadline}
        if deadline is not None:
            view["review_remaining_s"] = round(max(0, deadline - now), 2)
            start = self.record.get("updated_at")
            if type(start) in (int, float) and math.isfinite(start):
                view["review_elapsed_s"] = round(max(0, now - start), 2)
        return view
        return None

    def update(self, scope, run_id, receipt):
        if receipt.get("status") not in ("observing", "accepted", "running", "completed"):
            return False
        proposed = receipt.get("task")
        brief = strategy(proposed)
        if brief is None:
            return False
        seconds = receipt.get("recheck_after_s") if receipt["status"] == "observing" else None
        if seconds is None:
            seconds = proposed.get("recheck_after_s")
        if seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 600):
            return False
        at = time.time()
        record = dict(version=1, scope=list(scope), brief=brief, updated_at=at, run_id=run_id,
                      action_id=receipt.get("id"), review_at=at+seconds if seconds else None)
        write_json(self.path, record)
        self.record = record
        return True

    def clear_deadline(self):
        if self.has_deadline():
            record = dict(self.record, review_at=None)
            write_json(self.path, record)
            self.record = record
