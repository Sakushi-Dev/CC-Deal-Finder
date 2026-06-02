/* Marketplace-Seite: Filter + Observe + Insured + Lightbox + Tabs. */
(function () {
  // Snapshot-Felder aus Marketplace-Cards einmal aktualisieren, falls
  // ältere Observe-Einträge (vor Insured-Wert) im Store stehen.
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

  // Filter
  CCFilters.init({ gridSelector: '#grid-marketplace' });

  // Insured + Lightbox initial
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
