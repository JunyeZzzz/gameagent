"""Manage one persistent Pi RPC session for any project-local environment."""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid

from environment import write_json
from recent_actions import RecentActions

RUNTIME = Path(__file__).resolve().parent
SYSTEM = """你是通用游戏 Agent。根据当前观察里的用户目标、现场事实、可选动作和上次结果，决定下一步。
只能提交当前观察提供的动作 ID，也可以重新观察或等待。每轮最多提交一个动作。
以当前现场和已确认的动作效果判断进度；旧计划只是意图。先核对哪些前提已满足，再选尚未完成的目标。
涉及移动时，核对当前位置、朝向和障碍。若有通往所选地标的单步路线动作，优先提交该动作的完整 ID；
距离为零表示已经到达该地标的指定位置，再根据目标选择交互。路线只负责移动，目的地和交互由你选择。
重复经过相同状态或动作没有效果时，根据现有事实重新选路。结果未知时等待核实。"""


class PiSession:
    def __init__(self, home, model, env, stop):
        self.id = uuid.uuid4().hex
        self.stop = stop
        self.events = queue.Queue()
        sessions = home / "sessions"
        sessions.mkdir(exist_ok=True)
        args = [str(RUNTIME / "node_modules/.bin/pi"), "--offline", "--no-extensions",
                "--no-builtin-tools", "--no-skills", "--no-prompt-templates", "--no-context-files",
                "--no-mcp", "--session-id", self.id, "--session-dir", str(sessions),
                "--extension", str(RUNTIME / "game-tools.mjs"), "--system-prompt", SYSTEM,
                "--provider", "gameagent", "--model", model, "--thinking", "off", "--mode", "rpc"]
        self.process = subprocess.Popen(args, cwd=RUNTIME, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, encoding="utf-8")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                event = json.loads(line)
                if event.get("type") in {"response", "message_end", "agent_settled", "tool_execution_end"}:
                    self.events.put(event)
        finally:
            self.events.put({"type": "eof"})

    def next_event(self, deadline):
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pi 超时，请从环境核实动作结果")
            try:
                event = self.events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if event["type"] == "eof":
                raise RuntimeError("Pi 会话已退出")
            return event
        raise InterruptedError()

    def send(self, kind, **fields):
        request_id = uuid.uuid4().hex
        line = json.dumps({"id": request_id, "type": kind, **fields}, ensure_ascii=False)
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        return request_id

    def request(self, kind, **fields):
        request_id = self.send(kind, **fields)
        deadline = time.monotonic() + 20
        while True:
            event = self.next_event(deadline)
            if event.get("type") == "response" and event.get("id") == request_id:
                if not event.get("success"):
                    raise RuntimeError(event.get("error", "Pi 拒绝请求"))
                return event.get("data") or {}

    def round(self, observation):
        basis = (observation["session"], observation["revision"])
        request_id = self.send("prompt", message="根据本轮游戏快照决定下一步。")
        deadline = time.monotonic() + 120
        acknowledged = settled = False
        receipt = None
        while not (acknowledged and settled):
            event = self.next_event(deadline)
            kind = event["type"]
            if kind == "response" and event.get("id") == request_id:
                if not event.get("success") or event.get("data", {}).get("disposition") != "started":
                    raise RuntimeError(event.get("error", "模型轮次未启动"))
                acknowledged = True
            elif kind == "tool_execution_end" and event.get("toolName") == "game_schedule":
                receipt = (event.get("result") or {}).get("details")
            elif kind == "tool_execution_end" and event.get("toolName") == "game_observe":
                fresh = (event.get("result") or {}).get("details") or {}
                if fresh.get("ready"):
                    basis = (fresh["session"], fresh["revision"])
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("stopReason") in {"aborted", "error", "length"}:
                    raise RuntimeError(message.get("errorMessage") or message["stopReason"])
            elif kind == "agent_settled":
                settled = True
        return receipt, basis

    def close(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


def serve(home, adapter, model, env, once):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    interval = max(1, int(os.getenv("GAMEAGENT_INTERVAL", "2")))
    session = None
    backoff = 2
    completed_basis = None
    history = RecentActions()
    def status(name, **fields):
        write_json(home / "status.json", {"status": name, "updated_at": time.time(),
                                         "adapter": adapter.__name__, **fields})
    try:
        while not stop.is_set():
            try:
                if shutil.disk_usage(home).free < 512 * 1024**2:
                    raise RuntimeError("磁盘剩余空间不足")
                observed_start = time.time()
                start = time.monotonic()
                read = subprocess.run([env["GAMEAGENT_PYTHON"], str(RUNTIME / "environment.py")],
                                      cwd=RUNTIME, env=env, input="null", text=True,
                                      capture_output=True, timeout=7)
                if read.returncode:
                    raise RuntimeError(read.stderr.strip().splitlines()[-1] if read.stderr.strip() else "环境观察失败")
                observation = json.loads(read.stdout)
                elapsed = round((time.monotonic() - start) * 1000)
                basis = (observation.get("session"), observation.get("revision"))
                unchanged = completed_basis is not None and basis == completed_basis
                completed_basis = None
                wait = True
                if not observation["ready"] or not observation.get("actions") or unchanged:
                    reason = observation.get("reason") or ("unresolved_action" if observation.get("pending")
                             else "unchanged_revision" if unchanged else "no_actions")
                    status("waiting", reason=reason, receipt=observation.get("last_result"))
                else:
                    history.select(observation["session"])
                    history.record(observation.get("last_result"))
                    if session is None:
                        session = PiSession(home, model, env, stop)
                        session.request("set_auto_compaction", enabled=False)
                        selected = session.request("get_state")
                        effective = selected.get("model") or {}
                        if (effective.get("id") != model or effective.get("provider") != "gameagent"
                                or selected.get("sessionId") != session.id or selected.get("autoCompactionEnabled") is not False):
                            raise RuntimeError("实际模型或会话与配置不一致")
                        session_file = selected.get("sessionFile")
                    round_id = uuid.uuid4().hex
                    write_json(home / "round.json", {"run_id": round_id, "observation": observation,
                                                     "recent_actions": history.entries(),
                                                      "read_ms": elapsed, "read_started": observed_start,
                                                      "read_finished": observed_start + elapsed / 1000})
                    status("thinking", run_id=round_id, session_id=session.id)
                    receipt, used_basis = session.round(observation)
                    history.select(used_basis[0])
                    history.record(receipt)
                    if receipt and receipt.get("status") == "completed":
                        completed_basis = used_basis
                        wait = False
                    status("reviewed", run_id=round_id, session_id=session.id, receipt=receipt)
                    if session_file and Path(session_file).stat().st_size >= 8 * 1024**2:
                        session.close()
                        session = None
                backoff = 2
                if once:
                    return 0
                if wait:
                    stop.wait(interval)
            except InterruptedError:
                break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                status("recovering", error=str(error))
                if session:
                    session.close()
                    session = None
                if once:
                    return 1
                stop.wait(backoff)
                backoff = min(backoff * 2, 120)
    finally:
        if session:
            session.close()
        if stop.is_set():
            status("stopped")
    return 0
