/* Marketplace page: filters + observe + insured + lightbox + tabs. */
(function () {
  // Update snapshot fields from marketplace cards once, in case older
  // observe entries (from before insured value existed) are in the store.
  (function migrate() {
    document.querySelectorAll('#grid-marketplace .card').forEach(c => {
      const id = c.dataset.nft || c.dataset.ccUrl;
      const snap = CCObserve.get(id);
      if (snap && !snap.insured && c.dataset.insured) {
        CCObserve.update(id, {
          insured: c.dataset.insured,
          price:   c.dataset.price || snap.price,
          currency:c.dataset.currency || snap.currency,
        });
      }
    });
  })();

  // Filters
  CCFilters.init({ gridSelector: '#grid-marketplace' });

  // Insured + lightbox, initial wiring
  CCInsured.wire(document.querySelectorAll('#grid-marketplace .card'));
  CCLightbox.wire(document.querySelectorAll('.card .detail'));

  // Observe – update both sidebar badge (#obs-count) and tab counter (#obs-count-tab)
  const observeGrid = document.getElementById('grid-observe');
  const _countEls = [
    document.getElementById('obs-count'),
    document.getElementById('obs-count-tab'),
  ].filter(Boolean);
  const countProxy = { set textContent(v) { _countEls.forEach(e => e.textContent = v); } };
  const ctrl = CCObserveCards.wireGrid(observeGrid, {
    emptyEl: document.getElementById('observe-empty'),
    countEl: countProxy,
    sourceCards: () => document.querySelectorAll('#grid-marketplace .card'),
    refreshFromApi: true,
  });

  // Tabs
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
      const which = t.dataset.panel;
      document.getElementById('panel-marketplace').classList.toggle('active', which === 'marketplace');
      document.getElementById('panel-observe').classList.toggle('active', which === 'observe');
      if (which === 'observe') ctrl.rebuild();
    });
  });

  // Open the observe tab automatically when the URL hash is #observe
  // (used by the sidebar "Observed" link from other pages).
  function activatePanel(which) {
    document.querySelectorAll('.tab').forEach(x =>
      x.classList.toggle('active', x.dataset.panel === which));
    document.getElementById('panel-marketplace').classList.toggle('active', which === 'marketplace');
    document.getElementById('panel-observe').classList.toggle('active', which === 'observe');
    if (which === 'observe') ctrl.rebuild();
  }
  if (location.hash === '#observe') activatePanel('observe');
  window.addEventListener('hashchange', () => {
    activatePanel(location.hash === '#observe' ? 'observe' : 'marketplace');
  });
})();
