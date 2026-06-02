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

  // Observe
  const observeGrid = document.getElementById('grid-observe');
  const ctrl = CCObserveCards.wireGrid(observeGrid, {
    emptyEl: document.getElementById('observe-empty'),
    countEl: document.getElementById('obs-count'),
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
})();
