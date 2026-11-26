import assert from 'node:assert/strict';
import { test } from 'node:test';
import { lookupCatalog, publicView } from '../runtime/game-catalogs.mjs';

const view = {
  goal: '选择最近的电影院',
  facts: { player: { position: [5, 0, 0] } },
  actions: [{ id: 'pin:far' }, { id: 'pin:near' }, { id: 'pin:museum' }, { id: 'walk' }],
  catalogs: {
    places: { revision: 'v1', description: 'GTA locations', entries: [
      { id: 'pin:far', name: '电影院', position: [500, 0, 0] },
      { id: 'pin:near', name: '电影院', position: [50, 0, 0] },
      { id: 'pin:museum', name: '博物馆', position: [20, 0, 0] },
    ] },
    cheats: { revision: 'v1', description: 'known effects', entries: [
      { id: 'cheat:BARNSTORM', name: '生成特技飞机', aliases: ['BARNSTORM'] },
    ] },
  },
};

test('explicitly requested catalog actions lead the visible choices, ranked by distance', () => {
  const model = publicView(view);
  assert.deepEqual(model.actions.map(a => a.id), ['pin:near', 'pin:far', 'walk']);
  assert.deepEqual(model.catalog_matches_in_goal.places.map(row => [row.action, row.distance_m]),
    [['pin:near', 45], ['pin:far', 495]]);
  const pending = publicView({ ...view, actions: [{ id: 'review:keep' }], pending: { id: 'drive' } });
  assert.deepEqual(pending.actions, [{ id: 'review:keep' }]);
  assert.equal(pending.catalog_matches_in_goal.places[0].action, undefined);
});

test('a cheat mentioned in the goal stays selectable without jumping ahead of movement', () => {
  const current = structuredClone(view);
  current.goal = '步行途中补满护甲';
  current.actions.push({ id: 'cheat:TURTLE' });
  current.catalogs.cheats.entries.push({ id: 'cheat:TURTLE', name: '补满护甲', method: 'scriptedEffect' });
  const model = publicView(current);
  assert.deepEqual(model.actions.map(a => a.id), ['walk', 'cheat:TURTLE']);
  assert.equal(model.catalog_matches_in_goal.cheats[0].action, 'cheat:TURTLE');
});

test('negative results stay valid only for that catalog revision', () => {
  const first = lookupCatalog(view, 'cheats', '坦克', []);
  assert.equal(first.status, 'no_match');
  assert.equal(lookupCatalog(view, 'cheats', ' 坦克！', [first]).previously_checked, true);
  const next = structuredClone(view);
  next.catalogs.cheats.revision = 'v2';
  next.catalogs.cheats.entries.push({ id: 'test-action', name: '坦克' });
  const found = lookupCatalog(next, 'cheats', '坦克', [first]);
  assert.equal(found.status, 'found');
  assert.equal(found.previously_checked, false);
  assert.equal(found.entries[0].action, undefined);
  const pending = { ...view, actions: [], pending: { id: 'walk' } };
  const readOnly = lookupCatalog(pending, 'places', '电影院', []);
  assert.equal(readOnly.entries[0].availability, '当前动作仍持有控制');
  assert.equal(readOnly.entries[0].action, undefined);
});
test('cheat searches distinguish available background effects, unsupported codes, and temporary action ownership', () => {
  const view = {
    goal: '召唤坦克', facts: {}, actions: [{ id: 'spawn:RHINO' }],
    catalogs: { cheats: { entries: [
      { id: 'spawn:RHINO', name: '犀牛坦克', aliases: ['坦克'], category: '载具', search_terms: ['后台'],
        method: 'scriptedEffect', source: '插件生成载具', background_available: true,
        requirements: '空旷道路', verification: '实体与车型' },
      { id: 'cheat:EXTINCT', name: '渡渡鸟', code: 'EXTINCT', category: '载具', method: null, background_available: false },
    ] } },
  };
  const tank = lookupCatalog(view, 'cheats', '坦克', []).entries[0];
  assert.equal(tank.action, 'spawn:RHINO');
  assert.equal(tank.code, undefined);
  assert.equal(tank.method, 'scriptedEffect');
  assert.equal(tank.availability, '可直接提交后台效果，无需窗口焦点');
  assert.equal(tank.requirements, '空旷道路');
  assert.equal(tank.verification, '实体与车型');
  assert.equal(lookupCatalog(view, 'cheats', '后台', []).count, 1);
  assert.equal(lookupCatalog(view, 'cheats', '载具', []).count, 2);
  assert.equal(lookupCatalog({ ...view, actions: [], pending: { id: 'walk' } }, 'cheats', '坦克', []).entries[0].availability,
    '当前动作暂未提供这一后台效果');
  const unsupported = lookupCatalog(view, 'cheats', '渡渡鸟', []).entries[0];
  assert.equal(unsupported.action, undefined);
  assert.equal(unsupported.availability, '后台不可用');
  assert.equal(publicView({ ...view, goal: '使用 EXTINCT' }).catalog_matches_in_goal.cheats[0].availability, '后台不可用');
});

test('an urgent supply is visible and searchable whether or not the goal mentions ammo', () => {
  const current = structuredClone(view);
  const action = { id: 'review:supply:ammo:1B06D571', relation: 'parallel' };
  current.actions.push(action);
  current.catalogs.cheats.entries.push({
    id: action.id, name: '补充当前枪械弹药', aliases: ['补弹'], method: 'scriptedEffect', keep_visible: true,
  });
  assert.ok(publicView(current).actions.some(row => row.id === action.id));
  assert.equal(lookupCatalog(current, 'cheats', '补弹', []).entries[0].action, action.id);
  current.goal = '补弹';
  assert.equal(publicView(current).actions.filter(row => row.id === action.id).length, 1);
});

test('autoplay offers only selectable actions and reports a requested limitation as a result', () => {
  const current = structuredClone(view);
  current.autoplay = true;
  current.conversation = [{ id: 'viewer', instruction: '试试爆炸子弹' }];
  current.catalogs.cheats.entries.push({ id: 'cheat:HIGHEX', name: '爆炸弹药',
    aliases: ['爆炸子弹'], background_available: false });
  const unavailable = lookupCatalog(current, 'cheats', '爆炸子弹', []);
  assert.equal(unavailable.status, 'unavailable_here');
  assert.deepEqual(unavailable.entries, []);
  assert.equal(publicView(current).catalog_matches_in_goal.cheats[0].action, undefined);
  current.catalogs.cheats.entries.pop();
  const action = 'review:effect:HIGHEX';
  current.actions.push({ id: action });
  current.catalogs.cheats.entries.push({ id: action, name: '爆炸弹药',
    aliases: ['爆炸子弹'], background_available: true, method: 'scriptedEffect' });
  assert.deepEqual(lookupCatalog(current, 'cheats', '爆炸子弹', [unavailable]).entries.map(row => row.action), [action]);
  assert.equal(publicView(current).catalog_matches_in_goal.cheats[0].action, action);
});
