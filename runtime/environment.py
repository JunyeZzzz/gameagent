"""Adapter boundary shared by automatic observations and model tool calls."""
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from navigation import route_actions

PROJECT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "rejected", "failed", "cancelled"}


def inside(project, path):
    directory = Path(path).resolve()
    if project not in directory.parents or directory.relative_to(project).parts[0] in ("adapters", "runtime"):
        raise ValueError("数据目录必须在独立项目内，并与运行时代码分开")
    return directory


def adapter_for(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ValueError("无效适配器名称")
    path = (PROJECT / "adapters" / (name + ".py")).resolve()
    if path.parent != PROJECT / "adapters" or not path.is_file():
        raise ValueError("找不到项目内适配器：" + name)
    spec = importlib.util.spec_from_file_location(name, path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter


def write_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as file:
        name = file.name
        try:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def observe(adapter, root, bindings=None):
    data = adapter.observe(root)
    if data.get("ready") is not True:
        return {**data, "ready": False}
    for key in ("session", "revision", "goal"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError("缺少观察字段：" + key)
    actions = data.get("actions")
    if not isinstance(actions, list) or any(not isinstance(a, dict) or
            not isinstance(a.get("id"), str) or not a["id"] for a in actions):
        raise ValueError("动作列表无效")
    if len({a["id"] for a in actions}) != len(actions):
        raise ValueError("候选动作 ID 重复")
    sampled = data.get("observed_at")
    ttl = data.get("valid_for")
    if not all(type(x) in (int, float) and math.isfinite(x) for x in (sampled, ttl)) or ttl <= 0:
        raise ValueError("观察缺少有效采样时间或有效期")
    if not -0.5 <= time.time() - sampled <= ttl:
        return {"ready": False, "reason": "stale_observation"}
    if data.get("routes"):
        options = route_actions(data["routes"], actions)
        if bindings is not None:
            bindings.update((option["id"], option["primitive"]) for option in options)
        data = {**data, "actions": actions + options}
    return data


def exchange(adapter, root, request=None):
    # Record the intent before dispatch. On interruption, only matching evidence can release it.
    pending = root / (".pending-" + adapter.__name__ + ".json")
    with (root / ".exchange.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bindings = {}
        data = observe(adapter, root, bindings)
        if pending.exists():
            intent = json.loads(pending.read_text())
            receipt = data.get("last_result") or {}
            if (receipt.get("id") == intent["id"] and receipt.get("session") == intent["session"]
                    and receipt.get("status") in TERMINAL):
                pending.unlink()
            else:
                data = {**data, "pending": intent, "actions": []}
        if request is None:
            return data
        last = data.get("last_result") or {}
        if last.get("id") == request["id"] and last.get("session") == request["session"]:
            return last
        if data.get("pending"):
            return {"status": "rejected", "reason": "unresolved_action", "pending": data["pending"]}
        if (not data["ready"] or time.time() > request["expires_at"] or request["session"] != data["session"]
                or request["revision"] != data["revision"]
                or request["action"] not in {a["id"] for a in data["actions"]}):
            return {"status": "rejected", "reason": "observation_changed"}
        write_json(pending, request)
        try:
            command = request
            if request["action"] in bindings:
                command = {**request, "action": bindings[request["action"]]}
            receipt = adapter.act(root, command)
            status = receipt.get("status")
            if (receipt.get("id") != request["id"] or receipt.get("session") != request["session"]
                    or status not in TERMINAL | {"accepted", "running", "unknown"}):
                raise ValueError("invalid adapter receipt")
            if status in TERMINAL:
                pending.unlink()
            return receipt
        except Exception as error:
            return {"status": "unknown", "id": request["id"], "reason": str(error)}


if __name__ == "__main__":
    root = inside(PROJECT, os.environ["GAMEAGENT_ROOT"])
    adapter = adapter_for(os.environ["GAMEAGENT_ADAPTER"])
    print(json.dumps(exchange(adapter, root, json.load(sys.stdin)), ensure_ascii=False))
