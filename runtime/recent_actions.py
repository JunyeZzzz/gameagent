"""A bounded, session-scoped account of what the game confirmed."""
from collections import deque


def compact(value):
    if isinstance(value, list):
        return len(value) <= 4 and all(isinstance(x, (bool, int, float)) for x in value)
    return value is None or isinstance(value, (bool, int, float)) or isinstance(value, str) and len(value) <= 80


class RecentActions:
    def __init__(self):
        self.session = None
        self.rows = deque(maxlen=8)

    def select(self, session):
        if self.session != session:
            self.session = session
            self.rows.clear()

    def record(self, receipt):
        if not receipt or receipt.get("status") != "completed" or receipt.get("session") != self.session:
            return
        identity, action = receipt.get("id"), receipt.get("action")
        if not identity or not action or any(old_id == identity for old_id, _ in self.rows):
            return
        row = {"action": action}
        before, after = receipt.get("before"), receipt.get("after")
        if isinstance(before, dict) and isinstance(after, dict):
            if before == after:
                row["unchanged"] = True
            else:
                changes = {k: v for k, v in after.items() if k in before and v != before[k] and compact(v)}
                if changes:
                    row["changed_to"] = dict(list(changes.items())[:6])
        self.rows.append((identity, row))

    def entries(self):
        return [row for _, row in self.rows]
