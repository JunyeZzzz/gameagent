"""Exercise a grouped input through the normal environment boundary and native MiniGrid."""
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from environment import adapter_for, exchange, write_json


def request(state, action, identity):
    return dict(id=identity, session=state["session"], revision=state["revision"],
                action=action, expires_at=time.time()+30)


@unittest.skipUnless(find_spec("minigrid"), "MiniGrid example dependencies are optional")
class DynamicObstacleTest(unittest.TestCase):
    def test_offers_native_inputs_and_reports_terminal_collision(self):
        adapter = adapter_for("doorworld")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = dict(name="dynamic", game="MiniGrid-Dynamic-Obstacles-6x6-v0", seed=12,
                          limit=60, routes=True, steps=[])
            write_json(root / "episode.json", config)
            state = exchange(adapter, root)
            actions = {a["id"]: a for a in state["actions"] if not a["id"].startswith("route:")}
            self.assertEqual(set(actions), {"left", "right", "forward"})
            self.assertTrue(state["facts"]["obstacles_move_each_action"])
            self.assertEqual(state["facts"]["front_tile"], "b")
            self.assertEqual(actions["forward"]["immediate_effect"]["outcome"], "episode ends in failure")

            before = (root / "episode.json").read_bytes()
            unavailable = adapter.act(root, request(state, "pickup", "not-supported"))
            self.assertEqual((unavailable["status"], unavailable["reason"]), ("rejected", "action_unavailable"))
            self.assertEqual((root / "episode.json").read_bytes(), before)

            result = exchange(adapter, root, request(state, "forward", "native-collision"))
            self.assertTrue(result["terminated"])
            self.assertEqual(result["reward"], -1)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "episode.json", dict(config, seed=17))
            state = exchange(adapter, root)
            effect = next(a["immediate_effect"] for a in state["actions"] if a["id"] == "forward")
            self.assertEqual(effect["tile_ahead"], ".")
            self.assertNotIn("xy_after", effect)


@unittest.skipUnless(find_spec("minigrid"), "MiniGrid example dependencies are optional")
class PlacementPickupTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.episode = self.root / "episode.json"
        self.adapter = adapter_for("doorworld")
        write_json(self.episode, dict(name="pickup-test", game="MiniGrid-BlockedUnlockPickup-v0",
                                      seed=314, limit=56, routes=True, steps=[]))
        # Reach the open room while carrying the purple key, facing the yellow box.
        before_box = (
            "right forward forward forward right pickup drop right left forward pickup "
            "forward right drop left pickup left toggle forward forward forward forward forward right forward"
        )
        for index, action in enumerate(before_box.split(), 1):
            state = exchange(self.adapter, self.root)
            receipt = exchange(self.adapter, self.root, request(state, action, f"setup-{index}"))
            self.assertEqual(receipt["status"], "completed")
        self.before = exchange(self.adapter, self.root)
        self.assertEqual(self.before["facts"]["inventory"], "purple key")
        self.assertEqual(self.before["facts"]["front_tile"], "B")
        self.choice = next(a for a in self.before["actions"] if a["id"].startswith(self.adapter.PLACE_AND_PICK_UP))
        self.intent = request(self.before, self.choice["id"], "pick-box")

    def test_commit_and_recover_one_receipt_for_four_native_inputs(self):
        receipt = exchange(self.adapter, self.root, self.intent)
        self.assertEqual(receipt["id"], self.intent["id"])
        self.assertEqual(receipt["action"], self.choice["id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["native_actions"], ["right", "drop", "left", "pickup"])
        self.assertEqual(receipt["before"]["inventory"], "purple key")
        self.assertEqual(receipt["after"]["inventory"], "yellow box")
        self.assertEqual(receipt["after"]["map_rows"][3][8], "K")
        self.assertTrue(receipt["terminated"])
        self.assertGreater(receipt["reward"], 0)

        # A committed group can be recovered when the worker stopped before receiving it.
        pending = self.root / ".pending-doorworld.json"
        write_json(pending, self.intent)
        self.assertEqual(exchange(self.adapter, self.root)["last_result"], receipt)
        self.assertFalse(pending.exists())
        self.assertEqual(exchange(self.adapter, self.root, self.intent), receipt)
        run, env, _ = self.adapter.restore(self.root)
        env.close()
        self.assertEqual(len(run["steps"]), int(self.before["revision"]) + 4)
        self.assertEqual([s["action"] for s in run["steps"][-4:]], receipt["native_actions"])
        self.assertEqual(len({s["id"] for s in run["steps"]}), len(run["steps"]))

    def test_write_failure_leaves_all_steps_uncommitted(self):
        before = self.episode.read_bytes()
        with patch.object(self.adapter, "write_json", side_effect=OSError("simulated write failure")):
            self.assertEqual(exchange(self.adapter, self.root, self.intent)["status"], "unknown")
        self.assertEqual(self.episode.read_bytes(), before)
        state = exchange(self.adapter, self.root)
        self.assertEqual(state["revision"], self.before["revision"])
        self.assertEqual(state["facts"], self.before["facts"])
        self.assertEqual(state["last_result"]["reason"], "action_not_committed")

    def test_offer_requires_time_for_the_entire_action(self):
        run = json.loads(self.episode.read_text())
        run["limit"] = len(run["steps"]) + 3
        write_json(self.episode, run)
        state = exchange(self.adapter, self.root)
        self.assertNotIn(self.choice["id"], {a["id"] for a in state["actions"]})
        self.assertEqual(exchange(self.adapter, self.root, self.intent)["status"], "rejected")
        self.assertEqual(exchange(self.adapter, self.root)["revision"], self.before["revision"])

    def test_opening_the_mission_box_explains_the_persistent_effect(self):
        toggle = next(a for a in self.before["actions"] if a["id"] == "toggle")
        self.assertIn("mission_outcome", toggle["immediate_effect"])
        receipt = exchange(self.adapter, self.root, request(self.before, "toggle", "open-box"))
        self.assertFalse(receipt["terminated"])
        self.assertEqual(receipt["reward"], 0)
        self.assertEqual(receipt["after"]["front_tile"], ".")


@unittest.skipUnless(find_spec("minigrid"), "MiniGrid example dependencies are optional")
class UnlockDoorTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.episode = self.root / "episode.json"
        self.adapter = adapter_for("doorworld")
        write_json(self.episode, dict(name="door-test", game="MiniGrid-BlockedUnlockPickup-v0",
                                      seed=42, limit=56, routes=True, steps=[]))

    def open_door(self, initial):
        option, = (a for a in initial["actions"] if a["id"].startswith(self.adapter.UNLOCK_DOOR))
        intent = request(initial, option["id"], "open-blocked-door")
        receipt = exchange(self.adapter, self.root, intent)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["id"], intent["id"])
        self.assertEqual(receipt["after"]["front_tile"], "O")
        self.assertEqual(receipt["after"]["inventory"], "red key")
        self.assertEqual(receipt["reward"], 0)
        state = exchange(self.adapter, self.root)
        self.assertIn(option["immediate_effect"]["ball"], state["facts"]["objects"])
        self.assertEqual(state["last_result"], receipt)
        self.assertEqual(exchange(self.adapter, self.root, intent), receipt)
        return receipt

    def test_open_a_blocked_door_when_the_nearest_ball_approach_has_no_drop_slot(self):
        state = exchange(self.adapter, self.root)
        self.assertIsNone(state["routes"]["face locked door, red (5,1)"])
        receipt = self.open_door(state)
        episode, env, _ = self.adapter.restore(self.root)
        env.close()
        self.assertEqual([s["action"] for s in episode["steps"]], receipt["native_actions"])
        self.assertIn("drop", receipt["native_actions"])
        self.assertEqual(receipt["native_actions"][-1], "toggle")

    def test_recover_a_temporarily_placed_key_before_unlocking(self):
        for i, move in enumerate(("left", "left", "forward", "left", "pickup")):
            state = exchange(self.adapter, self.root)
            receipt = exchange(self.adapter, self.root, request(state, move, f"carry-key-{i}"))
            self.assertEqual(receipt["status"], "completed")
        state = exchange(self.adapter, self.root)
        self.assertEqual(state["facts"]["inventory"], "red key")
        receipt = self.open_door(state)
        self.assertEqual(receipt["native_actions"].count("drop"), 2)
        self.assertEqual(receipt["native_actions"].count("pickup"), 2)

    def test_skip_unlock_when_the_native_budget_is_insufficient(self):
        run = json.loads(self.episode.read_text())
        run["limit"] = 2
        write_json(self.episode, run)
        state = exchange(self.adapter, self.root)
        self.assertTrue(state["ready"])
        self.assertFalse(any(a["id"].startswith(self.adapter.UNLOCK_DOOR) for a in state["actions"]))
