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

from environment import exchange, situation_key, write_json
from recent_actions import CatalogFindings, RecentActions
from task_state import TaskState
from world_feed import WorldFeed

RUNTIME = Path(__file__).resolve().parent
SYSTEM = """你是通用游戏 Agent。根据当前观察里的用户目标、现场事实、可选动作和上次结果，决定下一步。
委托模式按 goal 及相关前文理解目标和约束。用户改口时遵循最新要求，旧回复可用于了解进度。用户只问当前情况时，用 game_observe 说明现场。自主游玩时按下面的观众对话约定回应。
只能提交当前观察或 game_lookup 提供的候选。按候选原样填写 choice 或动作 ID，也可以重新观察或等待。每轮最多提交一个动作。
已有可执行的下一步就直接用 game_schedule，下发理由写进 subgoal。查证和观察可与本轮下发一起进行。动作在途时，每轮按最新现场判断整件目标是否还在推进。路线合适时用 game_observe 填写 assessment 和 hold: true，游戏沿原路线继续执行。
地点或秘籍已在快照里提供可提交的动作 ID 时直接使用，包括现场候选和 catalog_matches_in_goal。缺少 ID 时用 game_lookup 查 queryable_catalogs。重用该目标已查证的结果；no_match 表示当前目录没有匹配。只使用确实能帮助当前步骤的秘籍。
秘籍目录标明仅后台执行时，使用当前可提交的后台候选。具体秘籍标为后台不可用时，直接报告该项限制，继续执行目标中可完成的部分；没有可执行的部分时说明现状并保持观察。
以当前现场和已确认的动作效果判断进度；旧计划只是意图。先核对哪些前提已满足，再选尚未完成的目标。
用户对动作顺序、重合时段或触发条件的要求必须落实到当前这一步。用户要求移动途中改变状态时，第一步启动移动，之后选保留移动的途中动作，即使相同效果现在也可单独执行。
途中还有多步尚未完成、且用户未指定终点时，选留有足够行进距离的路线，避免在下一步到来前就走完。途中动作来不及提交且移动已结束时，先恢复移动，再在途中完成余下的步骤。
task_progress 里的已确认结果、拒绝和当前障碍来自现场。选择能改变失败条件的办法，等待输入恢复只适用于已经找到所需输入的情形。
用户明确指定的对象、地点和次数属于目标。只有用户已允许时才替换它们。指定对象失效或现场不满足条件时，用观察判断说明原因，等待条件或目标变化。
completion_checks 是整个当前目标结束时才验收的条件，中间步骤根据现场使用所需装备和载具。
用 game_observe 的 assessment 简述观察依据。仅需维持或原地观察时，同时填写 hold: true。委托模式的一次性目标全部达成且没有动作进行时，填写 completed_after: "latest"，运行时核对当前观察里的最后一步回执。如果最后一步是按要求主动结束动作，使用游戏已确认的停止回执。新目标开始前的回执可供了解现场，新要求的动作需要取得新的结果。委托模式收到「停下等我」时，游戏确认停稳就用 completed_after: "latest" 结束这条指令，继续接收新的指令。
涉及移动时，核对当前位置、朝向和障碍。核对距离要求时区分累计路程、起终点直线距离和剩余距离，改路线也以指令约定的起点为准。若有通往所选地标的单步路线动作，优先提交该动作的完整 ID。前往地标的连续路线候选一次提交即走到该地标；回执的 travel 给出实际执行的原生动作和 stopped 停下原因，未到达时依据最新现场重新选择。
距离为零表示已经到达该地标的指定位置，再根据目标选择交互。路线只负责移动，目的地和交互由你选择。
动作执行期间参考最新观测和上次判断，简述维持或干预的依据。遇到短暂停顿先核对进展，能在原意图内修正时优先修正。
ongoing.game_control_state 描述游戏当前的接单或执行阶段。依据本轮候选选择操作。当前只可观察时，报告正在执行的事项和现场变化。
可用工具能补齐目标需要的条件时，自行比较使用工具和现场已有资源。比较实际效果，取得资源后继续原目标；保留与目标无关的开关和世界状态。
等待时说明要等的具体变化。没有进行中动作且所需能力在 blocked_capabilities 里时，若有符合目标的替代行动就行动，否则用 game_observe 的 assessment 和 wait_for 登记其中的 id。用 recheck_after_s 记录目标明确要求的时间节点。行动期间运行时会继续按新鲜现场复核。保留目标要求的前置条件和保护，直到依赖它们的步骤已确认。重生后核对哪些结果仍然成立。
动作 subgoal 写下所选意图和促成它的一条现场事实，供游戏内显示。重复经过相同状态或动作没有效果时，根据现有事实重新选路。结果未知时等待核实。"""

AUTONOMY = """
处理执行命令时，把用户明确交代的内容当作约束。用户留给你的细节由你根据现场决定，包括主动提出和调整子目标。
先理解整件事期望得到的结果或过程。用户说「边 A 边 B」「在 A 途中做 B」时，A 与 B 的重合是验收条件。优先选能一起开始的动作；否则先确认 A 已启动，再在同一个 A 动作中提交 B。一次动作的回执只证明那一步。
用户说「等 X 后再做 Y」时，把实际观察到 X 作为 Y 的前提。如果目标包含经历 X，先用现场已有的条件推进；执行前先做 Y 或换掉指定对象，可能让 X 无法发生。仅当当前条件已经无法推进时，再考虑用户允许的替代办法。
每次复核比较实际效果与预期。状态合适时维持并明确等待的信号；机会、风险或进展偏离时，可以换办法或先做有帮助的一步。整件委托满足后才报告完成。
有「现场变化」时，把这项变化和用户目标一起判断，重新选择维持、暂缓或切换当前做法。用提交理由或观察判断指出影响这次选择的现场事实。候选的 relation 为 parallel 表示可同时做，replace 表示交接当前执行方式，两种都按一次操作提交。"""

TASK_MEMORY = """
快照外的「当前任务」是上一轮已经保存的策略判断。它是可修正的预期和意图，游戏事实以新快照与回执为准。开始执行命令并首次形成策略、或策略实质改变时，在本轮已有的 game_schedule 或带 assessment 的 game_observe 里填写 task。用 intent、expected、revisit、finish_when 写清当前意图、尚未确认的并行步骤、重新判断条件和收尾证据。维持一项已开始的动作时，仍核对同一目标下剩余步骤。需要时间触发复核时，在 task 填 recheck_after_s。review_due 为真表示先前登记的时间已经过去；如果只是等足指定时间且现场满足要求，执行下一步。review_remaining_s 是剩余秒数，review_elapsed_s 是登记后经过的秒数。策略更新可随动作一起提交。"""
SYSTEM += AUTONOMY + TASK_MEMORY
SYSTEM += """
ongoing.feedback 和最近动作的 progress_feedback 是适配器根据游戏数据给出的途中反馈。按动作声明的效果核实进展；同一种方法出现相同反馈时，换目标能改变失败前提才值得再试。秘籍回执只证明其注明的效果和来源。发现预期已落空且有可行下一步时，立即调整。"""
SYSTEM += """
游戏快照的 autoplay 为真时，你是快照 game 所指游戏里正在玩的玩家。conversation 里是看你玩的观众说过的话。对话用来理解问题、可采纳的建议和明确要求。普通提问和建议期间，持续判断自己所处的局面，并维持有用的现行操作。你自己决定下一步玩什么，活动到点就根据现场接着玩。明确要求停下、改变玩法或交还控制时优先遵循。没有新消息也继续游玩。
有没答的新留言时，用本轮 game_observe 或 game_schedule 的 viewer_reply 简短回应。建议涉及可并行的效果时，沿用当前活动，处理该效果并核对游戏证据。如果已确认观众想要的能力不可用，说清原因一次，按现场继续玩。弹药耗尽、路线受阻、角色失去装备或现场出现变化时，依据最新候选恢复并推进游戏。用 task 记自己正在尝试的玩法；随现场调整，无需重复汇报。完成一项活动后继续决定下一件事。"""
SYSTEM += """
自主游玩中的活动结果用 assessment 或随下一步动作提交的 viewer_reply 报告一次，省略 completed_after。大招回执注明 awaiting_effect 时表示已启动，后续 effect_outcome 是这一次施放的实际结算，partial 表示演出已结束且部分结果未确认。回应当前留言的施放结果要核对结算的 viewer_id 与当前留言相同；无归属或旧留言的结果属于现场历史。游戏交还控制且下一步可用时直接继续，本轮可以同时回复观众。"""


class WorldChanged(Exception):
    """The game invalidated a model round before it committed an action."""

    def __init__(self, replace_session=False):
        self.replace_session = replace_session


def goal_identity(view):
    return view.get("session"), view.get("goal"), view.get("goal_id")


def matches_goal_evidence(view, receipt, prior):
    return (view.get("ready") and not view.get("pending") and receipt.get("evidence_id")
            and receipt["evidence_id"] != prior
            and (view.get("last_result") or {}).get("id") == receipt["evidence_id"]
            and goal_identity(view) == goal_identity(receipt)
            and all(c.get("met") is True for c in view.get("completion_checks", [])))


def direct_user_completion(view, prior):
    assessment = view.get("direct_user_completion")
    last = view.get("last_result") or {}
    if (not isinstance(assessment, str) or not assessment or not view.get("goal_id")
            or last.get("goal_id") != view["goal_id"] or last.get("session") != view.get("session")
            or last.get("status") != "cancelled" or (last.get("interruption") or {}).get("confirmed") is not True):
        return None
    receipt = dict(session=view["session"], goal=view["goal"], goal_id=view["goal_id"],
                   evidence_id=last.get("id"), assessment=assessment, reason="game_goal_satisfied")
    return receipt if matches_goal_evidence(view, receipt, prior) else None


def dispatch_direct_control(adapter, root, intent):
    receipt = exchange(adapter, root, intent)
    if (receipt.get("status"), receipt.get("reason")) != ("rejected", "observation_changed"):
        return receipt
    latest = exchange(adapter, root)
    if (not latest.get("ready") or goal_identity(latest) != goal_identity(intent)
            or (latest.get("pending") or {}).get("id") != intent["parent_id"]
            or ("situation_key" in intent and intent["situation_key"] != situation_key(latest))
            or latest.get("direct_user_action") != intent["action"]
            or not any(action["id"] == intent["action"] for action in latest["actions"])):
        return receipt
    return exchange(adapter, root, dict(intent, revision=latest["revision"],
                    expires_at=latest["observed_at"] + latest["valid_for"]))


def decision_key(view):
    parent = view.get("pending") or {}
    ongoing = view.get("ongoing") or {}
    last = view.get("last_result") or {}
    relevant = ongoing.get("depends_on", ())
    capabilities = sorted((item["id"], item.get("reason") or "")
                          for item in view.get("blocked_capabilities", ())
                          if not parent or item["id"] in relevant)
    checks = tuple((check.get("field"), check.get("met")) for check in view.get("completion_checks", ()))
    stop_confirmed = last.get("status") == "cancelled" and (last.get("interruption") or {}).get("confirmed") is True
    base = (*goal_identity(view), last.get("id"), last.get("status"), stop_confirmed, capabilities, checks,
            situation_key(view))
    if parent:
        controls = ongoing.get("control_families")
        if controls is None:
            controls = [a["id"] for a in view.get("actions", [])]
        return (*base, parent["id"], ongoing.get("trigger"), ongoing.get("style"), ongoing.get("activity"),
                ongoing.get("decision_revision"), tuple(sorted(controls)), view.get("decision_event"))
    return (*base, view.get("decision_event", view.get("revision")))


def superseded(original, current):
    if not current.get("ready"):
        return False
    if (goal_identity(original) != goal_identity(current)
            or original.get("viewer_id") != current.get("viewer_id")
            or (original.get("pending") or {}).get("id") != (current.get("pending") or {}).get("id")):
        return True
    if ((current.get("ongoing") or {}).get("requires_action") is True
            and (original.get("ongoing") or {}).get("requires_action") is not True):
        return True
    last = current.get("last_result") or {}
    if last.get("status") not in ("completed", "failed", "cancelled", "rejected"):
        return False
    previous = original.get("last_result") or {}
    fields = ("id", "status", "effect_outcome")
    return (any(last.get(field) != previous.get(field) for field in fields)
            or ((last.get("interruption") or {}).get("confirmed") is True
                and (previous.get("interruption") or {}).get("confirmed") is not True))


def control_wait_reason(view):
    pending = view.get("pending")
    if not pending or any(action["id"] != "agent:continue" for action in view["actions"]):
        return None
    now = time.time()
    result = view.get("last_result") or {}
    if (result.get("status") == "accepted" and result.get("id") == pending["id"]
            and result.get("session") == pending.get("session") and goal_identity(pending) == goal_identity(view)
            and now < min(pending.get("expires_at", 0), result.get("claim_deadline", float("inf")))):
        return "action_start"
    control = view.get("last_review") or {}
    assessment = view.get("last_assessment") or {}
    if (not control.get("id") or control["id"] != assessment.get("id")
            or goal_identity(assessment) != goal_identity(view)
            or result.get("status") != "running" or result.get("id") != pending["id"]
            or result.get("session") != view.get("session") or assessment.get("parent_id") != pending["id"]):
        return None
    if control.get("status") == "accepted" and now < assessment.get("expires_at", 0):
        return "control_receipt"
    ongoing = view.get("ongoing") or {}
    if (control.get("status") == "completed" and ongoing.get("id") == pending["id"]
            and ongoing.get("control_effect_pending") is True):
        return "control_effect"
    return None


def wait_condition(view, capability):
    if not view.get("ready") or view.get("pending"):
        return None
    blocker = next((b for b in view.get("blocked_capabilities", []) if b["id"]==capability), None)
    if blocker is None:
        return None
    last = view.get("last_result") or {}
    return (*goal_identity(view), last.get("id"), last.get("status"),
            blocker.get("wake_on", view.get("decision_event", view.get("revision"))),
            [c.get("met") for c in view.get("completion_checks", [])])


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
                if event.get("type") in {"response", "message_end", "agent_settled", "tool_execution_end", "tool_execution_start"}:
                    self.events.put(event)
        finally:
            self.events.put({"type": "eof"})

    def next_event(self, deadline, changed=None):
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pi 超时，请从环境核实动作结果")
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                if changed and changed():
                    return {"type": "world_changed"}
                try:
                    event = self.events.get(timeout=min(remaining, 0.10))
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

    def round(self, observation, changed, record_lookup=None):
        basis = (observation["session"], observation["revision"])
        request_id = self.send("prompt", message="根据本轮游戏快照决定下一步。")
        deadline = time.monotonic() + 120
        acknowledged = settled = False
        truncated = False
        action_receipt = observation_receipt = None
        abort_id = None
        abort_ack = committing = False
        while not (acknowledged and settled and (not abort_id or abort_ack)):
            try:
                event = self.next_event(deadline, changed if not (committing or abort_id) else None)
            except TimeoutError:
                if abort_id:
                    raise WorldChanged(replace_session=True)
                raise
            kind = event["type"]
            if kind == "world_changed":
                abort_id = self.send("abort")
                deadline = min(deadline, time.monotonic() + .75)
            elif kind == "tool_execution_start" and event.get("toolName") == "game_schedule":
                committing = True
            elif abort_id and kind == "response" and event.get("id") == abort_id:
                abort_ack = True
            elif kind == "response" and event.get("id") == request_id:
                if not event.get("success") or event.get("data", {}).get("disposition") != "started":
                    raise RuntimeError(event.get("error", "模型轮次未启动"))
                acknowledged = True
            elif kind == "tool_execution_end" and event.get("toolName") == "game_schedule":
                candidate = (event.get("result") or {}).get("details")
                if candidate and candidate.get("reason") != "action_already_submitted":
                    if action_receipt is None or candidate.get("status") != "rejected":
                        action_receipt = candidate
                committing = bool(action_receipt and action_receipt.get("status") != "rejected")
            elif kind == "tool_execution_end" and event.get("toolName") == "game_observe":
                fresh = (event.get("result") or {}).get("details") or {}
                if fresh.get("ready"):
                    basis = (fresh["session"], fresh["revision"])
                    if fresh.get("status") in ("goal_satisfied", "rejected") or (
                            fresh.get("status") == "observing" and
                            (observation_receipt or {}).get("status") not in ("goal_satisfied", "rejected")):
                        earlier_task = (observation_receipt or {}).get("task")
                        if fresh["status"] == "observing" and earlier_task and not fresh.get("task"):
                            fresh = {**fresh, "task": earlier_task}
                        observation_receipt = fresh
            elif kind == "tool_execution_end" and event.get("toolName") == "game_lookup" and record_lookup:
                record_lookup((event.get("result") or {}).get("details"))
            elif kind == "message_end":
                message = event.get("message") or {}
                if message.get("stopReason") == "length":
                    truncated = True
                if not abort_id and message.get("stopReason") in {"aborted", "error"}:
                    raise RuntimeError(message.get("errorMessage") or message["stopReason"])
            elif kind == "agent_settled":
                settled = True
        if abort_id:
            raise WorldChanged()
        if action_receipt and action_receipt["status"] in ("accepted", "running", "completed"):
            for key in ("task", "viewer_reply"):
                if not action_receipt.get(key) and (observation_receipt or {}).get(key):
                    action_receipt[key] = observation_receipt[key]
        return action_receipt or observation_receipt, basis, truncated

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
    idle_interval = max(0, int(os.getenv("GAMEAGENT_INTERVAL", "0"))) or float("inf")
    feed = WorldFeed(home, env, stop, adapter.__file__)
    feed.thread.start()
    session = None
    history = RecentActions()
    findings = CatalogFindings()
    sequence = 0
    reviewed = None
    next_review = 0
    retry_at = 0
    backoff = 1
    last_status = None
    satisfied_goal = None
    assessment = None
    goal_scope = None
    direct_action_scope = None
    last_terminal = None
    prior_goal_result = None
    task = TaskState(home / "task.json")
    last_judgment = None
    viewer_reply = None
    situation_session = None
    judged_situation = None
    read_after = 0
    watch = None
    correction = None
    wait_log = home / "dashboard-waits.jsonl" if env.get("GAMEAGENT_DASHBOARD") == "1" else None
    recorded_wait = None

    def trace_wait(reason=None, detail=None, wake=None):
        nonlocal recorded_wait
        if wait_log is None or recorded_wait == (reason, detail):
            return
        try:
            with wait_log.open("a", encoding="utf-8") as out:
                out.write(json.dumps(dict(at=time.time(), reason=reason, detail=detail, wake=wake), ensure_ascii=False) + "\n")
        except OSError:
            return
        recorded_wait = reason, detail

    def status(name, **fields):
        nonlocal last_status
        wait_reason = {
            "monitoring": "goal_monitoring",
            "waiting": "observation_wait",
            "watching": "capability_blocked",
            "recovering": "retry_backoff",
        }.get(name)
        if name == "observing":
            wait_reason = fields.get("wait_reason") or "waiting_for_change"
        trace_wait(wait_reason, fields.get("wait_detail") or fields.get("blocker")
                   or (fields.get("reason") if name == "waiting" else None), fields.get("wake_reason"))
        if goal_scope:
            fields.setdefault("goal", goal_scope[1])
            if goal_scope[2] is not None:
                fields.setdefault("goal_id", goal_scope[2])
            if task.record:
                fields.setdefault("task", task.record["brief"])
        if viewer_reply:
            fields.setdefault("viewer_reply", viewer_reply)
        data = dict(status=name, adapter=adapter.__name__, **fields)
        if data != last_status:
            write_json(home / "status.json", dict(data, updated_at=time.time()), durable=False)
            last_status = data

    try:
        while not stop.is_set():
            try:
                sample = feed.after(sequence)
                sequence = sample.sequence
                if sample.started < read_after:
                    feed.wake.set()
                    continue
                observation = sample.observation
                key = decision_key(observation)
                now = time.monotonic()
                if observation.get("ready"):
                    scope = goal_identity(observation)
                    if scope != goal_scope:
                        last_judgment = None
                        findings = CatalogFindings()
                        correction = None
                        prior_goal_result = last_terminal if goal_scope and goal_scope[0] == scope[0] else None
                        last_terminal = None
                        goal_scope = scope
                        task.select(scope)
                    last = observation.get("last_result") or {}
                    if last.get("session") == scope[0] and last.get("status") in ("completed", "failed", "cancelled", "rejected"):
                        last_terminal = last.get("id")
                if satisfied_goal and observation.get("ready"):
                    same_goal = goal_identity(observation) == goal_identity(satisfied_goal)
                    if (same_goal and not observation.get("pending")
                            and all(c.get("met") is True for c in observation.get("completion_checks", []))):
                        status("monitoring", **satisfied_goal)
                        if once:
                            return 0
                        continue
                    satisfied_goal = None
                if not observation.get("ready"):
                    watch = None
                    status("waiting", reason=observation.get("reason") or "no_actions",
                           receipt=observation.get("last_result"))
                    reviewed = None
                    if once:
                        return 1 if observation.get("reason") == "observation_unavailable" else 0
                    continue
                situation = observation.get("situation")
                if situation_session != observation["session"]:
                    situation_session = observation["session"]
                    previous = observation.get("last_assessment") or observation.get("pending") or {}
                    judged_situation = {"key": previous["situation_key"]} if "situation_key" in previous else situation
                situation_changed = situation_key({"situation": judged_situation}) != situation_key(observation)
                completion = direct_user_completion(observation, prior_goal_result)
                if completion:
                    history.select(observation["session"])
                    history.record(observation["last_result"])
                    satisfied_goal = dict(completion, assessed_sequence=sequence)
                    if observation.get("viewer_id"):
                        viewer_reply = dict(id=observation["viewer_id"], text=completion["assessment"])
                    reviewed = correction = None
                    status("monitoring", **satisfied_goal)
                    if once:
                        return 0
                    continue
                direct = observation.get("direct_user_action")
                parent_id = (observation.get("pending") or {}).get("id")
                if (direct and parent_id and goal_scope[2] and goal_scope != direct_action_scope
                        and any(a["id"] == direct for a in observation["actions"])):
                    direct_action_scope = goal_scope
                    # Keep the same validation, native actuator and receipts as a model-issued review.
                    identity = json.dumps((*goal_scope, parent_id, direct), ensure_ascii=False)
                    intent = dict(id=str(uuid.uuid5(uuid.NAMESPACE_OID, identity)), action=direct,
                                  session=observation["session"], revision=observation["revision"],
                                  **({"situation_key": situation_key(observation)} if situation else {}),
                                  goal=observation["goal"], goal_id=observation["goal_id"], parent_id=parent_id,
                                  expires_at=observation["observed_at"] + observation["valid_for"],
                                  subgoal="按用户指令执行当前操作")
                    receipt = dispatch_direct_control(adapter, Path(env["GAMEAGENT_ROOT"]), intent)
                    dispatched = receipt.get("status") in {"accepted", "running", "completed"}
                    if dispatched:
                        reviewed = watch = None
                        last_judgment = dict(text=intent["subgoal"], at=time.time(), parent_id=parent_id)
                        status("reviewed", reason="direct_user_control", parent_id=parent_id,
                               receipt=receipt, last_judgment=last_judgment)
                        if once:
                            return 0
                    if dispatched or (receipt.get("status"), receipt.get("reason")) == ("rejected", "observation_changed"):
                        read_after = time.time()
                        feed.wake.set()
                        continue
                task_due = task.due()
                active_play = bool(parent_id) or observation.get("continuous_activity") is True or observation.get("autoplay") is True
                if watch:
                    capability, expected, watched_key, deadline = watch
                    if (not active_play and not task_due and now < deadline and key == watched_key
                            and wait_condition(observation, capability) == expected):
                        blocker = next(b for b in observation["blocked_capabilities"] if b["id"]==capability)
                        status("watching", reason="capability_blocked", capability=capability,
                               assessment=assessment["assessment"], blocker=blocker["reason"]+" · "+blocker["resumes_when"])
                        if once:
                            return 0
                        continue
                    watch = None
                    reviewed = None
                if now < retry_at or (not active_play and key == reviewed and now < next_review and not task_due):
                    if now >= retry_at:
                        waiting_for = ((task.view() or {}).get("revisit") or (last_judgment or {}).get("text")
                                       or "等待游戏进展或动作回执")
                        status("observing", reason="waiting_for_change", last_judgment=last_judgment,
                               wait_reason=("scheduled_recheck" if next_review < float("inf") or task.has_deadline() else
                                            "waiting_for_change"),
                               wait_detail=waiting_for)
                    if once:
                        return 0
                    continue
                if shutil.disk_usage(home).free < 512 * 1024**2:
                    raise RuntimeError("磁盘剩余空间不足")
                history.select(observation["session"])
                history.record(observation.get("last_result"))
                history.record_progress(observation)
                if session is None:
                    trace_wait("model_initializing")
                    session = PiSession(home, model, env, stop)
                    session.request("set_auto_compaction", enabled=False)
                    selected = session.request("get_state")
                    effective = selected.get("model") or {}
                    if (effective.get("id") != model or effective.get("provider") != "gameagent"
                            or selected.get("sessionId") != session.id or selected.get("autoCompactionEnabled") is not False):
                        raise RuntimeError("实际模型或会话与配置不一致")
                    session_file = selected.get("sessionFile")
                # Initialization may have taken longer than a game event.
                latest = feed.latest
                if latest.sequence != sequence and (not latest.observation.get("ready")
                                                    or superseded(observation, latest.observation)):
                    continue
                round_id = uuid.uuid4().hex
                if assessment and goal_identity(assessment) != goal_identity(observation):
                    assessment = None
                control_state = control_wait_reason(observation)
                if control_state:
                    ongoing = observation.get("ongoing") or {}
                    observation = {**observation, "ongoing": {**ongoing, "game_control_state": control_state}}
                write_json(home / "round.json", {"run_id":round_id, "observation":observation, "prior_goal_result":prior_goal_result,
                           "situation_change":{"previous": judged_situation} if situation_changed else None,
                           "task":task.view(),
                           "viewer_reply":viewer_reply if viewer_reply and viewer_reply["id"] == observation.get("viewer_id") else None,
                           "catalog_findings":findings.current(observation),
                           "rejection":correction[1] if correction and correction[0] == key else None,
                           "assessment":{k:assessment[k] for k in ("assessment","reason") if assessment.get(k)} if assessment else None,
                           "recent_actions":history.entries(), "read_ms":round((sample.finished-sample.started)*1000),
                           "read_started":sample.started, "read_finished":sample.finished})
                if situation_changed:
                    wake = "现场发生了影响策略的变化"
                elif key == reviewed:
                    wake = "根据最新现场继续判断" if active_play else "复核时间到"
                else:
                    wake = (observation.get("ongoing") or {}).get("feedback")
                    if not wake and recorded_wait and recorded_wait[0]:
                        wake = "当前动作或关键现场发生变化"
                status("thinking", run_id=round_id, session_id=session.id, parent_id=parent_id,
                       last_judgment=last_judgment, wake_reason=wake)
                receipt, used_basis, truncated = session.round(
                    observation, lambda: superseded(observation, feed.latest.observation), findings.record)
                if receipt and receipt.get("viewer_reply") and observation.get("viewer_id"):
                    viewer_reply = dict(id=observation["viewer_id"], text=receipt["viewer_reply"])
                latest = feed.latest.observation
                if (receipt and receipt.get("status") in ("observing", "accepted", "running", "completed", "goal_satisfied")
                        and goal_identity(latest) == goal_scope and situation_key(latest) == situation_key(observation)):
                    judged_situation = situation
                task_updated = False
                if receipt and receipt.get("task") and goal_identity(latest) == goal_scope:
                    if receipt["status"] != "observing" or (latest.get("ready")
                            and situation_key(observation) == situation_key(latest)
                            and not superseded(observation, latest)):
                        task_updated = task.update(goal_scope, round_id, receipt)
                if task_due and not task_updated:
                    task.clear_deadline()
                explicit_recheck = None
                if receipt:
                    assessment = receipt if receipt.get("status") == "observing" else None
                    if not active_play and assessment and assessment.get("recheck_after_s") and not task_updated:
                        explicit_recheck = time.monotonic() + assessment["recheck_after_s"]
                    if not active_play and assessment and assessment.get("wait_for"):
                        capability = assessment["wait_for"]
                        expected = wait_condition(observation, capability)
                        if expected is not None:
                            watch = (capability, expected, key, explicit_recheck or float("inf"))
                    public = receipt.get("assessment") or receipt.get("subgoal")
                    if public and receipt.get("status") in ("observing", "accepted", "completed", "goal_satisfied"):
                        last_judgment = dict(text=public, at=time.time(), parent_id=parent_id or receipt.get("id"))
                if receipt and receipt.get("status") == "goal_satisfied":
                    latest = feed.latest.observation
                    if not matches_goal_evidence(latest, receipt, prior_goal_result):
                        continue
                    satisfied_goal = {k:receipt[k] for k in ("session","goal","evidence_id","assessment")}
                    if receipt.get("goal_id") is not None:
                        satisfied_goal["goal_id"] = receipt["goal_id"]
                    satisfied_goal["assessed_sequence"] = sequence
                    satisfied_goal["reason"] = "model_goal_satisfied"
                    reviewed = correction = None
                    status("monitoring", **satisfied_goal)
                    if once:
                        return 0
                    continue
                history.select(used_basis[0])
                history.record(receipt)
                incomplete = (truncated or situation_changed) and receipt is None
                status("incomplete" if incomplete else "reviewed", run_id=round_id, session_id=session.id, receipt=receipt,
                       parent_id=parent_id, assessed_sequence=sequence, last_judgment=last_judgment,
                       reason=("situation_unassessed" if situation_changed and incomplete else "output_limit" if incomplete
                               else "no_change_requested" if receipt is None else None))
                reviewed = key
                next_review = explicit_recheck or time.monotonic() + idle_interval
                rejected = receipt and receipt.get("status") == "rejected"
                if incomplete or rejected:
                    corrections = correction[2] if correction and correction[0] == key else 0
                    limit = 2 if rejected and (observation.get("ongoing") or {}).get("requires_action") is True else 1
                    feedback = {k: receipt[k] for k in ("action", "reason", "unmet_conditions") if receipt and k in receipt}
                    correction = (key, feedback or {"reason": "situation_unassessed" if situation_changed else "output_limit"},
                                  min(corrections + 1, limit))
                    if corrections < limit:
                        reviewed = None
                else:
                    correction = None
                if session_file and Path(session_file).stat().st_size >= 8 * 1024**2:
                    session.close()
                    session = None
                backoff = 1
                if once:
                    return 1 if incomplete else 0
                # Force an observation made after tool dispatch, even if a read was already in flight.
                sequence = feed.latest.sequence
                read_after = time.time()
                feed.wake.set()
            except WorldChanged as change:
                if change.replace_session:
                    session.process.kill()
                    session.close()
                    session = None
                status("superseded", run_id=round_id, reason="world_changed_during_decision")
                reviewed = None
                if once:
                    return 0
            except InterruptedError:
                break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                status("recovering", error=str(error))
                if session:
                    session.close()
                    session = None
                if once:
                    return 1
                retry_at = time.monotonic() + backoff
                backoff = min(backoff * 2, 120)
    finally:
        feed.close()
        if session:
            session.close()
        if stop.is_set():
            status("stopped")
    return 0
