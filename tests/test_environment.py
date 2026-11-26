"""A second environment with unrelated facts and actions exercises the same boundary."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

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

    def test_adapter_can_reconcile_uncommitted_action_before_resuming(self):
        self.adapter.outcome = "crash"
        self.assertEqual(self.submit()["status"], "unknown")
        restored = self.adapter = Switchboard()
        original = dict(id="first", session="panel-5", status="failed", reason="action_not_committed")
        restored.reconcile_pending = lambda root, intent: dict(original, session="other")
        self.assertIn("pending", exchange(restored, self.root))
        restored.reconcile_pending = lambda root, intent: dict(original, status="unknown")
        self.assertIn("pending", exchange(restored, self.root))

        def reconcile(root, intent):
            self.assertEqual(intent["id"], "first")
            restored.view["last_result"] = original
            return original

        restored.reconcile_pending = reconcile
        view = exchange(restored, self.root)
        self.assertEqual(view["last_result"], original)
        self.assertNotIn("pending", view)
        self.assertEqual(self.submit(), original)
        self.assertEqual(restored.calls, [])
        self.assertEqual(self.submit(id="new")["status"], "completed")
        self.assertEqual(restored.calls, ["new"])

    def test_new_session_releases_only_local_ownership(self):
        self.adapter.outcome = "accepted"
        self.submit()
        self.adapter.view["session"] = "new-world"
        view = exchange(self.adapter, self.root)
        self.assertNotIn("pending", view)
        self.assertEqual(self.submit(id="old-response", action="agent:continue", parent_id="first")["reason"],
                         "parent_ended")
        self.assertTrue((self.root / ".detached-switchboard.json").exists())

    def test_accepted_waits_for_outcome(self):
        self.adapter.outcome = "accepted"
        self.assertEqual(self.submit()["status"], "accepted")
        self.assertEqual([a["id"] for a in exchange(self.adapter, self.root)["actions"]], ["agent:continue"])
        self.assertEqual(self.submit(id="second")["reason"], "unresolved_action")

    def test_running_action_rejects_previous_goal_control_with_same_text_and_revision(self):
        self.adapter.view["goal_id"] = "old-command"
        self.adapter.outcome = "running"
        parent = self.submit(goal_id="old-command")
        self.assertEqual(parent["status"], "running")
        self.adapter.view.update(ongoing={"id": parent["id"]}, actions=[{"id": "review:keep"}])
        self.adapter.review = Mock(side_effect=lambda root, request: dict(request, status="accepted"))
        control = dict(self.request, parent_id=parent["id"], action="review:keep", goal=self.adapter.view["goal"])
        self.adapter.view["goal_id"] = "new-command"

        stale = exchange(self.adapter, self.root, dict(control, id="old-control", goal_id="old-command"))
        self.assertEqual(stale, {"status": "rejected", "reason": "observation_changed"})
        self.adapter.review.assert_not_called()
        self.assertEqual(exchange(self.adapter, self.root)["pending"]["id"], parent["id"])

        current = dict(control, id="new-control", goal_id="new-command")
        self.assertEqual(exchange(self.adapter, self.root, current)["status"], "accepted")
        self.adapter.review.assert_called_once_with(self.root, current)


    def test_route_choice_binds_one_current_action_and_is_idempotent(self):
        self.adapter.view["routes"] = {"signal": {"distance": 2, "first_action": "toggle.blue"}}
        offered = exchange(self.adapter, self.root)["actions"][-1]
        self.assertEqual(offered["primitive"], "toggle.blue")
        self.assertTrue(offered["numbered"])
        receipt = self.submit(action=offered["id"])
        self.assertEqual(self.adapter.last_action, "toggle.blue")
        self.assertEqual(self.submit(action=offered["id"]), receipt)
        self.assertEqual(self.adapter.calls, ["first"])
        self.adapter.view["actions"].append({"id":"other"})
        self.adapter.view["routes"]["signal"] = {"distance": 1, "first_action":"other"}
        self.assertEqual(self.submit(id="later", action=offered["id"])["status"], "rejected")
        self.assertEqual(self.adapter.calls, ["first"])

    def test_live_candidate_relations_are_scoped_to_the_current_situation_and_parent(self):
        self.adapter.outcome = "running"
        self.assertEqual(self.submit()["status"], "running")
        self.adapter.view.update(ongoing={"id": "first", "activity": "transport"},
                                 actions=[{"id": "scan", "relation": "parallel"}, {"id": "shelter", "relation": "replace"}],
                                 situation={"key": "open"})

        def review(root, request):
            if request["action"] == "shelter":
                self.adapter.view["ongoing"]["activity"] = "shelter"
            return dict(request, status="completed")

        self.adapter.review = Mock(side_effect=review)
        self.adapter.view["situation"] = {"key": "closing"}
        self.assertEqual(self.submit(id="stale", parent_id="first", action="scan", situation_key="open")["status"], "rejected")
        self.adapter.review.assert_not_called()

        for action, relation, activity in [("scan", "parallel", "transport"), ("shelter", "replace", "shelter")]:
            request = dict(self.request, id=action, parent_id="first", action=action, situation_key="closing")
            receipt = exchange(self.adapter, self.root, request)
            self.assertEqual((receipt["status"], receipt["relation"]), ("completed", relation))
            self.assertEqual(exchange(self.adapter, self.root, request)["relation"], relation)
            view = exchange(self.adapter, self.root)
            self.assertEqual((view["pending"]["id"], view["ongoing"]["activity"]), ("first", activity))
        self.assertEqual(self.adapter.calls, ["first"])
        self.assertEqual(self.adapter.review.call_count, 2)

    def test_routes_use_directed_edges_and_multiple_arrival_nodes(self):
        graph = {"home":[("lift","roof"),("east","hall")], "roof":[], "hall":[("north","exit")], "exit":[]}
        choices = route_options("home", graph.__getitem__, {"entrance":["home"], "outside":["exit"],
                                "meeting":["exit","roof"], "basement":["cellar"]})
        self.assertEqual(choices, {"entrance":{"distance":0,"first_action":None}, "outside":{"distance":2,"first_action":"east"},
                                  "meeting":{"distance":1,"first_action":"lift"}, "basement":None})

    def test_history_keeps_observed_effect_of_confirmed_interruption_once(self):
        history = RecentActions()
        history.select("one")
        receipt = dict(id="turn", session="one", action="travel", status="cancelled",
                       effect={"distance": 20, "unbounded": "x" * 200})
        history.record(receipt)
        history.record({**receipt, "interruption": {"confirmed": False}})
        self.assertEqual(history.entries(), [])
        receipt["interruption"] = {"confirmed": True, "subgoal": "arrived close enough"}
        history.record(receipt)
        history.record(receipt)
        self.assertEqual(history.entries(), [{"action": "travel", "status": "cancelled",
                                              "interruption": "arrived close enough", "effect": {"distance": 20}}])
        history.record(dict(id="second", session="one", action="travel", status="failed", reason="blocked",
                            effect={"distance": 15}))
        self.assertEqual(history.entries()[-1], {"action":"travel", "status":"failed", "reason":"blocked",
                                                "effect":{"distance":15}})
        history.record(dict(id="third", session="one", action="toggle", status="completed", verification="input_only"))
        self.assertEqual(history.entries()[-1], {"action":"toggle", "verification":"input_only"})
        stopped = dict(id="fourth", session="one", action="travel", status="cancelled", reason="stopped",
                       effect={"distance": 4})
        history.record(stopped)
        self.assertNotIn("interruption", history.entries()[-1])
        history.record(dict(stopped, interruption={"confirmed":True, "subgoal":"change route"}))
        self.assertEqual(len(history.entries()), 4)
        self.assertEqual(history.entries()[-1]["interruption"], "change route")

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

    def test_history_keeps_progress_evidence_after_action_ends_and_target_changes(self):
        history = RecentActions()
        history.select("one")
        pending = dict(id="original", action="follow:old")
        observation = dict(session="one", pending=pending,
                           ongoing=dict(id="original", feedback="The lead stopped; the vehicle is maintaining its gap."))
        history.record_progress(observation)
        history.record_progress(observation)
        history.record(dict(pending, session="one", status="cancelled", reason="targetUnavailable",
                            effect={"travelled_m": 15}))
        history.record(dict(id="other", session="one", action="follow:new", status="completed"))
        self.assertEqual(len(history.entries()), 2)
        self.assertEqual(history.entries()[0]["progress_feedback"], observation["ongoing"]["feedback"])
        self.assertEqual(history.entries()[0]["reason"], "targetUnavailable")


if __name__ == "__main__":
    unittest.main()
