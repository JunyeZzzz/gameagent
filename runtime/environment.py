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
from navigation import TRAVEL, route_actions

PROJECT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "rejected", "failed", "cancelled"}
TRAVEL_STEP_LIMIT = 32
TRAVEL_SECONDS = 4.0
TRAVEL_STALL_LIMIT = 3


def situation_key(view):
    return (view.get("situation") or {}).get("key")


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
    # A same-size edit inside one second can match Python's timestamp-based bytecode cache.
    exec(compile(path.read_bytes(), str(path), "exec"), adapter.__dict__)
    return adapter


def write_json(path, value, durable=True):
    # Atomic replace always; fsync only for files that must survive a crash (intents, receipts, task state).
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as file:
        name = file.name
        try:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            if durable:
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
    if any("relation" in a and a["relation"] not in ("parallel", "replace") for a in actions):
        raise ValueError("途中动作关系无效")
    if "situation" in data and (not isinstance(data["situation"], dict) or
                               not isinstance(situation_key(data), str) or not situation_key(data)):
        raise ValueError("现场状态标识无效")
    sampled = data.get("observed_at")
    ttl = data.get("valid_for")
    if not all(type(x) in (int, float) and math.isfinite(x) for x in (sampled, ttl)) or ttl <= 0:
        raise ValueError("观察缺少有效采样时间或有效期")
    if not -0.5 <= time.time() - sampled <= ttl:
        return {"ready": False, "reason": "stale_observation"}
    if data.get("routes"):
        options = route_actions(data["routes"], actions, travel=data.get("travel") is True)
        if bindings is not None:
            bindings.update((option["id"], option["primitive"]) for option in options)
        data = {**data, "actions": actions + options}
    return data


def exchange(adapter, root, request=None):
    # Record the intent before dispatch. On interruption, only matching evidence can release it.
    pending = root / (".pending-" + adapter.__name__ + ".json")
    assessment = root / (".assessment-" + adapter.__name__ + ".json")
    with (root / ".exchange.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bindings = {}
        data = observe(adapter, root, bindings)
        if pending.exists():
            intent = json.loads(pending.read_text())
            receipt = data.get("last_result") or {}
            identity = intent["id"], intent["session"]
            same_world = data.get("ready") and data["session"] == intent["session"]
            reconcile = getattr(adapter, "reconcile_pending", None)
            if same_world and (receipt.get("id"), receipt.get("session")) != identity and callable(reconcile):
                proof = reconcile(root, intent)
                if (isinstance(proof, dict) and (proof.get("id"), proof.get("session")) == identity
                        and proof.get("status") in TERMINAL):
                    receipt = data["last_result"] = proof
            if (receipt.get("id"), receipt.get("session")) == identity and receipt.get("status") in TERMINAL:
                pending.unlink()
                assessment.unlink(missing_ok=True)
            elif data.get("ready") and not same_world:
                # This session cannot actuate the previous world, whose physical outcome is unknown.
                write_json(root / (".detached-" + adapter.__name__ + ".json"),
                           dict(intent=intent, reason="session_changed", effect="unknown"))
                pending.unlink()
                assessment.unlink(missing_ok=True)
            else:
                ongoing = data.get("ongoing") or {}
                matches = (
                    data["ready"] and receipt.get("id") == intent["id"]
                    and data.get("session") == receipt.get("session") == intent["session"]
                    and receipt.get("status") == "running"
                )
                scoped = matches and ongoing.get("id") == intent["id"]
                can_review = scoped and callable(getattr(adapter, "review", None))
                controls = list(data["actions"]) if can_review else []
                if same_world:
                    if not (can_review and ongoing.get("requires_action")) and not any(a["id"] == "review:keep" for a in controls):
                        controls.append({"id": "agent:continue", "description": "保持当前意图，继续观察最新状态。"})
                    if matches and callable(getattr(adapter, "cancel", None)):
                        controls.append({"id": "agent:cancel", "description": "请求终止当前意图，收到反馈后另选行动。"})
                if not scoped:
                    ongoing = {"id": intent["id"], "decision_revision": data.get("revision"), "action": intent["action"],
                               "trigger": receipt.get("status", "awaiting_receipt")
                               if receipt.get("id") == intent["id"] else "awaiting_receipt"}
                data = {**data, "pending": intent, "ongoing": ongoing, "actions": controls}
                if assessment.exists():
                    prior = json.loads(assessment.read_text())
                    if (prior.get("parent_id"), prior.get("session")) == (intent["id"], intent["session"]):
                        actual = data.get("last_review") or {}
                        if actual.get("id") == prior["id"] and actual.get("parent_id") == intent["id"]:
                            prior["status"] = actual.get("status", prior["status"])
                        data["last_assessment"] = prior
        elif data.get("ongoing"):
            data = {**data, "actions": []}
        if request is None:
            return data
        last = data.get("last_result") or {}
        if last.get("id") == request["id"] and last.get("session") == request["session"]:
            return last
        parent = request.get("parent_id")
        if parent and (data.get("last_review") or {}).get("id") == request["id"] and (
                data["last_review"].get("parent_id"), data["last_review"].get("session")) == (parent, request["session"]):
            return data["last_review"]
        if data.get("pending") and parent != data["pending"]["id"]:
            return {"status": "rejected", "reason": "unresolved_action", "pending": data["pending"]}
        if parent and (data.get("pending") or {}).get("id") != parent:
            return {"status": "rejected", "reason": "parent_ended"}
        choice = next((a for a in data["actions"] if a["id"] == request["action"]), None) if data["ready"] else None
        if (not data["ready"] or time.time() > request["expires_at"] or request["session"] != data["session"]
                or ("goal" in request and request["goal"] != data["goal"])
                or request.get("goal_id") != data.get("goal_id")
                or request["revision"] != data["revision"]
                or ("situation_key" in request and request["situation_key"] != situation_key(data))
                or choice is None):
            return {"status": "rejected", "reason": "observation_changed"}
        if parent:
            # Adapter reviews share the parent handle, including native mode replacements.
            previous = data.get("last_assessment") or {}
            if previous.get("id") == request["id"]:
                return previous
            try:
                if request["action"] == "agent:continue":
                    receipt = dict(request, status="completed", reason="intent_maintained")
                elif request["action"] == "agent:cancel":
                    receipt = adapter.cancel(root, data["pending"], request)
                else:
                    receipt = adapter.review(root, request)
            except (OSError, ValueError) as error:
                receipt = dict(request, status="unknown", reason=str(error))
            watch = data["ongoing"]
            record = {k: request[k] for k in ("id", "session", "parent_id", "action", "subgoal", "situation_key") if k in request}
            record.update(status=receipt["status"], assessed_at=time.time(),
                          context={k: watch[k] for k in ("trigger","distance_m","stationary_s") if k in watch})
            if choice.get("relation"):
                receipt = {**receipt, "relation": choice["relation"]}
                record["relation"] = choice["relation"]
            if receipt["status"] == "accepted":
                record.update(goal=data["goal"], goal_id=data.get("goal_id"),
                              expires_at=min(request["expires_at"], receipt.get("expires_at", request["expires_at"])))
            if "reason" in receipt:
                record["reason"] = receipt["reason"]
            write_json(assessment, record)
            return receipt
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


def travel_stop(start, view, target):
    """Why a travel must stop before its next step, or None with the route to follow."""
    if not view.get("ready"):
        return view.get("reason") or "not_ready", None
    if view.get("pending"):
        return "action_pending", None
    if any(start.get(key) != view.get(key) for key in ("session", "goal", "goal_id", "viewer_id")):
        return "goal_changed", None
    if situation_key(start) != situation_key(view):
        return "situation_changed", None
    if (view.get("ongoing") or {}).get("requires_action"):
        return "action_required", None
    route = (view.get("routes") or {}).get(target)
    if not isinstance(route, dict) or type(route.get("distance")) not in (int, float):
        return "route_lost", None
    if route["distance"] <= 0:
        return "arrived", route
    if route.get("first_action") not in {action["id"] for action in view["actions"]}:
        return "route_lost", None
    return None, route


def travel(adapter, root, request):
    """Follow the route to one landmark, one ordinary intent per native step.

    Each step is written, deduplicated and receipted exactly like a single action, and the
    route is recomputed from a fresh observation after every step. The plan itself is not
    persisted: after a crash only the in-flight step is reconciled and the model decides again.
    """
    target = json.loads(request["action"][len(TRAVEL):])
    start = exchange(adapter, root)
    if not start.get("ready") or start.get("pending"):
        return exchange(adapter, root, request)
    receipt = exchange(adapter, root, request)
    receipts = [receipt]
    deadline = time.monotonic() + TRAVEL_SECONDS
    best = ((start.get("routes") or {}).get(target) or {}).get("distance")
    stalled = 0
    view = start
    while True:
        if receipt.get("status") != "completed":
            stopped = "step_" + str(receipt.get("status") or "unknown")
            break
        view = exchange(adapter, root)
        stopped, route = travel_stop(start, view, target)
        if stopped:
            break
        if type(best) in (int, float) and route["distance"] >= best:
            stalled += 1
        else:
            stalled = 0
        best = route["distance"] if best is None else min(best, route["distance"])
        if stalled >= TRAVEL_STALL_LIMIT:
            stopped = "no_progress"
        elif len(receipts) >= TRAVEL_STEP_LIMIT:
            stopped = "step_limit"
        elif time.monotonic() >= deadline:
            stopped = "time_limit"
        if stopped:
            break
        step = dict(id=f'{request["id"]}:{len(receipts)}', action=route["first_action"],
                    session=view["session"], goal=view["goal"], revision=view["revision"],
                    expires_at=view["observed_at"] + view["valid_for"])
        for key in ("goal_id", "subgoal"):
            if request.get(key) is not None:
                step[key] = request[key]
        if situation_key(view) is not None:
            step["situation_key"] = situation_key(view)
        receipt = exchange(adapter, root, step)
        receipts.append(receipt)
    done = [r for r in receipts if r.get("status") == "completed"]
    summary = dict(target=target, native_steps=len(done), native_actions=[r.get("action") for r in done],
                   stopped=stopped)
    left = ((view.get("routes") or {}).get(target) or {}).get("distance")
    if stopped != "arrived" and type(left) in (int, float):
        summary["distance_left"] = left
    result = dict(receipt, action=request["action"], travel=summary)
    if done and isinstance(done[0].get("before"), dict):
        result["before"] = done[0]["before"]
    return result


def dispatch(adapter, root, request=None):
    if isinstance(request, dict) and str(request.get("action", "")).startswith(TRAVEL) and not request.get("parent_id"):
        return travel(adapter, root, request)
    return exchange(adapter, root, request)


if __name__ == "__main__":
    root = inside(PROJECT, os.environ["GAMEAGENT_ROOT"])
    adapter = adapter_for(os.environ["GAMEAGENT_ADAPTER"])
    if sys.argv[1:] == ["--observe-stream"]:
        for line in sys.stdin:
            if line.strip() != "observe":
                raise ValueError("invalid observation request")
            print(json.dumps(exchange(adapter, root), ensure_ascii=False), flush=True)
    elif sys.argv[1:] == ["--serve"]:
        # Tool calls: one JSON request per line (null observes). Any error ends the worker.
        for line in sys.stdin:
            print(json.dumps(dispatch(adapter, root, json.loads(line)), ensure_ascii=False), flush=True)
    else:
        print(json.dumps(dispatch(adapter, root, json.load(sys.stdin)), ensure_ascii=False))
