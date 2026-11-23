import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import persistent_gameagent as runtime


class PacingTest(unittest.TestCase):
    def test_completed_rereads_immediately_and_idle_or_stale_views_wait(self):
        def scene(revision, **fields):
            return dict({"ready": True, "session": "world", "revision": revision,
                         "goal": "reach target", "actions": [{"id": "move"}]}, **fields)

        scenes = iter([scene("1"), scene("1"), scene("2"),
                       scene("3", pending={"id":"pending"}, actions=[]),
                       scene("4", ready=False, reason="paused"), scene("5")])
        events = []
        def read(*args, **kwargs):
            snapshot = next(scenes, None)
            if snapshot is None:
                raise InterruptedError()
            events.append("read " + snapshot["revision"])
            return Mock(returncode=0, stdout=json.dumps(snapshot))

        def decide(obs):
            events.append("decide " + obs["revision"])
            return {"id":obs["revision"], "session":"world", "status":"completed", "action":"move"}, ("world",obs["revision"])

        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda delay: events.append("wait")
        pi = Mock(id="pi")
        pi.request.return_value = {"model":{"id":"m","provider":"gameagent"},
                                   "sessionId":"pi","autoCompactionEnabled":False}
        pi.round.side_effect = decide
        with tempfile.TemporaryDirectory() as temp, patch.object(runtime.threading,"Event",return_value=stop), \
             patch.object(runtime.signal,"signal"), patch.object(runtime.subprocess,"run",side_effect=read), \
             patch.object(runtime,"PiSession", return_value=pi):
            self.assertEqual(runtime.serve(Path(temp), Mock(__name__="adapter"), "m", {"GAMEAGENT_PYTHON":sys.executable}, False),0)
        self.assertEqual(events, ["read 1", "decide 1", "read 1", "wait", "read 2", "decide 2",
                                  "read 3", "wait", "read 4", "wait", "read 5", "decide 5"])
