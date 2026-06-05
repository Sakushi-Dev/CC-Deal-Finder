/* Mirrors the observe count from CCObserve into every #obs-count element
 * on the page (sidebar badge, optional tab counter, etc). Listens to
 * cross-tab localStorage changes too.
 */
(function () {
  function refresh() {
    if (!window.CCObserve) return;
    const n = window.CCObserve.count();
    document.querySelectorAll('#obs-count, #obs-count-tab')
      .forEach(el => { el.textContent = n; });
  }

  function init() {
    refresh();
    window.addEventListener('storage', refresh);
    window.CCObsBadge = { refresh };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
