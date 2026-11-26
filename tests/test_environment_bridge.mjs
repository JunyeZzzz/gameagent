import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { createEnvironmentBridge } from '../runtime/game-tools.mjs';

const project = dirname(dirname(fileURLToPath(import.meta.url)));

function fakeWorker() {
  const root = mkdtempSync(join(tmpdir(), 'gameagent-bridge-'));
  const script = join(root, 'worker');
  const adapter = join(root, 'adapter.py');
  writeFileSync(adapter, '# version 1\n');
  writeFileSync(script, `#!/bin/sh
[ "$2" = --serve ] || exit 2
echo start >> "$GAMEAGENT_ROOT/starts.log"
while IFS= read -r request; do
  case "$request" in
    '"crash"') exit 3 ;;
    '"hang"') sleep 5 ;;
    '"twice"') printf '{"n":1}\\n{"n":2}\\n' ;;
    *) printf '{"echo":%s}\\n' "$request" ;;
  esac
done
`, { mode: 0o700 });
  Object.assign(process.env, { GAMEAGENT_ROOT: root, GAMEAGENT_PYTHON: script });
  const starts = () => readFileSync(join(root, 'starts.log'), 'utf8').trim().split('\n').length;
  return { root, adapter, starts };
}

test('tool calls share one warm worker and recover from crashes, hangs and adapter edits', async () => {
  const { root, adapter, starts } = fakeWorker();
  const bridge = createEnvironmentBridge({ timeoutMs: 400, adapterFile: () => adapter });
  try {
    const replies = await Promise.all([bridge.call(null), bridge.call({ id: 'a' }), bridge.call({ id: 'b' })]);
    assert.deepEqual(replies, [{ echo: null }, { echo: { id: 'a' } }, { echo: { id: 'b' } }]);
    assert.equal(starts(), 1, 'concurrent calls are serialized onto one worker');

    await assert.rejects(bridge.call('crash'), /环境接口未返回有效结果/);
    assert.deepEqual(await bridge.call(null), { echo: null });
    assert.equal(starts(), 2, 'a crashed worker is replaced on the next call');

    const began = Date.now();
    await assert.rejects(bridge.call('hang'), /环境接口未返回有效结果/);
    assert.ok(Date.now() - began < 2000, 'a hung worker is abandoned at the timeout');
    assert.deepEqual(await bridge.call(null), { echo: null });
    assert.equal(starts(), 3);

    await assert.rejects(bridge.call('twice'), /多余数据/);
    assert.deepEqual(await bridge.call(null), { echo: null });
    assert.equal(starts(), 4, 'an unexpected extra reply discards the worker');

    writeFileSync(adapter, '# version 2, longer\n');
    const later = new Date(Date.now() + 5000);
    utimesSync(adapter, later, later);
    assert.deepEqual(await bridge.call(null), { echo: null });
    assert.equal(starts(), 5, 'an edited adapter is loaded by a new worker');
  } finally {
    bridge.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test('the worker serves real observations and receipts through environment.py', async () => {
  mkdirSync(join(project, 'local'), { recursive: true });
  const root = mkdtempSync(join(project, 'local', 'bridge-'));
  copyFileSync(join(project, 'state', 'fixture.json'), join(root, 'fixture.json'));
  const python = execFileSync('sh', ['-c', 'command -v python3'], { encoding: 'utf8' }).trim();
  Object.assign(process.env, { GAMEAGENT_ROOT: root, GAMEAGENT_ADAPTER: 'sandbox', GAMEAGENT_PYTHON: python });
  const bridge = createEnvironmentBridge();
  try {
    const first = await bridge.call(null);
    assert.equal(first.ready, true);
    assert.equal(first.facts.position, 0);
    const intent = { id: 'intent-1', action: 'advance', session: first.session, revision: first.revision,
      goal: first.goal, expires_at: first.observed_at + first.valid_for };
    const receipt = await bridge.call(intent);
    assert.equal(receipt.status, 'completed');
    assert.equal((await bridge.call(intent)).status, 'completed', 'a repeated intent returns its receipt');
    const after = await bridge.call(null);
    assert.equal(after.facts.position, 1, 'the repeated intent did not move the marker twice');
    assert.equal(after.last_result.id, 'intent-1');
  } finally {
    bridge.close();
    rmSync(root, { recursive: true, force: true });
  }
});
