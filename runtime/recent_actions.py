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
        if not receipt or receipt.get("parent_id") or receipt.get("session") != self.session or receipt.get("awaiting_effect"):
            return
        control = receipt.get("interruption")
        outcome = receipt.get("effect_outcome") or {}
        status = outcome.get("state", receipt.get("status"))
        if status == "partial":
            status = "failed"
        interrupted = status == "cancelled" and isinstance(control, dict) and control.get("confirmed") is True
        evidence = outcome or receipt.get("effect")
        observed_stop = status == "cancelled" and receipt.get("reason") and isinstance(evidence, dict) and bool(evidence)
        if status not in ("completed", "failed") and not (interrupted or observed_stop):
            return
        identity, action = receipt.get("id"), receipt.get("action")
        if not identity or not action:
            return
        row = {"action": action}
        if receipt.get("cheat_method"):
            row["source"] = receipt["cheat_method"]
        verification = receipt.get("verification")
        if isinstance(verification, str) and compact(verification):
            row["verification"] = verification
        if status != "completed":
            row["status"] = status
            reason = outcome.get("reason") or receipt.get("reason")
            if reason:
                row["reason"] = str(reason)[:160]
        if interrupted:
            row["interruption"] = str(control.get("subgoal") or "")[:160]
        if isinstance(evidence, dict):
            details = {k: v for k, v in evidence.items() if compact(v)}
            if details:
                row["effect"] = dict(list(details.items())[:6])
        before, after = receipt.get("before"), receipt.get("after")
        if isinstance(before, dict) and isinstance(after, dict):
            if before == after:
                if "effect" not in row:
                    row["unchanged"] = True
            else:
                changes = {k: v for k, v in after.items() if k in before and v != before[k] and compact(v)}
                if changes:
                    row["changed_to"] = dict(list(changes.items())[:6])
        self.remember(identity, row)

    def record_progress(self, observation):
        pending = observation.get("pending") or {}
        ongoing = observation.get("ongoing") or {}
        feedback = ongoing.get("feedback")
        if (observation.get("session") == self.session and pending.get("id")
                and pending["id"] == ongoing.get("id") and pending.get("action")
                and isinstance(feedback, str) and feedback):
            self.remember(pending["id"], {"action": pending["action"], "progress_feedback": feedback[:160]})

    def remember(self, identity, row):
        for old_id, prior in self.rows:
            if old_id == identity:
                prior.update(row)
                if "effect" in prior or "changed_to" in prior:
                    prior.pop("unchanged", None)
                return
        self.rows.append((identity, row))

    def entries(self):
        return [row for _, row in self.rows]


class CatalogFindings:
    """Remember verified lookups while the same goal and catalog remain current."""

    def __init__(self):
        self.rows = deque(maxlen=8)

    def record(self, result):
        if not result or result.get("status") not in ("found", "no_match", "unavailable_here") or not result.get("key"):
            return
        row = {key: result[key] for key in ("catalog", "query", "key", "revision", "status", "count")}
        identity = (row["catalog"], row["key"])
        for prior in self.rows:
            if (prior["catalog"], prior["key"]) == identity:
                self.rows.remove(prior)
                break
        self.rows.append(row)

    def current(self, view):
        catalogs = view.get("catalogs", {})
        return [row for row in self.rows
                if catalogs.get(row["catalog"], {}).get("revision") == row["revision"]]
