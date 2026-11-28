"""Travel candidates move to a landmark in one decision, one ordinary intent per native step."""
import json
from importlib.util import find_spec
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from environment import adapter_for, dispatch, exchange
from navigation import travel_id


class Corridor:
    """Positions 0..6 on a line with an exit at 5. Hooks alter the world after a number of steps."""
    __name__ = "corridor"

    def __init__(self, travel=True):
        self.position, self.steps, self.travel = 0, [], travel
        self.goal = "Reach the exit"
        self.after_step = {}
        self.crash_at = None
        self.slippery = False

    def observe(self, root):
        distance = 5 - self.position
        view = {"ready": True, "session": "corridor", "revision": str(len(self.steps)), "goal": self.goal,
                "observed_at": time.time(), "valid_for": 10, "facts": {"position": self.position},
                "actions": [{"id": "east"}, {"id": "west"}],
                "routes": {"exit": {"distance": abs(distance),
                                    "first_action": None if distance == 0 else "east" if distance > 0 else "west"}},
                "last_result": self.steps[-1] if self.steps else None}
        if self.travel:
            view["travel"] = True
        return view

    def act(self, root, intent):
        if self.crash_at == len(self.steps):
            raise RuntimeError("worker interrupted during dispatch")
        if not self.slippery:
            self.position += 1 if intent["action"] == "east" else -1
        receipt = dict(id=intent["id"], session=intent["session"], action=intent["action"], status="completed",
                       before={"position": self.position}, after={"position": self.position})
        self.steps.append(receipt)
        self.after_step.get(len(self.steps), lambda: None)()
        return receipt


class TravelTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.adapter = Corridor()

    def travel(self, **changes):
        view = exchange(self.adapter, self.root)
        candidate = next(a for a in view["actions"] if a["id"] == travel_id("exit"))
        request = dict(id=str(uuid.uuid4()), action=candidate["id"], session=view["session"], goal=view["goal"],
                       revision=view["revision"], expires_at=view["observed_at"] + view["valid_for"], **changes)
        return request, dispatch(self.adapter, self.root, request)

    def test_one_decision_reaches_the_landmark_with_a_receipt_per_step(self):
        request, receipt = self.travel()
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["travel"], dict(target="exit", native_steps=5, native_actions=["east"] * 5,
                                                 stopped="arrived"))
        self.assertEqual(self.adapter.position, 5)
        ids = [step["id"] for step in self.adapter.steps]
        self.assertEqual(ids, [request["id"]] + [f'{request["id"]}:{n}' for n in range(1, 5)])
        self.assertFalse(list(self.root.glob(".pending-*")), "every step reached a terminal receipt")
        again = dispatch(self.adapter, self.root, request)
        self.assertEqual(again["status"], "rejected", "a replayed travel cannot move the marker again")
        self.assertEqual(self.adapter.position, 5)

    def test_a_changed_goal_ends_the_travel_for_a_new_decision(self):
        self.adapter.after_step[2] = lambda: setattr(self.adapter, "goal", "Go back")
        _, receipt = self.travel()
        self.assertEqual(receipt["travel"]["stopped"], "goal_changed")
        self.assertEqual(receipt["travel"]["native_steps"], 2)
        self.assertEqual(receipt["travel"]["distance_left"], 3)

    def test_an_interrupted_step_stays_pending_alone(self):
        self.adapter.crash_at = 3
        request, receipt = self.travel()
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["travel"]["stopped"], "step_unknown")
        self.assertEqual(receipt["travel"]["native_steps"], 3)
        pending = json.loads(next(self.root.glob(".pending-*")).read_text())
        self.assertEqual(pending["id"], f'{request["id"]}:3')
        self.assertEqual(exchange(self.adapter, self.root)["actions"][0]["id"], "agent:continue",
                         "the unresolved step blocks new actions until it is reconciled")

    def test_a_route_that_stops_shrinking_hands_back_control(self):
        self.adapter.slippery = True
        _, receipt = self.travel()
        self.assertEqual(receipt["travel"]["stopped"], "no_progress")
        self.assertEqual(receipt["travel"]["native_steps"], 3)

    def test_adapters_that_do_not_opt_in_keep_single_step_routes(self):
        self.adapter.travel = False
        ids = [a["id"] for a in exchange(self.adapter, self.root)["actions"]]
        self.assertIn('route:["exit","east"]', ids)
        self.assertFalse(any(i.startswith("travel:") for i in ids))


def play(test, adapter, root, prefer):
    """Drive an episode with a scripted chooser; returns decisions and native steps."""
    decisions = 0
    for _ in range(40):
        view = exchange(adapter, root)
        if not view["ready"]:
            return view, decisions
        action = prefer(view["actions"])
        intent = dict(id=str(uuid.uuid4()), session=view["session"], revision=view["revision"],
                      action=action["id"], goal=view["goal"], expires_at=view["observed_at"] + view["valid_for"])
        receipt = dispatch(adapter, root, intent)
        test.assertEqual(receipt["status"], "completed", receipt)
        decisions += 1
    test.fail("episode did not end")


def first_of(*prefixes):
    return lambda actions: next(a for p in prefixes for a in actions if a["id"].startswith(p))


@unittest.skipUnless(find_spec("gymnasium"), "Taxi example uses Gymnasium")
class TaxiTravelTest(unittest.TestCase):
    def run_taxi(self, **settings):
        adapter = adapter_for("taxi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "episode.json").write_text(json.dumps(dict(name="taxi-travel", seed=42, limit=60, steps=[],
                                                               routes=True, **settings)), encoding="utf-8")
            final, decisions = play(self, adapter, root, first_of("pickup", "dropoff", "travel:", "route:"))
            steps = json.loads((root / "episode.json").read_text())["steps"]
            return final, decisions, steps

    def test_travel_needs_far_fewer_decisions_for_the_same_native_route(self):
        stepwise, stepwise_decisions, stepwise_steps = self.run_taxi()
        travel, travel_decisions, travel_steps = self.run_taxi(travel=True)
        for final in (stepwise, travel):
            self.assertEqual(final["reason"], "goal_completed")
            self.assertEqual(final["last_result"]["reward"], 20)
        self.assertEqual([s["action"] for s in travel_steps], [s["action"] for s in stepwise_steps])
        self.assertEqual(stepwise_decisions, len(stepwise_steps))
        self.assertEqual(travel_decisions, 4, "drive, pick up, drive, drop off")

    def test_rain_is_corrected_by_replanning_after_each_step(self):
        final, decisions, steps = self.run_taxi(travel=True, is_rainy=True)
        self.assertEqual(final["reason"], "goal_completed")
        self.assertLess(decisions, len(steps))


@unittest.skipUnless(find_spec("minigrid"), "MiniGrid example dependencies are optional")
class GridTravelTest(unittest.TestCase):
    def episode(self, **settings):
        adapter = adapter_for("doorworld")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "episode.json").write_text(json.dumps(dict(name="grid-travel", limit=60, routes=True, steps=[],
                                                           **settings)), encoding="utf-8")
        return adapter, root

    def test_door_key_level_in_a_handful_of_decisions(self):
        adapter, root = self.episode(game="MiniGrid-DoorKey-5x5-v0", seed=1, travel=True)
        final, decisions = play(self, adapter, root, first_of("unlock_door:", 'travel:"stand on goal'))
        steps = json.loads((root / "episode.json").read_text())["steps"]
        self.assertEqual(final["reason"], "native_end")
        self.assertGreater(final["last_result"]["reward"], 0)
        self.assertLess(decisions, len(steps))

    def test_moving_obstacles_keep_one_decision_per_step(self):
        adapter, root = self.episode(game="MiniGrid-Dynamic-Obstacles-6x6-v0", seed=29, travel=True)
        ids = [a["id"] for a in exchange(adapter, root)["actions"]]
        self.assertFalse(any(i.startswith("travel:") for i in ids))


if __name__ == "__main__":
    unittest.main()
