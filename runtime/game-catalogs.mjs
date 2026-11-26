// Read-only search over the observation already captured by the adapter.
const normalize = text => text.normalize('NFKC').toLowerCase().replace(/[\p{P}\p{S}\s]/gu, '');
const namesFor = entry => [entry.name, entry.code, ...(entry.aliases ?? [])].filter(Boolean).map(normalize).filter(Boolean);

function distanceTo(view, entry) {
  const from = view.facts?.player?.position;
  return from && entry.position ? Math.hypot(entry.position[0] - from[0], entry.position[1] - from[1]) : Infinity;
}

function displayEntry(view, catalog, entry, actions) {
  const selectable = actions.has(entry.id);
  const background = catalog === 'cheats' && entry.method === 'scriptedEffect';
  let availability = '当前游戏状态不可执行';
  if (selectable) {
    availability = background ? '可直接提交后台效果，无需窗口焦点' : '可提交';
  } else if (catalog === 'cheats' && entry.background_available === false) {
    availability = '后台不可用';
  } else if (view.pending) {
    availability = background ? '当前动作暂未提供这一后台效果' : '当前动作仍持有控制';
  }
  const distance = distanceTo(view, entry);
  const metadata = catalog === 'cheats' ? {
    code: entry.code ?? (entry.source ? undefined : entry.aliases?.[0]),
    category: entry.category,
    background_available: entry.background_available,
    method: entry.method,
    requirements: entry.requirements ?? undefined,
    verification: entry.verification,
  } : { requirements: entry.requirements, verification: entry.verification };
  return {
    name: entry.name,
    source: entry.source,
    position: entry.position,
    distance_m: Number.isFinite(distance) ? Math.round(distance) : undefined,
    ...metadata,
    radius_m: entry.radius_m,
    action: selectable ? entry.id : undefined,
    availability,
  };
}

export function lookupCatalog(view, catalog, query, checked) {
  const directory = view.catalogs?.[catalog];
  if (!directory || !Array.isArray(directory.entries)) {
    return { status: 'catalog_unavailable', catalog, query };
  }
  const key = normalize(query);
  if (!key) return { status: 'invalid_query', catalog, query };
  const prior = checked.find(row => row.catalog === catalog && row.key === key && row.revision === directory.revision);
  if (prior?.status === 'no_match') {
    return { status: prior.status, catalog, query, key, revision: directory.revision,
      entries: [], count: 0, previously_checked: true };
  }
  const matches = directory.entries.flatMap(entry => {
    const names = namesFor(entry);
    const exact = names.includes(key);
    const category = [entry.category, ...(entry.search_terms ?? [])].some(term => term && normalize(term) === key);
    return exact || category || names.some(name => name.includes(key)) ? [{ entry, exact }] : [];
  });
  const actions = new Set((view.actions ?? []).map(action => action.id));
  const unavailable = view.autoplay && matches.length && !matches.some(({ entry }) => actions.has(entry.id));
  const status = unavailable ? 'unavailable_here' : matches.length ? 'found' : 'no_match';
  matches.sort((a, b) => Number(b.exact) - Number(a.exact) ||
    (catalog === 'cheats' ? Number(actions.has(b.entry.id)) - Number(actions.has(a.entry.id)) : 0) ||
    distanceTo(view, a.entry) - distanceTo(view, b.entry));
  const entries = matches.filter(({ entry }) => !view.autoplay || actions.has(entry.id))
    .slice(0, 8).map(({ entry }) => displayEntry(view, catalog, entry, actions));
  return {
    status, catalog, query, key, revision: directory.revision, entries,
    count: view.autoplay ? entries.length : matches.length,
    ...(unavailable && catalog === 'cheats' && matches.every(({ entry }) => !entry.background_available)
      ? { reason: '当前插件版本没有后台入口' } : {}),
    previously_checked: Boolean(prior),
  };
}

export function publicView(view) {
  if (!view.catalogs) return view;
  const { catalogs, ...visible } = view;
  const ids = new Set(Object.values(catalogs).flatMap(catalog => catalog.entries.filter(entry => !entry.keep_visible)
    .map(entry => entry.id)));
  const actions = new Map(visible.actions.map(action => [action.id, action]));
  const goal = normalize(visible.autoplay ? visible.conversation?.at(-1)?.instruction ?? '' : visible.goal);
  const goalMatches = Object.fromEntries(Object.entries(catalogs).flatMap(([name, catalog]) => {
    const entries = catalog.entries.filter(entry => namesFor(entry).some(text => text.length >= 2 && goal.includes(text)));
    entries.sort((a, b) => distanceTo(view, a) - distanceTo(view, b));
    return entries.length ? [[name, entries.slice(0, 4).flatMap(entry => {
      if (!view.autoplay || actions.has(entry.id)) return [displayEntry(view, name, entry, actions)];
      return name === 'cheats' && !entry.background_available
        ? [{ name: entry.name, status: 'unavailable_here', reason: '当前插件版本没有后台入口' }] : [];
    })]] : [];
  }));
  const requested = new Set(Object.values(goalMatches).flatMap(entries =>
    entries.flatMap(entry => entry.action === undefined ? [] : [entry.action])));
  const destinations = new Set((goalMatches.places ?? []).flatMap(entry =>
    entry.action === undefined ? [] : [entry.action]));
  const selectableEntries = catalog => view.autoplay
    ? catalog.entries.filter(entry => actions.has(entry.id))
    : catalog.entries;
  return {
    ...visible,
    actions: [...Array.from(destinations, id => actions.get(id)),
      ...visible.actions.filter(action => !destinations.has(action.id) &&
        (!ids.has(action.id) || requested.has(action.id)))],
    queryable_catalogs: Object.fromEntries(Object.entries(catalogs).map(([name, catalog]) => [
      name, { description: catalog.description, entries: selectableEntries(catalog).length,
        ...(name === 'cheats' ? { categories: [...new Set(selectableEntries(catalog).map(entry => entry.category).filter(Boolean))] } : {}) },
    ])),
    ...(Object.keys(goalMatches).length ? { catalog_matches_in_goal: goalMatches } : {}),
  };
}
