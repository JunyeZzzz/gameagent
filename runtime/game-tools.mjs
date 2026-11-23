import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Type } from 'typebox';
import { createFlowTrace } from './agent-flow-trace.mjs';

const bridge = fileURLToPath(new URL('./environment.py', import.meta.url));

function exchange(request) {
  return new Promise((resolve, reject) => {
    const child = execFile(process.env.GAMEAGENT_PYTHON, [bridge], {
      timeout: 7000, killSignal: 'SIGKILL', maxBuffer: 512_000,
      env: { GAMEAGENT_ROOT: process.env.GAMEAGENT_ROOT, GAMEAGENT_ADAPTER: process.env.GAMEAGENT_ADAPTER,
             PATH: process.env.PATH, PYTHONUTF8: '1' },
    }, (error, output) => {
      if (error) return reject(new Error('环境接口未返回有效结果'));
      try { resolve(JSON.parse(output)); } catch (parseError) { reject(parseError); }
    });
    child.stdin.on('error', () => {}); // Early exit is reported by execFile.
    child.stdin.end(JSON.stringify(request));
  });
}

function result(details) {
  return { content: [{ type: 'text', text: JSON.stringify(details) }], details };
}

export default function gameTools(pi) {
  const trace = createFlowTrace();
  let observation, submitted, requestId;
  pi.on('context', ({ messages }) => {
    const start = messages.findLastIndex(m => m.role === 'user');
    return { messages: start >= 0 ? messages.slice(start) : [] };
  });
  pi.on('before_agent_start', () => {
    const round = JSON.parse(readFileSync(process.env.GAMEAGENT_PROMPT_CLOCK, 'utf8'));
    observation = round.observation;
    submitted = false;
    trace.write('prefetch_start', {}, null, round.run_id, round.read_started);
    trace.write('prefetch_end', { duration_ms: round.read_ms }, { observation }, round.run_id, round.read_finished);
    return { message: { customType: 'game-prefetched-observation', display: true,
                        content: JSON.stringify({ 游戏快照: observation, 最近已执行动作: round.recent_actions ?? [] }) } };
  });
  pi.on('before_provider_request', event => {
    requestId = randomUUID();
    trace.write('model_start', { request_id: requestId }, { request_payload: event.payload });
  });
  pi.on('message_end', ({ message }) => {
    if (message.role !== 'assistant') return;
    const publicText = message.content?.filter(p => p.type === 'text').map(p => p.text);
    trace.write('model_end', { request_id: requestId, stop_reason: message.stopReason }, { public_text: publicText });
  });
  pi.on('tool_execution_start', e => trace.write('tool_start', { tool: e.toolName, call_id: e.toolCallId }, { arguments: e.args }));
  pi.on('tool_execution_end', e => {
    const details = e.result?.details;
    const status = e.isError || details?.ready === false ? 'failure' : details?.status ?? 'success';
    trace.write('tool_end', { tool: e.toolName, call_id: e.toolCallId, status }, details);
  });
  pi.on('agent_end', () => trace.write('round_end', {}));
  pi.on('session_shutdown', () => trace.close());

  pi.registerTool({
    name: 'game_observe', label: '观察现场', description: '重新获取当前目标、可选动作及执行回执。',
    parameters: Type.Object({}, { additionalProperties: false }), executionMode: 'sequential',
    async execute() {
      try { observation = await exchange(null); }
      catch (error) { observation = { ready: false, reason: error.message }; }
      return result(observation);
    },
  });
  pi.registerTool({
    name: 'game_schedule', label: '选择动作', description: '提交当前观察中列出的一项动作，使用原始动作 ID。用回执和后续观察核实效果。',
    parameters: Type.Object({ action: Type.String({ minLength: 1 }), subgoal: Type.Optional(Type.String({ maxLength: 120,
      description: '需要跨轮保留时可填写下一步意图，以现场证据校正。' })) }, { additionalProperties: false }),
    executionMode: 'sequential',
    async execute(_callId, { action, subgoal }) {
      if (submitted || !observation?.ready || !observation.actions.some(a => a.id === action))
        return result({ status: 'rejected', reason: 'action_unavailable' });
      submitted = true;
      const intent = { id: randomUUID(), action, session: observation.session, revision: observation.revision,
                       expires_at: observation.observed_at + observation.valid_for };
      if (subgoal) intent.subgoal = subgoal;
      try {
        const receipt = await exchange(intent);
        return { ...result({ action, ...receipt }), terminate: true };
      } catch (error) {
        return { ...result({ status: 'unknown', id: intent.id, reason: error.message }), terminate: true };
      }
    },
  });
}
