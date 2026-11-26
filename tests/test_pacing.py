"""Run the real tool boundary and Pi RPC against a deliberately delayed model."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from persistent_gameagent import (
    PiSession, control_wait_reason, decision_key, direct_user_completion, dispatch_direct_control, serve, superseded,
)
from environment import write_json
from task_state import TaskState
from world_feed import WorldFeed


class DelayedModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        parts = [part["text"] for m in payload["messages"] if isinstance(m.get("content"), list)
                 for part in m["content"] if part.get("text", "").startswith('{"游戏快照"')]
        context = json.loads(parts[-1])
        view = context["游戏快照"]
        if view.get("blocked_capabilities"):
            self.server.wait_calls += 1
            self.send_call("game_observe", {"assessment":"The input is busy; watching for release.", "wait_for":"input"})
            return
        if view["goal"] in ("Rest here once", "Rest here again"):
            self.server.rest_assessments += 1
            if context.get("上次提交被拒"):
                self.server.rest_corrections.append(context["上次提交被拒"])
            evidence = view["last_result"]["id"]
            self.send_call("game_observe", {"assessment":"Checking the arrival receipt.",
                                           "completed_after":evidence if evidence == "second-arrival" else "latest"})
            return
        if view.get("pending"):
            action = "agent:cancel"
            self.server.pending_call.set()
            self.server.release.wait(12)
        else:
            action = view["actions"][0]["id"]
        self.send_call("game_schedule", {"action":action})

    def send_call(self, name, args):
        self.send_calls((name, args))

    def send_calls(self, *calls, stream_open=False):
        delta = {"role":"assistant", "tool_calls":[
            {"index":index, "type":"function", "id":uuid.uuid4().hex,
             "function":{"name":name, "arguments":json.dumps(args)}}
            for index, (name, args) in enumerate(calls)]}
        try:
            if not stream_open:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
            for d, reason in ((delta,None), ({}, "tool_calls")):
                chunk = {"id":"response", "object":"chat.completion.chunk", "created":int(time.time()),
                         "model":"test", "choices":[{"index":0,"delta":d,"finish_reason":reason}]}
                self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def wait_for(condition, limit=8):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Expected game event did not arrive")


class StalledModel(DelayedModel):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        view = next(json.loads(part["text"])["游戏快照"] for message in payload["messages"]
                    if isinstance(message.get("content"), list) for part in message["content"]
                    if part.get("text", "").startswith('{"游戏快照"'))
        request = len(self.server.requests)
        self.server.requests.append(time.time())
        if request == 0:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": waiting for model output\n\n")
            self.server.release.wait(45)
            self.send_calls(("game_schedule", {"action": "decode"}), stream_open=True)
        elif not view.get("pending"):
            action = "collect" if view["facts"]["decoded"] else "decode"
            self.send_call("game_schedule", {"action": action})
        elif request == 2:
            self.server.release.wait(45)
            self.send_call("game_schedule", {"action": "agent:cancel"})
        else:
            self.send_call("game_observe", {"assessment": "Keep decoding.", "hold": True})


class TaskModel(DelayedModel):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        texts = [part.get("text", "") for msg in body["messages"] if isinstance(msg["content"], list)
                 for part in msg["content"]]
        context = next(json.loads(t) for t in texts if t.startswith('{"游戏快照"'))
        view = context["游戏快照"]
        task = context.get("当前任务")
        if view["goal"] == "Rest here once":
            self.server.new_context = context
            self.send_call("game_observe", {"assessment": "Rest here now.", "hold": True})
        elif view.get("pending"):
            self.server.reviews.append((view["pending"]["id"], task))
            self.send_call("game_observe", {"assessment": "Continue decoding this message.", "hold": True})
        elif view["facts"]["decoded"]:
            self.server.decoded_context = context
            if not self.server.idle_hold:
                self.server.idle_hold = True
                self.send_call("game_observe", {"assessment": "The decoder finished; collect the message next.",
                                                "hold": True})
            else:
                assert any(msg["role"] == "tool" and "idle_hold_requires_condition" in str(msg["content"])
                           for msg in body["messages"])
                self.send_call("game_schedule", {"action": "collect"})
        elif not self.server.initial_assessment:
            self.server.initial_assessment = True
            self.send_call("game_observe", {"assessment": "Start the decoder now.", "task": {
                "intent": "Decode the message.",
                "expected": "The game reports decoded.",
                "revisit": "A result arrives or the decoder remains busy at the review.",
                "finish_when": "The decoded message is collected.",
                "recheck_after_s": 2,
            }})
        else:
            assert any(msg["role"] == "tool" for msg in body["messages"])
            self.send_call("game_schedule", {"action": "decode"})


class SteeringModel(DelayedModel):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        episode = next(json.loads(part["text"]) for message in payload["messages"]
                       if isinstance(message.get("content"), list) for part in message["content"]
                       if part.get("text", "").startswith('{"游戏快照"'))
        view = episode["游戏快照"]
        control = view.get("last_review") or {}
        self.server.rounds.append((view["goal"], control.get("status")))
        game_control_state = (view.get("ongoing") or {}).get("game_control_state")
        if game_control_state:
            self.server.control_reviews.append((game_control_state, view["observed_at"], view["pending"]["id"]))
        change = episode.get("现场变化")
        if not view.get("pending"):
            self.send_call("game_schedule", {"action":"drive"})
        elif change:
            self.server.situation_changes.append(change)
            self.send_call("game_observe", {"assessment":"The wind changed. Finish the accepted turn and reassess.", "hold":True})
        elif game_control_state:
            self.send_call("game_observe", {"assessment":"Keep the current action.", "hold":True, "recheck_after_s":20})
        elif (view["goal"], control.get("status")) in (("Start moving", None), ("Adjust again", "completed")):
            self.send_call("game_schedule", {"action":"review:steer"})
        else:
            self.send_call("game_observe", {"assessment":"Keep moving.", "hold":True})


class PacingTest(unittest.TestCase):
    def test_active_play_starts_each_round_from_a_new_snapshot(self):
        adapter = SimpleNamespace(__name__="test_game", __file__="test_game.py")
        for activity in ({"pending": {"id": "walking"}}, {"continuous_activity": True}):
            with self.subTest(activity=activity), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                feed, model = Mock(), Mock()
                view = dict(ready=True, revision="stable", session="game", goal="Keep moving",
                            actions=[{"id": "agent:continue"}], **activity)
                stamp = time.time() + 60
                feed.latest = SimpleNamespace(sequence=0, observation=view)
                feed.after.side_effect = [
                    SimpleNamespace(sequence=i, started=stamp+i, finished=stamp+i,
                                    observation=dict(view, observed_at=i)) for i in range(1, 4)
                ] + [InterruptedError()]
                model.id = "model-session"
                model.request.return_value = dict(model=dict(id="test", provider="gameagent"), sessionId=model.id,
                                                  autoCompactionEnabled=False)
                samples = []

                def decide(observation, *_):
                    samples.append(observation["observed_at"])
                    return dict(status="observing", recheck_after_s=20), ("game", "stable"), False

                model.round.side_effect = decide
                with patch("persistent_gameagent.WorldFeed", return_value=feed), \
                     patch("persistent_gameagent.PiSession", return_value=model), \
                     patch("persistent_gameagent.signal.signal"):
                    self.assertEqual(serve(home, adapter, "test", {}, once=False), 0)
                self.assertEqual(samples, [1, 2, 3])

    def test_consecutive_rejections_stay_visible_during_fresh_reviews(self):
        rejected = [dict(status="rejected", action="review:steer", reason=reason) for reason in
                    ("observation_changed", "select_one_action", "action_unavailable")]
        adapter = SimpleNamespace(__name__="steering", __file__="steering.py")
        for requires_action in (False, True):
            with self.subTest(requires_action=requires_action), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                view = dict(ready=True, session="track", goal="Drive", goal_id="command", revision="stable",
                            last_result=dict(id="drive", session="track", status="running"), pending={"id":"drive"},
                            ongoing=dict(trigger="waiting_for_route", requires_action=requires_action),
                            actions=[{"id":"review:steer"}])
                feed, model = Mock(), Mock()
                stamp = time.time() + 60
                feed.latest = SimpleNamespace(sequence=0, observation=view)
                feed.after.side_effect = [SimpleNamespace(sequence=i, started=stamp, finished=stamp, observation=view)
                                          for i in range(1, 7)] + [InterruptedError()]
                model.id = "model-session"
                model.request.return_value = dict(model=dict(id="test", provider="gameagent"), sessionId=model.id,
                                                  autoCompactionEnabled=False)
                seen = []

                def decide(*_):
                    seen.append(json.loads((home/"round.json").read_text())["rejection"])
                    receipt = rejected[len(seen)-1] if len(seen)<=len(rejected) else dict(status="observing")
                    return receipt, ("track", "stable"), False

                model.round.side_effect = decide
                with patch("persistent_gameagent.WorldFeed", return_value=feed), \
                     patch("persistent_gameagent.PiSession", return_value=model), \
                     patch("persistent_gameagent.signal.signal"):
                    self.assertEqual(serve(home, adapter, "test", {}, once=False), 0)
                expected = [None, *[{"action":r["action"], "reason":r["reason"]} for r in rejected], None, None]
                self.assertEqual(seen, expected)

    def test_direct_control_refresh_preserves_identity_and_bounds_submissions(self):
        intent = dict(id="stop", session="game", goal="Stop", goal_id="message", revision="old",
                      parent_id="drive", action="review:park")
        fresh = dict(intent, ready=True, revision="new", observed_at=100, valid_for=10,
                     pending={"id": "drive"}, direct_user_action="review:park",
                     actions=[{"id": "review:park"}])
        stale = dict(status="rejected", reason="observation_changed")
        unknown = dict(status="unknown", reason="transport_interrupted")
        cases = {
            "unknown effect": ([unknown], unknown),
            "another goal": ([stale, dict(fresh, goal_id="new-message")], stale),
            "another action in progress": ([stale, dict(fresh, pending={"id": "walk"})], stale),
            "direct action unavailable": ([stale, dict(fresh, direct_user_action=None)], stale),
            "revision changed again": ([stale, fresh, stale], stale),
        }
        for name, (responses, expected) in cases.items():
            with self.subTest(name=name), patch("persistent_gameagent.exchange", side_effect=responses) as invoke:
                self.assertEqual(dispatch_direct_control("adapter", "root", intent), expected)
                requests = [["adapter", "root", intent]]
                if len(responses) >= 2:
                    requests.append(["adapter", "root"])
                if len(responses) == 3:
                    requests.append(["adapter", "root", dict(intent, revision="new", expires_at=110)])
                self.assertEqual([list(call.args) for call in invoke.call_args_list], requests)

    def test_direct_completion_checks_current_scope_and_game_evidence(self):
        result = dict(id="original-action", session="track", goal_id="stop", status="cancelled",
                      interruption={"confirmed": True})
        view = dict(ready=True, session="track", goal="Stop", goal_id="stop", last_result=result,
                    direct_user_completion="The game confirmed the stop.")
        proof = direct_user_completion(view, None)
        self.assertEqual((proof["evidence_id"], proof["goal_id"], proof["reason"]),
                         (result["id"], "stop", "game_goal_satisfied"))
        invalid = (
            {"goal_id": "new"}, {"ready": False}, {"pending": {"id": result["id"]}},
            {"completion_checks": [{"met": False}]},
            {"last_result": dict(result, session="other")},
            {"last_result": dict(result, interruption={"confirmed": False})},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertIsNone(direct_user_completion(dict(view, **changes), None))
        self.assertIsNone(direct_user_completion(view, result["id"]))

    def test_observation_timer_survives_with_the_current_task(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "task.json"
            scope = ("world", "Hold then walk", "first")
            brief = dict(intent="Hold in place", expected="Stationary", revisit="After six seconds",
                         finish_when="Reached the next point")
            task = TaskState(path)
            with patch("task_state.time.time", return_value=100):
                self.assertTrue(task.update(scope, "round", dict(status="observing", recheck_after_s=6, task=brief)))
            recovered = TaskState(path)
            with patch("task_state.time.time", return_value=104):
                self.assertFalse(recovered.view()["review_due"])
                self.assertEqual(recovered.view()["review_remaining_s"], 2)
            with patch("task_state.time.time", return_value=106):
                self.assertTrue(recovered.view()["review_due"])
                self.assertEqual(recovered.view()["review_elapsed_s"], 6)
            recovered.select(("world", "Walk now", "next"))
            self.assertIsNone(recovered.view())

    def test_applied_control_follows_current_effect_after_submission_expires(self):
        scope = dict(session="track", goal="Turn west", goal_id="west")
        assessment = dict(scope, id="course", parent_id="drive", expires_at=100)
        active = dict(id="drive", trigger="turning", control_effect_pending=True)
        view = dict(scope, pending={"id": "drive"}, ongoing=active,
                    actions=[{"id": "agent:continue"}], last_assessment=assessment,
                    last_review={"id": "course", "status": "completed"},
                    last_result={"id": "drive", "session": "track", "status": "running"})
        with patch("persistent_gameagent.time.time", return_value=105):
            self.assertEqual(control_wait_reason(view), "control_effect")
            for changed in (
                dict(view, last_review={"id": "course", "status": "accepted"}),
                dict(view, last_result={"id": "drive", "session": "track", "status": "completed"}),
                dict(view, ongoing=dict(active, control_effect_pending=False, trigger="stalled")),
                dict(view, ongoing=dict(active, id="different-drive")),
                dict(view, goal="Turn back east", goal_id="east"),
                dict(view, actions=[{"id": "review:steer"}]),
            ):
                with self.subTest(snapshot=changed):
                    self.assertIsNone(control_wait_reason(changed))

    def test_fresh_reviews_continue_through_start_and_control_receipts_even_with_a_timer(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            shutil.copy2(ROOT / "run.py", project)
            shutil.copytree(ROOT / "runtime", project / "runtime",
                            ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
            (project / "runtime/node_modules").symlink_to(ROOT / "runtime/node_modules")
            (project / "adapters").mkdir()
            shutil.copy2(ROOT / "tests/fixtures/steering.py", project / "adapters")
            world, home = project / "state", project / "data"
            world.mkdir()
            (world / "hold_start").touch()
            write_json(world / "situation.json", {"key": "calm", "wind": "calm"})
            server = ThreadingHTTPServer(("127.0.0.1", 0), SteeringModel)
            server.rounds = []
            server.situation_changes = []
            server.control_reviews = []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = dict(os.environ, GAMEAGENT_API_KEY="fixture", GAMEAGENT_MODEL="test", GAMEAGENT_INTERVAL="0",
                       GAMEAGENT_MODEL_URL=f"http://127.0.0.1:{server.server_port}/v1",
                       GAMEAGENT_ROOT=str(world), GAMEAGENT_HOME=str(home))
            proc = subprocess.Popen([sys.executable, "run.py", "--adapter", "steering"], cwd=project, env=env)
            try:
                def read(path):
                    return json.loads(path.read_bytes()) if path.exists() else {}

                def check_fresh_reviews(control_state):
                    start = len(server.control_reviews)
                    samples = wait_for(lambda: entries if len(entries := [r for r in server.control_reviews[start:]
                        if r[0] == control_state]) >= 3 else None, limit=3)
                    timestamps = [r[1] for r in samples]
                    self.assertEqual(timestamps, sorted(set(timestamps)))
                    self.assertEqual({r[2] for r in samples}, {read(world / "drive.json")["id"]})

                wait_for(lambda: read(world / "drive.json").get("status") == "accepted")
                check_fresh_reviews("action_start")
                self.assertFalse((world / "control.json").exists())
                (world / "goal.txt").write_text("Check the current startup")
                wait_for(lambda: ("Check the current startup", None) in server.rounds)
                self.assertEqual(read(home / "perception.json")["result_status"], "accepted")
                (world / "hold_start").unlink()
                wait_for(lambda: read(home / "perception.json")["result_status"] == "running")
                (world / "goal.txt").write_text("Start moving")

                first = wait_for(lambda: read(world / "control.json"))
                check_fresh_reviews("control_receipt")
                self.assertEqual(read(world / "control.json")["id"], first["id"])
                (world / "settling").touch()
                (world / (first["id"] + ".ack")).touch()
                check_fresh_reviews("control_effect")
                write_json(world / "situation.json", {"key": "wind", "wind": "strong gust"})
                wait_for(lambda: server.situation_changes, limit=3)
                self.assertEqual(server.situation_changes, [{"之前": {"key": "calm", "wind": "calm"},
                                                            "当前": {"key": "wind", "wind": "strong gust"}}])
                check_fresh_reviews("control_effect")
                write_json(world / "situation.json", {"key": "wind", "wind": "same strong gust, updated sample"})
                check_fresh_reviews("control_effect")
                (world / "settling").unlink()
                wait_for(lambda: not (read(home / "perception.json").get("ongoing") or {}).get("control_effect_pending"))
                (world / "goal.txt").write_text("Adjust again")
                wait_for(lambda: read(world / "control.json").get("goal_id") == "Adjust again")
                second = read(world / "control.json")
                check_fresh_reviews("control_receipt")
                (world / "settling").touch()
                (world / (second["id"] + ".ack")).touch()
                check_fresh_reviews("control_effect")
                (world / "goal.txt").write_text("Tell me now")
                wait_for(lambda: ("Tell me now", "completed") in server.rounds)
                self.assertEqual(read(home / "perception.json")["result_status"], "running")
                (world / "settling").unlink()
                (world / "goal.txt").write_text("Set course now")
                wait_for(lambda: read(world / "control.json").get("goal_id") == "Set course now")
                direct = read(world / "control.json")
                check_fresh_reviews("control_receipt")
                self.assertEqual(read(world / "control.json")["id"], direct["id"])
                (world / (direct["id"] + ".ack")).touch()
                wait_for(lambda: ("Set course now", "completed") in server.rounds)
                self.assertEqual(read(world / "control.json")["id"], direct["id"])
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                server.shutdown()
                server.server_close()

    def test_model_transport_stalls_recover_without_replaying_or_interrupting_game_actions(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            shutil.copy2(ROOT / "run.py", project)
            shutil.copytree(ROOT / "runtime", project / "runtime",
                            ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
            (project / "runtime/node_modules").symlink_to(ROOT / "runtime/node_modules")
            (project / "adapters").mkdir()
            shutil.copy2(ROOT / "tests/fixtures/decoder.py", project / "adapters")
            world, home = project / "state", project / "data"
            world.mkdir()
            server = ThreadingHTTPServer(("127.0.0.1", 0), StalledModel)
            server.requests, server.release = [], threading.Event()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = dict(os.environ, GAMEAGENT_API_KEY="fixture", GAMEAGENT_MODEL="test", GAMEAGENT_INTERVAL="0",
                       GAMEAGENT_MODEL_URL=f"http://127.0.0.1:{server.server_port}/v1",
                       GAMEAGENT_ROOT=str(world), GAMEAGENT_HOME=str(home))
            proc = subprocess.Popen([sys.executable, "run.py", "--adapter", "decoder"], cwd=project, env=env)
            try:
                def read(path):
                    return json.loads(path.read_bytes()) if path.exists() else {}

                def recovery(request):
                    current = read(home / "status.json")
                    if current.get("status") == "recovering" and len(server.requests) == request + 1:
                        self.assertRegex(current["error"].lower(), r"timeout|timed out")
                        self.assertLess(current["updated_at"] - server.requests[request], 17)
                        return current

                wait_for(lambda: server.requests, limit=6)
                wait_for(lambda: recovery(0), limit=18)
                self.assertFalse((world / "decoding.json").exists())
                wait_for(lambda: len(server.requests) >= 3, limit=6)
                initial = read(home / "perception.json")
                parent = read(world / "decoding.json")
                self.assertEqual(initial["pending_id"], parent["id"])
                wait_for(lambda: recovery(2), limit=18)
                after = read(home / "perception.json")
                self.assertGreaterEqual(after["sequence"] - initial["sequence"], 10)
                self.assertEqual((after["result_status"], after["pending_id"]), ("running", parent["id"]))
                wait_for(lambda: len(server.requests) >= 4, limit=6)
                wait_for(lambda: len(server.requests) >= 5, limit=6)
                server.release.set()
                (world / "decoded").touch()
                wait_for(lambda: (world / "claimed").exists())
                self.assertEqual(read(world / "decoding.json"), parent)
                self.assertFalse((world / "cancelled").exists())
            finally:
                server.release.set()
                proc.terminate()
                proc.wait(timeout=10)
                server.shutdown()
                server.server_close()

    def test_task_survives_action_reviews_and_retires_with_its_instruction(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            shutil.copy2(ROOT / "run.py", project)
            shutil.copytree(ROOT / "runtime", project / "runtime",
                            ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
            (project / "runtime/node_modules").symlink_to(ROOT / "runtime/node_modules")
            (project / "adapters").mkdir()
            shutil.copy2(ROOT / "tests/fixtures/decoder.py", project / "adapters")
            world = project / "state"
            world.mkdir()
            home = project / "data"
            server = ThreadingHTTPServer(("127.0.0.1", 0), TaskModel)
            server.reviews = []
            server.decoded_context = server.new_context = None
            server.initial_assessment = False
            server.idle_hold = False
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = dict(os.environ, GAMEAGENT_API_KEY="fixture", GAMEAGENT_MODEL="test", GAMEAGENT_INTERVAL="0",
                       GAMEAGENT_MODEL_URL=f"http://127.0.0.1:{server.server_port}/v1",
                       GAMEAGENT_ROOT=str(world), GAMEAGENT_HOME=str(home))
            proc = subprocess.Popen([sys.executable, "run.py", "--adapter", "decoder"], cwd=project, env=env)
            try:
                wait_for(lambda: any(task and task["review_due"] for _, task in server.reviews), limit=10)
                due_task = next(task for _, task in server.reviews if task and task["review_due"])
                self.assertEqual(due_task["review_remaining_s"], 0)
                self.assertGreaterEqual(due_task["review_elapsed_s"], 2)
                active = json.loads((world / "decoding.json").read_text())
                self.assertEqual({parent for parent, _ in server.reviews}, {active["id"]})
                restored = TaskState(home / "task.json")
                self.assertEqual(restored.record["action_id"], active["id"])
                self.assertEqual(restored.view()["finish_when"], "The decoded message is collected.")
                wait_for(lambda: json.loads((home / "task.json").read_text())["review_at"] is None)
                (world / "decoded").touch()
                wait_for(lambda: (world / "claimed").exists())
                self.assertEqual(server.decoded_context["当前任务"]["intent"], "Decode the message.")
                self.assertEqual(server.decoded_context["游戏快照"]["last_result"]["status"], "completed")
                (world / "rest").write_text("arrived")
                (world / "claimed").unlink()
                wait_for(lambda: server.new_context)
                self.assertNotIn("当前任务", server.new_context)
                self.assertFalse((home / "task.json").exists())
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                server.shutdown()
                server.server_close()

    def test_observer_picks_up_changed_actions_without_losing_the_game_session(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            runtime = project / "runtime"
            runtime.mkdir()
            shutil.copy2(ROOT / "runtime/environment.py", runtime)
            shutil.copy2(ROOT / "runtime/navigation.py", runtime)
            adapter = project / "adapters/decoder.py"
            adapter.parent.mkdir()
            shutil.copy2(ROOT / "tests/fixtures/decoder.py", adapter)
            world = project / "state"
            world.mkdir()
            env = dict(GAMEAGENT_PYTHON=sys.executable, GAMEAGENT_ADAPTER="decoder",
                       GAMEAGENT_ROOT=str(world))
            feed = WorldFeed(project, env, threading.Event(), adapter)
            feed.command[1] = str(runtime / "environment.py")
            try:
                first = feed._read_view()
                self.assertEqual(first["actions"][0]["id"], "decode")
                stat = adapter.stat()
                adapter.write_text(adapter.read_text().replace('else "decode"', 'else "recode"'))
                # Also exercise the same-second, same-size update that native bytecode caches can miss.
                os.utime(adapter, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
                second = feed._read_view()
                self.assertEqual(second["session"], first["session"])
                self.assertEqual(second["actions"][0]["id"], "recode")
                self.assertEqual(feed._read_view()["actions"], second["actions"])
            finally:
                feed._close_reader()

    def test_output_limit_finishes_a_round_without_restarting_the_model_session(self):
        session = Mock(spec=PiSession)
        session.send.return_value = "request"
        session.next_event.side_effect = [
            {"type":"response", "id":"request", "success":True, "data":{"disposition":"started"}},
            {"type":"message_end", "message":{"role":"assistant", "stopReason":"length"}},
            {"type":"agent_settled"},
        ]
        receipt, basis, truncated = PiSession.round(session, {"session":"story","revision":"r1"}, lambda:False)
        self.assertIsNone(receipt)
        self.assertEqual(basis, ("story", "r1"))
        self.assertTrue(truncated)
        self.assertEqual(session.send.call_count, 1)

    def test_live_feedback_does_not_starve_same_intent(self):
        base = dict(session="g", revision="route", ready=True, pending={"id":"walk"},
                    ongoing={"trigger":"moving","distance":50}, actions=[{"id":"agent:continue"}])
        newer = dict(base, ongoing={"trigger":"moving","distance":30})
        self.assertEqual(decision_key(base), decision_key(newer))
        self.assertFalse(superseded(base, newer))
        blocked = dict(newer, ongoing={"trigger":"blocked"})
        self.assertNotEqual(decision_key(base), decision_key(blocked))
        self.assertFalse(superseded(base, blocked))
        self.assertTrue(superseded(base, dict(blocked, ongoing={"trigger":"blocked", "requires_action":True})))
        self.assertFalse(superseded(base, {"ready":False, "reason":"observation_unavailable"}))

    def test_running_action_tracks_relevant_capabilities_without_canceling_the_round(self):
        view = dict(session="g", goal="follow", ready=True, pending={"id":"travel"},
                    ongoing={"id":"travel", "trigger":"following"})
        background = dict(view, blocked_capabilities=[{"id":"cheat_console", "reason":"background"}])
        self.assertFalse(superseded(view, background))
        needs_console = dict(view["ongoing"], depends_on=["cheat_console"])
        self.assertNotEqual(decision_key(dict(view, ongoing=needs_console)),
                            decision_key(dict(background, ongoing=needs_console)))
        self.assertFalse(superseded(dict(view, ongoing=needs_console), dict(background, ongoing=needs_console)))
        self.assertFalse(superseded(view, dict(background, ongoing={"id":"travel", "trigger":"stalled"})))
        self.assertTrue(superseded(view, dict(background, pending=None)))

    def test_live_candidates_can_change_during_judgment(self):
        ongoing = {"trigger":"walking", "control_families":["cease","move"], "decision_revision":[True,[10,0,0]]}
        original = dict(session="g", goal="travel", ready=True, pending={"id":"battle"}, ongoing=ongoing,
                        actions=[{"id":"review:move:10,0,0"}])
        latest = dict(original, actions=[{"id":"review:move:11,0,0"}])
        self.assertFalse(superseded(original,latest))
        self.assertFalse(superseded(original,dict(latest,ongoing=dict(ongoing,trigger="blocked"))))
        self.assertFalse(superseded(original,dict(latest,ongoing=dict(ongoing,decision_revision=[False,[10,0,0]]))))
        self.assertFalse(superseded(original,dict(latest,decision_event="new_candidates")))
        self.assertFalse(superseded(dict(original,pending=None),dict(latest,pending=None,decision_event="new_candidates")))
        self.assertTrue(superseded(original,dict(latest,goal_id="new_command")))
        self.assertTrue(superseded(original,dict(latest,session="new_game")))
        self.assertTrue(superseded(original,dict(latest,pending=None)))

    def test_confirmation_on_the_same_stop_receipt_wakes_decision(self):
        stop = dict(id="stop", session="g", status="cancelled")
        original = dict(session="g", goal="Stop here", ready=True, revision="stable",
                        last_result=dict(stop, interruption={"confirmed": False}))
        self.assertFalse(superseded(original, dict(original, last_result=stop)))
        confirmed = dict(stop, interruption={"confirmed": True})
        self.assertTrue(superseded(original, dict(original, last_result=confirmed)))

    def test_completion_preempts_slow_answer_while_perception_continues(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            shutil.copy2(ROOT / "run.py", project)
            shutil.copytree(ROOT / "runtime", project / "runtime",
                            ignore=shutil.ignore_patterns("node_modules","__pycache__"))
            (project / "runtime/node_modules").symlink_to(ROOT / "runtime/node_modules")
            (project / "adapters").mkdir()
            shutil.copy2(ROOT/"tests/fixtures/decoder.py", project/"adapters")
            state = project / "state"
            state.mkdir()
            (state / "rest").touch()
            server = ThreadingHTTPServer(("127.0.0.1",0), DelayedModel)
            server.rest_assessments = server.wait_calls = 0
            server.rest_corrections = []
            (state/"locked").touch()
            server.pending_call, server.release = threading.Event(), threading.Event()
            threading.Thread(target=server.serve_forever,daemon=True).start()
            env = dict(os.environ, GAMEAGENT_API_KEY="fixture", GAMEAGENT_MODEL="test", GAMEAGENT_INTERVAL="0",
                       GAMEAGENT_MODEL_URL=f"http://127.0.0.1:{server.server_port}/v1",
                       GAMEAGENT_ROOT=str(state), GAMEAGENT_HOME=str(project/"data"))
            proc = subprocess.Popen([sys.executable,"run.py","--adapter","decoder"],cwd=project,env=env)
            try:
                status_file = project/"data/status.json"
                wait_for(lambda: status_file.exists() and json.loads(status_file.read_bytes()).get("status")=="watching")
                perception = project/"data/perception.json"
                start = json.loads(perception.read_bytes())["sequence"]
                wait_for(lambda: json.loads(perception.read_bytes())["sequence"] >= start+6)
                self.assertEqual(server.wait_calls,1)
                (state/"locked").unlink()
                wait_for(lambda: bool(server.rest_corrections))
                self.assertEqual(server.rest_corrections[-1]["reason"], "completion_evidence_missing")
                count = server.rest_assessments
                sample_start = json.loads(perception.read_bytes())["sequence"]
                wait_for(lambda: json.loads(perception.read_bytes())["sequence"] >= sample_start+6)
                self.assertEqual(server.rest_assessments, count)
                self.assertNotEqual(json.loads(status_file.read_bytes())["status"], "monitoring")
                count = server.rest_assessments
                (state / "unsettled").touch()
                (state / "rest").write_text("arrived")
                wait_for(lambda: server.rest_assessments >= count+2 and any(
                    c.get("unmet_conditions") == [{"field":"rested","met":False}] for c in server.rest_corrections))
                self.assertNotEqual(json.loads(status_file.read_bytes())["status"], "monitoring")
                (state / "unsettled").unlink()
                wait_for(lambda: status_file.exists() and json.loads(status_file.read_bytes()).get("status")=="monitoring")
                perception = project/"data/perception.json"
                sample = lambda: json.loads(perception.read_bytes())
                resting = sample()["sequence"]
                count = server.rest_assessments
                wait_for(lambda: sample()["sequence"] >= resting+6)
                self.assertEqual(server.rest_assessments, count)
                self.assertFalse((state/"decoding.json").exists())
                (state / "unsettled").touch()
                wait_for(lambda: server.rest_assessments >= count+2)
                self.assertNotEqual(json.loads(status_file.read_bytes())["status"], "monitoring")
                (state / "unsettled").unlink()
                (state / "again").touch()
                wait_for(lambda: server.rest_assessments >= count+2)
                self.assertNotEqual(json.loads(status_file.read_bytes())["status"], "monitoring")
                (state / "rest").write_text("arrived again")
                wait_for(lambda: json.loads(status_file.read_bytes()).get("evidence_id") == "second-arrival")
                count = server.rest_assessments
                (state / "rest").write_text("external stop")
                (state / "again").unlink()
                wait_for(lambda: server.rest_assessments >= count+2)
                self.assertNotEqual(json.loads(status_file.read_bytes())["status"], "monitoring")
                confirmation = state / "stop-confirmation"
                confirmation.write_text("deliberate stop")
                confirmation.replace(state / "rest")
                wait_for(lambda: json.loads(status_file.read_bytes()).get("evidence_id") == "final-stop")
                (state / "rest").unlink()
                self.assertTrue(server.pending_call.wait(10))
                first = sample()
                wait_for(lambda: sample()["sequence"] >= first["sequence"]+4)
                last = sample()
                self.assertEqual(last["result_status"], "running")
                self.assertFalse((state/"claimed").exists())
                parent = json.loads((state/"decoding.json").read_bytes())
                started = time.monotonic()
                (state/"decoded").touch()
                wait_for(lambda: (state/"claimed").exists(), 5)
                self.measurements = {"observations_while_model_delayed":last["sequence"]-first["sequence"],
                                     "completion_to_next_action_s":round(time.monotonic()-started,3),
                                     "next_action_before_old_answer": not server.release.is_set()}
                server.release.set()
                late = dict(parent, id="late", parent_id=parent["id"], revision="busy", action="agent:cancel")
                reply = subprocess.check_output([sys.executable, "runtime/environment.py"], cwd=project,
                                                env=dict(env,GAMEAGENT_ADAPTER="decoder"), text=True,input=json.dumps(late))
                self.assertEqual(json.loads(reply)["reason"],"parent_ended")
                self.assertFalse((state/"cancelled").exists())
                self.measurements["late_action"] = json.loads(reply)["reason"]
            finally:
                server.release.set()
                proc.terminate()
                proc.wait(timeout=10)
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
