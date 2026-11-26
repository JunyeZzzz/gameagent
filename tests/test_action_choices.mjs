import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import gameTools from '../runtime/game-tools.mjs';

test('numbered actions keep their target through refresh and use the original ID at dispatch', async () => {
  const root = mkdtempSync(join(tmpdir(), 'gameagent-action-choices-'));
  const fixture = join(root, 'bridge');
  const clock = join(root, 'round.json');
  const snapshot = join(root, 'snapshot.json');
  const dispatched = join(root, 'intent.json');
  Object.assign(process.env, { GAMEAGENT_HOME: root, GAMEAGENT_ROOT: root,
    GAMEAGENT_PROMPT_CLOCK: clock, GAMEAGENT_PYTHON: fixture });
  writeFileSync(fixture, `#!/bin/sh
[ "$2" = --serve ] || exit 2
while IFS= read -r request; do
  if [ "$request" = null ]; then
    cat "$GAMEAGENT_ROOT/snapshot.json"
  elif [ -f "$GAMEAGENT_ROOT/change-once" ]; then
    rm "$GAMEAGENT_ROOT/change-once"
    printf '%s' "$request" > "$GAMEAGENT_ROOT/rejected-intent.json"
    printf '{"status":"rejected","reason":"observation_changed"}'
  else
    printf '%s' "$request" > "$GAMEAGENT_ROOT/intent.json"
    printf '{"status":"accepted"}'
  fi
  printf '\\n'
done
`, { mode: 0o700 });
  const hooks = new Map(), tools = new Map();
  let activeTools = [];
  gameTools({
    on: (event, callback) => hooks.set(event, callback),
    registerTool: tool => tools.set(tool.name, tool),
    getActiveTools: () => activeTools,
    setActiveTools: names => { activeTools = names; },
  });
  try {
    const schedule = tools.get('game_schedule');
    const view = { ready: true, session: 'game', goal: 'Stop the car', revision: 'stable',
      observed_at: Date.now() / 1000, valid_for: 15 };
    const controls = ['keep', 'cancel', 'traffic', 'park'].map(name => ({ id: `review:${name}` }));
    const pending = { id: '2cbd5482-ea3e-4992-8df7-df564cefaabf' };
    writeFileSync(clock, JSON.stringify({ run_id: 'c'.repeat(32),
      observation: { ...view, pending, actions: controls } }));
    const stopContext = JSON.parse(hooks.get('before_agent_start')().message.content);
    assert.deepEqual(stopContext.游戏快照.actions[3], { id: 'review:park', choice: 4 });
    const park = await schedule.execute('park', { choice: 4 });
    assert.equal(park.details.status, 'accepted');
    const parked = JSON.parse(readFileSync(dispatched, 'utf8'));
    assert.equal(parked.action, 'review:park');
    assert.equal(parked.parent_id, pending.id);

    writeFileSync(clock, JSON.stringify({ run_id: 'f'.repeat(32), observation: { ...view, pending, actions: controls } }));
    hooks.get('before_agent_start')();
    const conflicting = await schedule.execute('conflicting', { action: 'review:traffic', choice: 4 });
    assert.equal(conflicting.details.reason, 'select_one_action');
    const echoed = await schedule.execute('parent-echo', { action: pending.id.replaceAll('-', ''), choice: 4 });
    assert.equal(echoed.details.status, 'accepted');
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).action, 'review:park');

    const redirect = { id: 'review:drive_to:place:小首尔', description: '用当前车前往小首尔并停稳' };
    const redirection = { ...view, goal: '改去小首尔并停稳', pending, actions: [...controls, redirect],
      catalogs: { places: { revision: 'v1', entries: [{ id: redirect.id, name: '小首尔' }] } } };
    writeFileSync(clock, JSON.stringify({ run_id: 'e'.repeat(32), observation: redirection }));
    const redirectContext = JSON.parse(hooks.get('before_agent_start')().message.content);
    assert.deepEqual(redirectContext.游戏快照.actions[0], { ...redirect, choice: 1 });
    const redirected = await schedule.execute('redirect', { choice: 1 });
    assert.equal(redirected.details.status, 'accepted');
    assert.equal(redirected.details.action, redirect.id);
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).parent_id, pending.id);

    const south = { id: 'walknear:-1180.75,-1293.75,4.28125', numbered: true,
      description: 'South, 170 m', target_position: [-1180.75, -1293.75, 4.28125] };
    const north = { id: 'walknear:-1271,-980,9.21875', numbered: true, description: 'North, 170 m' };
    const ordinary = { id: '1', description: 'An unrelated string ID' };
    const walk = { ...view, goal: 'Walk 170 m', actions: [south, ordinary, north] };
    writeFileSync(clock, JSON.stringify({ run_id: 'a'.repeat(32), observation: walk }));
    const context = JSON.parse(hooks.get('before_agent_start')().message.content);
    assert.deepEqual(context.游戏快照.actions, [
      { choice: 1, description: south.description, target_position: south.target_position },
      { ...ordinary, choice: 2 }, { choice: 3, description: north.description },
    ]);

    const east = { id: 'walknear:-1144,-1263,5.71875', numbered: true, description: 'East, 170 m' };
    writeFileSync(snapshot, JSON.stringify({ ...walk, actions: [east, north, ordinary] }));
    const refreshed = await tools.get('game_observe').execute('observe', {});
    assert.deepEqual(refreshed.details.actions, [
      { choice: 4, description: east.description }, { choice: 3, description: north.description },
      { ...ordinary, choice: 2 },
    ]);
    const ambiguous = await schedule.execute('ambiguous', { action: '1', choice: 2 });
    assert.equal(ambiguous.details.reason, 'select_one_action');
    const removed = await schedule.execute('stale', { choice: 1 });
    assert.equal(removed.details.reason, 'action_unavailable');
    const accepted = await schedule.execute('select', { action: '3' });
    assert.equal(accepted.details.status, 'accepted');
    assert.equal(accepted.details.action, north.id);
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).action, north.id);

    const nextRound = { ...walk, actions: [east, ordinary] };
    writeFileSync(clock, JSON.stringify({ run_id: 'b'.repeat(32), observation: nextRound }));
    const nextContext = JSON.parse(hooks.get('before_agent_start')().message.content);
    assert.equal(nextContext.游戏快照.actions[0].choice, 1);
    const normal = await schedule.execute('string', { action: '1' });
    assert.equal(normal.details.status, 'accepted');
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).action, '1');

    const rebased = { ...nextRound, revision: 'new-observation', ongoing: { trigger: 'moving' },
      actions: [ordinary, east, north] };
    writeFileSync(clock, JSON.stringify({ run_id: '1'.repeat(32), observation: nextRound }));
    writeFileSync(snapshot, JSON.stringify(rebased));
    writeFileSync(join(root, 'change-once'), '');
    hooks.get('before_agent_start')();
    assert.equal((await schedule.execute('revalidate', { choice: 1 })).details.status, 'accepted');
    const retried = JSON.parse(readFileSync(dispatched, 'utf8'));
    assert.equal(retried.action, east.id);
    assert.equal(retried.revision, rebased.revision);
    assert.equal(retried.id, JSON.parse(readFileSync(join(root, 'rejected-intent.json'), 'utf8')).id);

    writeFileSync(clock, JSON.stringify({ run_id: '5'.repeat(32), observation: nextRound }));
    writeFileSync(snapshot, JSON.stringify({ ...rebased, actions: [ordinary, north] }));
    writeFileSync(join(root, 'change-once'), '');
    hooks.get('before_agent_start')();
    assert.equal((await schedule.execute('revalidate-missing', { choice: 1 })).details.reason, 'observation_changed');
    assert.deepEqual(JSON.parse(readFileSync(dispatched, 'utf8')), retried);

    writeFileSync(clock, JSON.stringify({ run_id: 'd'.repeat(32), observation: nextRound }));
    hooks.get('before_agent_start')();
    writeFileSync(snapshot, JSON.stringify({ ...walk, actions: [east] }));
    await tools.get('game_observe').execute('observe', {});
    const expiredId = await schedule.execute('old-numeric-id', { action: '1' });
    assert.equal(expiredId.details.reason, 'action_unavailable');
    const outside = await schedule.execute('unknown-number', { action: '9' });
    assert.equal(outside.details.reason, 'action_unavailable');
    const explicitChoice = await schedule.execute('registered-number', { choice: '1' });
    assert.equal(explicitChoice.details.status, 'accepted');
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).action, east.id);

    const observer = tools.get('game_observe');
    const waiting = { ...view, goal: 'Continue the drive', pending: { ...pending, goal: 'Continue the drive' },
      ongoing: { requires_action: true }, actions: [{ id: 'review:route' }] };
    const observeHold = { assessment: 'The car is waiting for the next route.', hold: true };
    writeFileSync(clock, JSON.stringify({ run_id: '2'.repeat(32), observation: waiting }));
    writeFileSync(snapshot, JSON.stringify(waiting));
    hooks.get('before_agent_start')();
    const idle = await observer.execute('route-required', observeHold);
    assert.equal(idle.details.status, 'rejected');
    assert.equal(idle.details.reason, 'action_required');
    assert.equal(idle.terminate, false);
    assert.equal((await schedule.execute('continue-route', { choice: 1 })).details.status, 'accepted');
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).action, 'review:route');

    writeFileSync(clock, JSON.stringify({ run_id: '3'.repeat(32), observation: waiting }));
    hooks.get('before_agent_start')();
    const timed = await observer.execute('wait-for-traffic', { ...observeHold, recheck_after_s: 1 });
    assert.equal(timed.details.status, 'observing');
    assert.equal(timed.terminate, true);

    const newGoal = { ...waiting, goal: 'Describe the current situation' };
    writeFileSync(clock, JSON.stringify({ run_id: '4'.repeat(32), observation: newGoal }));
    writeFileSync(snapshot, JSON.stringify({ ...newGoal, revision: 'newer', decision_event: 'new-candidates' }));
    hooks.get('before_agent_start')();
    assert.equal((await observer.execute('report-status', observeHold)).details.status, 'observing');

    const cast = { id: 'own-cast', session: 'game', status: 'completed', awaiting_effect: true };
    const casting = { ...view, last_result: cast, actions: [] };
    const report = { assessment: 'The game reported the result.', completed_after: 'latest', hold: true };
    for (const effect_outcome of [undefined, { state: 'partial' }, { state: 'completed' }]) {
      const current = { ...casting, last_result: { ...cast, awaiting_effect: !effect_outcome, effect_outcome } };
      writeFileSync(clock, JSON.stringify({ run_id: '6'.repeat(32), observation: current }));
      writeFileSync(snapshot, JSON.stringify(current));
      hooks.get('before_agent_start')();
      const result = await observer.execute('verify-world-effect-commission', report);
      assert.equal(result.details.status, effect_outcome?.state === 'completed' ? 'goal_satisfied' : 'rejected');
    }

    const playing = { ...view, autoplay: true, viewer_id: 'viewer-1', actions: [north],
      last_result: { ...cast, awaiting_effect: false, effect_outcome: { state: 'completed' } } };
    writeFileSync(clock, JSON.stringify({ run_id: '7'.repeat(32), observation: playing }));
    writeFileSync(snapshot, JSON.stringify(playing));
    hooks.get('before_agent_start')();
    hooks.get('message_end')({ message: { role: 'assistant',
      content: [{ type: 'toolCall', name: 'game_observe', arguments: report }] } });
    const legacy = await observer.execute('legacy-autoplay-completion', { ...report, viewer_reply: 'The activity ended.' });
    assert.equal(legacy.details.status, 'observing');
    assert.equal(legacy.details.reason, undefined);
    assert.equal(legacy.details.viewer_reply, 'The activity ended.');
    assert.equal(legacy.terminate, false);
    assert.equal((await schedule.execute('play-on', { choice: 1 })).details.status, 'accepted');

    const aura = { id: 'spectacle:lightning_aura', radius_m: { min: 1, max: 200, default: 20 } };
    const configure = { ...playing, actions: [aura, north] };
    writeFileSync(clock, JSON.stringify({ run_id: '8'.repeat(32), observation: configure }));
    hooks.get('before_agent_start')();
    assert.equal((await schedule.execute('wrong-ability', { action: north.id, radius_m: 40 })).details.reason,
      'invalid_action_radius');
    assert.equal((await schedule.execute('out-of-bounds', { action: aura.id, radius_m: 201 })).details.reason,
      'invalid_action_radius');
    assert.equal((await schedule.execute('configure-aura', { action: aura.id, radius_m: 40 })).details.status,
      'accepted');
    assert.equal(JSON.parse(readFileSync(dispatched, 'utf8')).radius_m, 40);
  } finally {
    hooks.get('session_shutdown')();
    rmSync(root, { recursive: true, force: true });
  }
});
