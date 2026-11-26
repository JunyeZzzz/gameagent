"""Sample game I/O independently of model inference, retaining the newest observation."""
from dataclasses import dataclass
import json
from pathlib import Path
import os
import selectors
import subprocess
import threading
import time

from environment import write_json


@dataclass(frozen=True)
class Sample:
    sequence: int
    observation: dict
    started: float
    finished: float


class WorldFeed:
    def __init__(self, home, env, stop, adapter_source, period=.25):
        self.home = home
        self.env = {key: env[key] for key in ("GAMEAGENT_ROOT", "GAMEAGENT_ADAPTER", "PATH") if key in env}
        self.command = [env["GAMEAGENT_PYTHON"], str(Path(__file__).with_name("environment.py")), "--observe-stream"]
        self.reader = None
        self.adapter_source = Path(adapter_source)
        self.reader_generation = None
        self.period = period
        self.stop = stop
        self.condition = threading.Condition()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.latest = None
        self.thread = threading.Thread(target=self._sample, name="world-feed", daemon=True)

    def _close_reader(self):
        reader = self.reader
        self.reader = None
        if reader is not None:
            if reader.poll() is None:
                reader.kill()
            reader.wait()
            reader.stdin.close()
            reader.stdout.close()

    def _read_view(self):
        stat = self.adapter_source.stat()
        generation = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if self.reader is not None and generation != self.reader_generation:
            self._close_reader()
        if self.reader is None:
            self.reader = subprocess.Popen(self.command, env=self.env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.reader_generation = generation
        self.reader.stdin.write(b"observe\n")
        line = bytearray()
        deadline = time.monotonic() + 7
        with selectors.DefaultSelector() as selector:
            selector.register(self.reader.stdout, selectors.EVENT_READ)
            while not self.closed.is_set() and not self.stop.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("game observation timed out")
                if not selector.select(.1):
                    continue
                block = os.read(self.reader.stdout.fileno(), 65536)
                if not block:
                    raise OSError("game observation process exited")
                line.extend(block)
                if len(line) > 512_000:
                    raise ValueError("game observation exceeds tool payload limit")
                if line.endswith(b"\n"):
                    return json.loads(line)
        raise InterruptedError()

    def _sample(self):
        try:
            self._loop()
        finally:
            self._close_reader()

    def _loop(self):
        sequence = 0
        while not self.closed.is_set() and not self.stop.is_set():
            self.wake.clear()
            start = time.time()
            try:
                view = self._read_view()
            except InterruptedError:
                return
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self._close_reader()
                view = {"ready": False, "reason": "observation_unavailable", "error": str(error)[:200]}
            sequence += 1
            now = time.time()
            sample = Sample(sequence, view, start, now)
            with self.condition:
                self.latest = sample
                self.condition.notify_all()
            # This row is the observer's own clock, including while the model is busy.
            state = {k: view.get(k) for k in ("observed_at", "session", "revision", "ready", "reason", "ongoing", "situation")}
            last = view.get("last_result") or {}
            state.update(sequence=sequence, sampled_at=now, pending_id=(view.get("pending") or {}).get("id"),
                         result_id=last.get("id"), result_status=last.get("status"))
            try:
                write_json(self.home / "perception.json", state)
            except OSError:
                pass  # The live in-memory sample remains available.
            self.wake.wait(max(.02, self.period - (time.time() - start)))

    def after(self, sequence):
        with self.condition:
            while not self.stop.is_set():
                if self.latest and self.latest.sequence > sequence:
                    return self.latest
                self.condition.wait(.2)
        raise InterruptedError()

    def close(self):
        self.closed.set()
        self.wake.set()
        self.thread.join(timeout=8)
