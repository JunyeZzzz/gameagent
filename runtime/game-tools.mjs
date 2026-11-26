import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Type } from 'typebox';
import { createFlowTrace } from './agent-flow-trace.mjs';
import { lookupCatalog, publicView } from './game-catalogs.mjs';

const bridge = fileURLToPath(new URL('./environment.py', import.meta.url));
const readOnlyLimit = 2;
const decimalChoice = /^[1-9]\d*$/;
const taskUpdate = Type.Optional(Type.Object({
  intent: Type.String({ minLength: 1, maxLength: 160, description: '当前阶段主动选择的子目标。' }),
  expected: Type.String({ minLength: 1, maxLength: 160, description: '用哪项游戏效果判断这一步有进展。' }),
  revisit: Type.String({ minLength: 1, maxLength: 160, description: '哪项现场变化值得考虑维持、调整或换目标。' }),
  finish_when: Type.String({ minLength: 1, maxLength: 160, description: '整件委托的具体收尾条件。' }),
  recheck_after_s: Type.Optional(Type.Integer({ minimum: 1, maximum: 600,
    description: '需要时间复核时填写秒数。现场变化也可提前触发判断。' })),
}, { additionalProperties: false, description: '首次形成或实质改变的当前任务判断。' }));

// Commitment: one warm environment worker answers tool calls in order, one JSON line per request.
// Assumption: adapters keep game state on disk, so a long-lived module sees the same world a fresh one would.
// Trade-off: any failure, timeout or adapter edit discards the worker; the next call starts a clean one.
// Reconsider: adapters that cache state in memory, or calls that must run concurrently.
export function createEnvironmentBridge({ timeoutMs = 7000, replyLimit = 512_000,
  adapterFile = () => new URL(`../adapters/${process.env.GAMEAGENT_ADAPTER}.py`, import.meta.url) } = {}) {
  let worker = null, generation = null, queue = Promise.resolve();
  const adapterVersion = () => {
    try {
      const info = statSync(adapterFile());
      return `${info.ino}:${info.mtimeMs}:${info.size}`;
    } catch { return null; }
  };
  const discard = () => {
    const current = worker;
    worker = null;
    if (current && current.exitCode === null && current.signalCode === null) current.kill('SIGKILL');
  };
  function start() {
    const child = spawn(process.env.GAMEAGENT_PYTHON, [bridge, '--serve'], {
      stdio: ['pipe', 'pipe', 'ignore'],
      env: { GAMEAGENT_ROOT: process.env.GAMEAGENT_ROOT, GAMEAGENT_ADAPTER: process.env.GAMEAGENT_ADAPTER,
             PATH: process.env.PATH, PYTHONUTF8: '1' },
    });
    child.stdin.on('error', () => {}); // Early exit is reported through the pending call.
    child.on('error', () => {});
    child.on('exit', () => { if (worker === child) worker = null; });
    return child;
  }
  function send(request) {
    return new Promise((resolve, reject) => {
      const version = adapterVersion();
      if (worker && version !== generation) discard();
      if (!worker) { worker = start(); generation = version; }
      const child = worker;
      const chunks = [];
      let size = 0;
      const settle = (error, value) => {
        clearTimeout(timer);
        child.stdout.off('data', onData);
        child.off('exit', onExit);
        child.off('error', onExit);
        if (error) {
          if (worker === child) discard();
          reject(error);
        } else resolve(value);
      };
      const onExit = () => settle(new Error('环境接口未返回有效结果'));
      const onData = chunk => {
        const end = chunk.indexOf(10);
        chunks.push(end === -1 ? chunk : chunk.subarray(0, end));
        size += end === -1 ? chunk.length : end;
        if (size > replyLimit) return settle(new Error('环境接口未返回有效结果'));
        if (end === -1) return;
        if (end !== chunk.length - 1) return settle(new Error('环境接口返回了多余数据'));
        try { settle(null, JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch (parseError) { settle(parseError); }
      };
      const timer = setTimeout(onExit, timeoutMs);
      child.stdout.on('data', onData);
      child.once('exit', onExit);
      child.once('error', onExit);
      child.stdin.write(JSON.stringify(request) + '\n');
    });
  }
  process.once('exit', discard);
  return {
    call(request) {
      const reply = queue.then(() => send(request));
      queue = reply.catch(() => {});
      return reply;
    },
    close: discard,
  };
}

function result(details) {
  return { content: [{ type: 'text', text: JSON.stringify(details) }], details };
}

export default function gameTools(pi) {
  const trace = createFlowTrace();
  const environment = createEnvironmentBridge();
  const exchange = request => environment.call(request);
  let observation, actionSubmitted, requestId, initialSession, initialParent, initialGoal, initialGoalId, priorGoalResult, findings, currentTask;
  let batchDecides, readOnlyBatches, initialSituation, situationChange, initialViewer, viewerAnswered;
  const viewerReply = Type.Optional(Type.String({ minLength: 1, maxLength: 240,
    description: '给最新未答复观众的简短回复。游戏回执确认后才能说操作成功。' }));
  const replyForRound = text => observation.autoplay && observation.viewer_id && !viewerAnswered ? text?.trim() : undefined;
  const choiceIds = [];
  const numericActionIds = new Set();
  function rememberObservation(snapshot) {
    observation = snapshot;
    if (snapshot.ready) {
      for (const { id } of snapshot.actions) {
        if (decimalChoice.test(id)) numericActionIds.add(id);
      }
    }
  }
  function modelView(snapshot) {
    const { direct_user_action, direct_user_completion, ...gameSnapshot } = snapshot;
    const visible = publicView(gameSnapshot);
    if (!visible.actions?.length) return visible;
    const actions = visible.actions.map(({ numbered, id, ...action }) => {
      let index = choiceIds.indexOf(id);
      if (index === -1) index = choiceIds.push(id) - 1;
      return { ...(numbered ? {} : { id }), ...action, choice: index + 1 };
    });
    return { ...visible, actions };
  }
  const endRead = () => batchDecides || readOnlyBatches > readOnlyLimit;
  const finalRead = "本轮查证结束。使用已知结果提交一个动作，或说明维持、等待的条件。";
  function holdIssue(args) {
    if (!args.hold || args.completed_after || args.wait_for || args.recheck_after_s || args.task?.recheck_after_s ||
        observation.last_result?.interruption?.confirmed === true) return;
    const parent = observation.pending;
    if (parent && observation.ongoing?.requires_action === true && parent.goal === observation.goal &&
        (parent.goal_id ?? null) === (observation.goal_id ?? null)) return 'action_required';
    if (!initialParent && !observation.autoplay && observation.continuous_activity !== true && observation.actions?.length &&
        (currentTask || args.task)) return 'idle_hold_requires_condition';
  }
  pi.on('context', ({ messages }) => {
    const start = messages.findLastIndex(m => m.role === 'user');
    return { messages: start >= 0 ? messages.slice(start) : [] };
  });
  pi.on('before_agent_start', () => {
    const round = JSON.parse(readFileSync(process.env.GAMEAGENT_PROMPT_CLOCK, 'utf8'));
    numericActionIds.clear();
    rememberObservation(round.observation);
    const active = pi.getActiveTools();
    const hasLookup = active.includes('game_lookup');
    if (Boolean(observation.catalogs) !== hasLookup) {
      pi.setActiveTools(observation.catalogs ? [...active, 'game_lookup'] : active.filter(name => name !== 'game_lookup'));
    }
    initialSession = observation.session;
    initialGoal = observation.goal;
    initialGoalId = observation.goal_id;
    initialParent = observation.pending?.id;
    initialSituation = observation.situation?.key;
    initialViewer = observation.viewer_id;
    viewerAnswered = Boolean(round.viewer_reply);
    if (viewerAnswered && observation.conversation?.length) {
      const conversation = observation.conversation.map(message =>
        message.id === round.viewer_reply.id ? { ...message, reply: round.viewer_reply.text } : message);
      observation = { ...observation, conversation };
    }
    situationChange = round.situation_change;
    currentTask = round.task;
    priorGoalResult = round.prior_goal_result;
    findings = round.catalog_findings ?? [];
    actionSubmitted = false;
    choiceIds.length = 0;
    batchDecides = false;
    readOnlyBatches = 0;
    trace.write('prefetch_start', {}, null, round.run_id, round.read_started);
    trace.write('prefetch_end', { duration_ms: round.read_ms }, { observation, situation_change: situationChange },
      round.run_id, round.read_finished);
    return { message: { customType: 'game-prefetched-observation', display: true,
                        content: JSON.stringify({ 游戏快照: modelView(observation), 最近已执行动作: round.recent_actions ?? [],
                          现场变化: situationChange ? { 之前: situationChange.previous, 当前: observation.situation ?? null } : undefined,
                          最新观众留言已答复: observation.autoplay ? Boolean(viewerAnswered) : undefined,
                          当前任务: round.task ?? undefined,
                          该目标已查证: findings.length ? findings : undefined,
                          上次观察判断: round.assessment, 上次提交被拒: round.rejection,
                          本目标开始前的回执ID: priorGoalResult ?? undefined }) } };
  });
  pi.on('before_provider_request', event => {
    requestId = randomUUID();
    trace.write('model_start', { request_id: requestId }, { request_payload: event.payload });
  });
  pi.on('message_end', ({ message }) => {
    if (message.role !== 'assistant') return;
    const calls = message.content?.filter(part => part.type === 'toolCall') ?? [];
    batchDecides = calls.some(({ name, arguments: args }) => name === 'game_schedule' ||
      (name === 'game_observe' && args?.assessment &&
        (observation.autoplay && args.completed_after ? false :
          (args.hold && !holdIssue(args)) || args.completed_after || args.wait_for || args.recheck_after_s)));
    if (!batchDecides && calls.some(call => call.name === 'game_lookup' || call.name === 'game_observe')) {
      readOnlyBatches++;
    }
    const publicText = message.content?.filter(p => p.type === 'text').map(p => p.text);
    trace.write('model_end', { request_id: requestId, stop_reason: message.stopReason, tool_count: calls.length },
      { public_text: publicText });
  });
  pi.on('tool_execution_start', e => trace.write('tool_start', { tool: e.toolName, call_id: e.toolCallId }, { arguments: e.args }));
  pi.on('tool_execution_end', e => {
    const details = e.result?.details;
    const status = e.isError || details?.ready === false ? 'failure' : details?.status ?? 'success';
    trace.write('tool_end', { tool: e.toolName, call_id: e.toolCallId, status }, details);
  });
  pi.on('agent_end', () => trace.write('round_end', {}));
  pi.on('session_shutdown', () => { environment.close(); trace.close(); });

  pi.registerTool({
    name: 'game_lookup', label: '查询游戏目录',
    description: '按简短名称查询当前目录。秘籍也支持效果、载具俗称或类别，例如“补甲”“坦克”“载具”。结果给出当前可用动作、前提、执行和验证方式。只读，可在动作持续时查询。',
    parameters: Type.Object({
      catalog: Type.String({ minLength: 1, maxLength: 32 }),
      query: Type.String({ minLength: 1, maxLength: 100 }),
    }, { additionalProperties: false }), executionMode: 'sequential',
    async execute(_callId, { catalog, query }) {
      if (actionSubmitted || readOnlyBatches > readOnlyLimit) {
        return { ...result({ status: 'lookup_round_finished', reason: finalRead }), terminate: true };
      }
      const found = lookupCatalog(observation, catalog, query, findings);
      if (found.key && !found.previously_checked) findings.push(found);
      return { ...result(readOnlyBatches === readOnlyLimit ? { ...found, next: finalRead } : found),
        terminate: endRead() };
    },
  });

  pi.registerTool({
    name: 'game_observe', label: '观察现场', description: '重新读取现场。一次性目标已实际完成时可登记结果，转为持续观察。',
    parameters: Type.Object({ assessment: Type.Optional(Type.String({ minLength: 1, maxLength: 160,
      description: '简述现场和依据。可以说明选择动作前的判断。' })),
      hold: Type.Optional(Type.Boolean({
        description: '维持当前动作或原地观察。任务还在推进且没有动作时，提交下一动作，或填写复核时间和等待条件。' })),
      wait_for: Type.Optional(Type.String({ minLength: 1, maxLength: 80,
      description: '所需能力受阻且只能等现场变化时，填写 blocked_capabilities 中对应的 id。底层持续读取，条件变化会唤醒判断。' })),
      recheck_after_s: Type.Optional(Type.Integer({ minimum: 1, maximum: 600,
        description: '记录目标明确要求的时间节点。行动在途时仍按新鲜现场继续复核。' })),
      completed_after: Type.Optional(Type.String({ minLength: 1, maxLength: 100,
          description: '仅委托模式使用。整件委托已完成时填写 latest 或最后一步回执的完整 ID。自主游玩报告活动结果时使用 assessment 和 viewer_reply。' })),
      task: taskUpdate, viewer_reply: viewerReply },
      { additionalProperties: false }), executionMode: 'sequential',
    async execute(_callId, { assessment, hold, completed_after, wait_for, recheck_after_s, task, viewer_reply }) {
      const reply = replyForRound(viewer_reply);
      const activityReport = observation.autoplay && Boolean(completed_after);
      if (activityReport) {
        completed_after = undefined;
        hold = false;
      }
      if (!assessment?.trim() && readOnlyBatches > readOnlyLimit && !batchDecides) {
        return { ...result({ status: 'observation_round_finished', reason: finalRead }), terminate: true };
      }
      const previous = observation;
      try { rememberObservation(await exchange(null)); }
      catch (error) { observation = { ready: false, reason: error.message }; }
      if (assessment?.trim() && observation.ready &&
          observation.pending?.id === initialParent &&
          observation.session === initialSession && observation.goal === initialGoal && observation.goal === previous.goal &&
          observation.goal_id === initialGoalId &&
          observation.viewer_id === initialViewer &&
          observation.situation?.key === initialSituation) {
        if (wait_for && (completed_after || initialParent ||
            !observation.blocked_capabilities?.some(b => b.id === wait_for)))
          return { ...result({ ...modelView(observation), reason: 'wait_condition_changed' }), terminate: endRead() };
        const last = observation.last_result;
        const evidence = completed_after === 'latest' ? previous.last_result?.id : completed_after;
        const priorEvidence = Boolean(evidence && evidence === priorGoalResult);
        const deliberateStop = last?.status === 'cancelled' && last.interruption?.confirmed === true;
        const unmet = (observation.completion_checks ?? []).filter(check => check.met !== true);
        const confirmed = Boolean(!observation.autoplay && !unmet.length && evidence && !initialParent && (last?.status === 'completed' || deliberateStop) &&
          !priorEvidence && last.id === evidence && last.session === initialSession &&
          !last.awaiting_effect && (!last.effect_outcome || last.effect_outcome.state === 'completed'));
        const issue = holdIssue({ hold, completed_after, wait_for, recheck_after_s, task });
        const mustAct = issue === 'action_required';
        const status = confirmed ? 'goal_satisfied' : (completed_after || mustAct) ? 'rejected' : 'observing';
        return { ...result({ ready: true, status, assessment: assessment.trim(),
          viewer_reply: reply,
          evidence_id: confirmed ? last.id : undefined, wait_for: wait_for || undefined,
          recheck_after_s: confirmed ? undefined : recheck_after_s,
          task: completed_after || mustAct ? undefined : task,
          unmet_conditions: completed_after && unmet.length ? unmet : undefined,
          reason: completed_after && unmet.length ? 'final_state_unmet' : priorEvidence ? 'receipt_predates_goal' : completed_after && !confirmed ? 'completion_evidence_missing' :
            issue,
          next: mustAct ? '当前动作停在等待下一步的阶段。请提交一个可用控制，确需等待时填写复核时间。' :
            issue ? '现在没有进行中的动作。有能推进任务的动作就提交；确实需要原地等待时填写复核时间或等待条件。' :
            activityReport ? '已记录本轮观察。自主游玩继续，请根据当前候选选择下一步。' : undefined,
          session: observation.session, goal: observation.goal, goal_id: observation.goal_id, revision: observation.revision }),
          terminate: endRead() || Boolean((hold && !issue) || completed_after || wait_for || recheck_after_s) };
      }
      const visible = modelView(observation);
      return { ...result(readOnlyBatches === readOnlyLimit ? { ...visible, next: finalRead } : visible),
        terminate: endRead() };
    },
  });
  pi.registerTool({
    name: 'game_schedule', label: '选择动作', description: '提交一个候选动作。使用候选显示的 choice，或将显示的 id 填入 action。查询结果提供的 action 可直接提交。用回执核实效果。',
    parameters: Type.Object({
      action: Type.Optional(Type.String({ minLength: 1, description: '候选的字符串 id，也兼容无歧义的十进制编号。' })),
      choice: Type.Optional(Type.Integer({ minimum: 1, description: '候选的数字 choice。' })),
      radius_m: Type.Optional(Type.Integer({ minimum: 1,
        description: '候选提供 radius_m 时可设置覆盖半径（米）。省略则使用候选默认值。' })),
      subgoal: Type.Optional(Type.String({ maxLength: 120, description: '本次动作意图与现场依据。' })),
      task: taskUpdate, viewer_reply: viewerReply,
    }, { additionalProperties: false }),
    executionMode: 'sequential',
    async execute(_callId, { action, choice, radius_m, subgoal, task, viewer_reply }) {
      const reject = reason => ({ ...result({ action: action ?? choice, status: 'rejected', reason }), terminate: true });
      if (actionSubmitted) return reject('action_already_submitted');
      if (!observation?.ready || observation.session !== initialSession || observation.goal !== initialGoal ||
          observation.goal_id !== initialGoalId ||
          observation.viewer_id !== initialViewer ||
          observation.situation?.key !== initialSituation ||
          observation.pending?.id !== initialParent) return reject('observation_changed');
      if (situationChange && !subgoal?.trim()) return reject('situation_requires_reason');
      const echoedParent = choice !== undefined && initialParent &&
        (action === initialParent || (action?.length === 32 && action === initialParent.replaceAll('-', ''))) &&
        !observation.actions.some(candidate => candidate.id === action);
      if (!echoedParent && (action === undefined) === (choice === undefined)) return reject('select_one_action');
      const number = choice ?? (decimalChoice.test(action) && !numericActionIds.has(action) ? Number(action) : undefined);
      const actionId = number === undefined ? action : choiceIds[number - 1];
      const chosen = observation.actions.find(a => a.id === actionId);
      if (!chosen) return reject('action_unavailable');
      if (radius_m !== undefined && (!chosen.radius_m || !Number.isInteger(radius_m) ||
          radius_m < chosen.radius_m.min || radius_m > chosen.radius_m.max)) return reject('invalid_action_radius');
      actionSubmitted = true;
      const intent = { id: randomUUID(), action: actionId, session: observation.session, goal: observation.goal,
                       goal_id: observation.goal_id, revision: observation.revision,
                       situation_key: observation.situation?.key,
                       expires_at: observation.observed_at + observation.valid_for };
      if (observation.pending) intent.parent_id = observation.pending.id;
      if (radius_m !== undefined) intent.radius_m = radius_m;
      if (subgoal) intent.subgoal = subgoal;
      try {
        let receipt = await exchange(intent);
        if (receipt.status === 'rejected' && receipt.reason === 'observation_changed' &&
            Date.now() / 1000 < intent.expires_at) {
          const fresh = await exchange(null);
          const sameChoice = fresh.actions?.find(a => a.id === actionId);
          if (fresh.ready && fresh.revision !== intent.revision &&
              fresh.session === initialSession && fresh.goal === initialGoal && fresh.goal_id === initialGoalId &&
              fresh.viewer_id === initialViewer &&
              fresh.situation?.key === initialSituation && fresh.pending?.id === initialParent &&
              JSON.stringify(sameChoice) === JSON.stringify(chosen)) {
            receipt = await exchange({ ...intent, revision: fresh.revision });
          }
        }
        const accepted = ['accepted', 'running', 'completed'].includes(receipt.status);
        return { ...result({ action: actionId, ...receipt, task: accepted ? task : undefined,
          viewer_reply: accepted ? replyForRound(viewer_reply) : undefined }), terminate: true };
      } catch (error) {
        return { ...result({ status: 'unknown', id: intent.id, reason: error.message }), terminate: true };
      }
    },
  });
}
