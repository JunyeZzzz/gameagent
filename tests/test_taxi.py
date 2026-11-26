import json
from importlib.util import find_spec
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from environment import adapter_for, exchange


@unittest.skipUnless(find_spec("gymnasium"), "Taxi example uses Gymnasium")
class TaxiTest(unittest.TestCase):
    def test_route_reacts_to_the_native_passenger_destination(self):
        adapter = adapter_for("taxi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "episode.json"
            path.write_text(json.dumps(dict(name="taxi-test", seed=42, limit=40, steps=[], routes=True,
                                            fickle_passenger=True, fickle_probability=1.0)),
                            encoding="utf-8")
            initial = exchange(adapter, root)
            destination = initial["facts"]["destination"]
            observed_switch = False
            for _ in range(40):
                view = exchange(adapter, root)
                if not view["ready"]:
                    break
                goal = view["facts"]["destination"]
                if goal != destination:
                    observed_switch = True
                    self.assertEqual(view["facts"]["passenger"], "onboard")
                    self.assertNotIn("送客 " + destination, view["routes"])
                actions = view["actions"]
                action = next(candidate for candidate in actions
                              if candidate["id"] in ("pickup", "dropoff") or candidate["id"].startswith("route:"))
                intent = dict(id=str(uuid.uuid4()), session=view["session"], revision=view["revision"],
                              action=action["id"], expires_at=time.time()+5)
                receipt = exchange(adapter, root, intent)
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(exchange(adapter, root, intent), receipt)
            self.assertTrue(observed_switch)
            final = exchange(adapter, root)
            self.assertEqual(final["reason"], "goal_completed")
            self.assertEqual(final["last_result"]["reward"], 20)
            self.assertNotEqual(final["facts"]["destination"], destination)
