"""A second environment with unrelated facts and actions exercises the same boundary."""
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from environment import exchange
from navigation import route_options
from recent_actions import RecentActions


class Switchboard:
    __name__ = "switchboard"

    def __init__(self):
        self.view = {"ready": True, "session": "panel-5", "revision": "off", "goal": "Light the signal",
                     "actions": [{"id": "toggle.blue", "description": "Toggle the blue switch"}],
                     "facts": {"signal": False}, "observed_at": time.time(), "valid_for": 10}
        self.calls = []
        self.outcome = "completed"

    def observe(self, root):
        return dict(self.view)

    def act(self, root, request):
        self.calls.append(request["id"])
        self.last_action = request["action"]
        if self.outcome == "crash":
            raise RuntimeError("interrupted after dispatch")
        result = {"status": self.outcome, "id": request["id"], "session": request["session"]}
        self.view["last_result"] = result
        return result


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.adapter = Switchboard()
        self.request = {"session": "panel-5", "revision": "off", "action": "toggle.blue",
                        "id": "first", "expires_at": time.time() + 10}

    def submit(self, **changes):
        return exchange(self.adapter, self.root, {**self.request, **changes})

    def test_unrelated_action_and_repeat_receipt(self):
        self.assertEqual(self.submit()["status"], "completed")
        self.assertEqual(self.submit()["status"], "completed")
        self.assertEqual(self.adapter.calls, ["first"])

    def test_invalid_or_stale_basis(self):
        for changes in ({"action": "missing"}, {"revision": "old"}, {"session": "old"}, {"expires_at": 0}):
            self.assertEqual(self.submit(**changes)["status"], "rejected")
        self.adapter.view.update(observed_at=time.time()-60)
        self.assertEqual(self.submit()["status"], "rejected")
        self.adapter.view.update(ready=False, reason="paused")
        self.assertEqual(self.submit()["status"], "rejected")
        self.assertEqual(self.adapter.calls, [])

    def test_pending_survives_restart_and_needs_matching_evidence(self):
        self.adapter.outcome = "crash"
        self.assertEqual(self.submit()["status"], "unknown")
        self.adapter = Switchboard()
        for identity, session in (("other", "panel-5"), ("first", "different")):
            self.adapter.view["last_result"] = {"id": identity, "session": session, "status": "completed"}
            self.assertEqual(self.submit(id="second")["reason"], "unresolved_action")
        self.assertEqual(self.adapter.calls, [])
        self.adapter.view["last_result"] = {"id": "first", "session": "panel-5", "status": "failed"}
        self.assertNotIn("pending", exchange(self.adapter, self.root))
        self.assertEqual(self.submit(id="second")["status"], "completed")

    def test_accepted_waits_for_outcome(self):
        self.adapter.outcome = "accepted"
        self.assertEqual(self.submit()["status"], "accepted")
        self.assertEqual(exchange(self.adapter, self.root)["actions"], [])
        self.assertEqual(self.submit(id="second")["reason"], "unresolved_action")


    def test_route_choice_binds_one_current_action_and_is_idempotent(self):
        self.adapter.view["routes"] = {"signal": {"distance": 2, "first_action": "toggle.blue"}}
        offered = exchange(self.adapter, self.root)["actions"][-1]
        self.assertEqual(offered["primitive"], "toggle.blue")
        receipt = self.submit(action=offered["id"])
        self.assertEqual(self.adapter.last_action, "toggle.blue")
        self.assertEqual(self.submit(action=offered["id"]), receipt)
        self.assertEqual(self.adapter.calls, ["first"])
        self.adapter.view["actions"].append({"id":"other"})
        self.adapter.view["routes"]["signal"] = {"distance": 1, "first_action":"other"}
        self.assertEqual(self.submit(id="later", action=offered["id"])["status"], "rejected")
        self.assertEqual(self.adapter.calls, ["first"])

    def test_routes_use_directed_edges_and_multiple_arrival_nodes(self):
        graph = {"home":[("lift","roof"),("east","hall")], "roof":[], "hall":[("north","exit")], "exit":[]}
        choices = route_options("home", graph.__getitem__, {"entrance":["home"], "outside":["exit"],
                                "meeting":["exit","roof"], "basement":["cellar"]})
        self.assertEqual(choices, {"entrance":{"distance":0,"first_action":None}, "outside":{"distance":2,"first_action":"east"},
                                  "meeting":{"distance":1,"first_action":"lift"}, "basement":None})

    def test_history_uses_session_and_confirmed_changes(self):
        history = RecentActions()
        history.select("one")
        receipt = {"id":"first","session":"one","action":"switch","before":{"count":1},"after":{"count":2}}
        history.record({**receipt,"status":"accepted"})
        self.assertEqual(history.entries(), [])
        history.record({**receipt,"status":"completed"})
        history.record({**receipt,"status":"completed"})
        self.assertEqual(history.entries(), [{"action":"switch","changed_to":{"count":2}}])
        history.select("two")
        history.record({**receipt,"status":"completed"})
        self.assertEqual(history.entries(), [])


if __name__ == "__main__":
    unittest.main()
