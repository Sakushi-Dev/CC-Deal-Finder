/* Persistently stored observe cards – data layer.
 *
 * Snapshot schema (compatible with marketplace + deals):
 *   { nft, title, category, price, currency, year, grading,
 *     grade, company, gradeStr, number, language, cardName, set,
 *     insured, marketplace, blockchain, image, imageFull, imageBack, ccUrl }
 */
(function () {
  const KEY = 'cc_observe_v1';
  let store = {};
  try { store = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
  catch (e) { store = {}; }

  const save = () => localStorage.setItem(KEY, JSON.stringify(store));

  const Observe = {
    keys()           { return Object.keys(store); },
    count()          { return Object.keys(store).length; },
    has(id)          { return !!store[id]; },
    get(id)          { return store[id]; },
    set(id, snap)    { store[id] = snap; save(); },
    remove(id)       { delete store[id]; save(); },
    all()            { return { ...store }; },
    update(id, partial) {
      if (!store[id]) return;
      store[id] = { ...store[id], ...partial };
      save();
    },
  };
  window.CCObserve = Observe;
})();
