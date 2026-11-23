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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = Path(__file__).resolve().parents[1]


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        tool = self.server.tool
        use_tool = tool and not any(m["role"] == "tool" for m in payload["messages"])
        delta = {"role": "assistant", "content": "Observation reviewed."}
        if use_tool:
            args = {"action": self.server.action} if tool == "game_schedule" else {}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "type": "function",
                     "id": "mock-call", "function": {"name": tool, "arguments": json.dumps(args)}}]}
        for content, reason in ((delta, None), ({}, "tool_calls" if use_tool else "stop")):
            chunk = {"id": "mock", "model": "test-model", "object": "chat.completion.chunk",
                     "created": int(time.time()), "choices": [{"index": 0, "delta": content, "finish_reason": reason}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class Smoke(unittest.TestCase):
    def run_worker(self, tool=None, alternative=False, continuous=False):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            for script in ("run.py", "monitor.py"):
                shutil.copy2(PROJECT / script, project / script)
            for directory in ("adapters", "runtime"):
                shutil.copytree(PROJECT / directory, project / directory,
                                ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
            (project / "state").mkdir()
            (project / "runtime/node_modules").symlink_to(PROJECT / "runtime/node_modules", target_is_directory=True)
            state = project / "state"
            fixture = {"session": "smoke", "revision": 1, "position": 0, "destination": 2}
            (state / "fixture.json").write_text(json.dumps(fixture))
            args = [sys.executable, "run.py"]
            if not continuous:
                args.append("--once")
            if alternative:
                # The second adapter has no fixture, marker, or shared action with the bundled one.
                (state / "fixture.json").unlink()
                (project / "adapters/signalbox.py").write_text(
                    'import json, time\n'
                    'def observe(root):\n'
                    '    return dict(ready=True, observed_at=time.time(), valid_for=60, session="panel",\n'
                    '                revision="dark", goal="Turn on the lamp", facts={"lamp": False},\n'
                    '                actions=[{"id":"light"}])\n'
                    'def act(root, intent):\n'
                    '    receipt = dict(id=intent["id"], session="panel", status="completed", lamp=True)\n'
                    '    (root/"result.json").write_text(json.dumps(receipt))\n'
                    '    return receipt\n')
                args += ["--adapter", "signalbox"]
            server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
            server.payloads = []
            server.tool = tool
            server.action = "light" if alternative else "advance"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = dict(os.environ, GAMEAGENT_API_KEY="local-test-token", GAMEAGENT_MODEL="test-model",
                       GAMEAGENT_MODEL_URL=f"http://127.0.0.1:{server.server_port}/v1",
                       GAMEAGENT_HOME=str(project / "data"), GAMEAGENT_ROOT=str(state),
                       GAMEAGENT_ADAPTER="sandbox", GAMEAGENT_INTERVAL="30" if continuous else "1",
                       PYTHONDONTWRITEBYTECODE="1")
            try:
                if continuous:
                    worker = subprocess.Popen(args, cwd=project, env=env)
                    try:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline and worker.poll() is None:
                            path = project / "data/status.json"
                            if path.exists() and json.loads(path.read_text()).get("reason") == "goal_completed":
                                break
                            time.sleep(.1)
                        else:
                            self.fail("worker did not reach its observed goal")
                        self.assertEqual(json.loads((state / "fixture.json").read_text())["position"], 2)
                        self.assertEqual(len(server.payloads), 2)
                        snapshots = [json.loads(part["text"]) for msg in server.payloads[1]["messages"]
                                     if isinstance(msg.get("content"), list) for part in msg["content"]
                                     if part.get("text","").startswith('{"游戏快照"')]
                        self.assertEqual(snapshots[-1]["最近已执行动作"], [{"action":"advance"}])
                    finally:
                        worker.terminate()
                        worker.wait(timeout=8)
                    self.assertEqual(json.loads(path.read_text())["status"], "stopped")
                    return
                proc = subprocess.run(args, cwd=project, env=env, capture_output=True, text=True, timeout=35)
            finally:
                server.shutdown()
                server.server_close()
            status_path = project / "data/status.json"
            self.assertEqual(proc.returncode, 0, (proc.stderr, status_path.read_text() if status_path.exists() else ""))
            status = json.loads(status_path.read_text())
            self.assertEqual(status["status"], "reviewed")
            payload = server.payloads[0]
            self.assertIn("游戏快照", json.dumps(payload["messages"], ensure_ascii=False))
            self.assertEqual({t["function"]["name"] for t in payload["tools"]}, {"game_schedule", "game_observe"})
            if alternative:
                self.assertIn("Turn on the lamp", json.dumps(payload))
                self.assertNotIn("Move the marker", json.dumps(payload))
                self.assertTrue(json.loads((state / "result.json").read_text())["lamp"])
            elif tool == "game_schedule":
                self.assertEqual(json.loads((state / "fixture.json").read_text())["position"], 1)
            if tool == "game_observe":
                self.assertGreaterEqual(len(server.payloads), 2)
            elif tool == "game_schedule":
                self.assertEqual(len(server.payloads), 1)
            self.check_monitor(project, env, status["run_id"], tool)

    def check_monitor(self, project, env, run_id, tool):
        monitor = subprocess.Popen([sys.executable, "monitor.py"], cwd=project, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            address = monitor.stdout.readline().strip().rstrip("/")
            def get(endpoint):
                with urllib.request.urlopen(address + endpoint, timeout=3) as response:
                    return json.load(response)
            self.assertTrue(get("/api/events")["events"])
            self.assertIn(run_id, [r["run_id"] for r in get("/api/history")["rounds"]])
            events = get("/api/round?run_id=" + run_id)["events"]
            phases = [e["phase"] for e in events]
            for phase in ("prefetch_start", "prefetch_end", "model_start", "model_end", "round_end"):
                self.assertIn(phase, phases)
            if tool:
                event = next(e for e in events if e["phase"] == "tool_end")
                self.assertEqual(event["summary"]["tool"], tool)
                self.assertEqual(event["summary"]["status"], "completed" if tool == "game_schedule" else "success")
        finally:
            monitor.terminate()
            monitor.communicate(timeout=5)

    def test_observe_only(self):
        self.run_worker()

    def test_explicit_observation(self):
        self.run_worker("game_observe")

    def test_action(self):
        self.run_worker("game_schedule")

    def test_replace_adapter_without_core_changes(self):
        self.run_worker("game_schedule", alternative=True)

    def test_persistent_session_reaches_goal(self):
        self.run_worker("game_schedule", continuous=True)

    def test_external_directory(self):
        env = dict(os.environ, GAMEAGENT_ROOT="/tmp")
        proc = subprocess.run([sys.executable, "run.py"], cwd=PROJECT, env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("独立项目内", proc.stderr)
