/* Persistenter Karten/Listen-Umschalter für alle .grid-Container der Seite. */
(function () {
  const KEY = 'cc_view_v1';

  function gridsAll() { return document.querySelectorAll('.grid'); }

  function applyView(mode) {
    const list = mode === 'list';
    gridsAll().forEach(g => g.classList.toggle('view-list', list));
    document.querySelectorAll('[data-view-cards]').forEach(b =>
      b.classList.toggle('active', !list));
    document.querySelectorAll('[data-view-list]').forEach(b =>
      b.classList.toggle('active', list));
  }

  function init() {
    const initial = localStorage.getItem(KEY) || 'cards';
    applyView(initial);
    document.querySelectorAll('[data-view-cards]').forEach(b =>
      b.addEventListener('click', () => {
        localStorage.setItem(KEY, 'cards'); applyView('cards');
      }));
    document.querySelectorAll('[data-view-list]').forEach(b =>
      b.addEventListener('click', () => {
        localStorage.setItem(KEY, 'list'); applyView('list');
      }));
  }

  window.CCView = { apply: applyView, init };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
