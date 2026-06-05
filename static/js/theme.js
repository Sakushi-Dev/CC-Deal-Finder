/* Theme switcher (dark / light) – persisted in localStorage. */
(function () {
  const KEY = 'cc_theme_v1';

  function apply(theme) {
    const t = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    document.querySelectorAll('[data-theme-set]').forEach(btn => {
      btn.classList.toggle('active', btn.getAttribute('data-theme-set') === t);
    });
  }

  function init() {
    apply(localStorage.getItem(KEY) || 'dark');
    document.querySelectorAll('[data-theme-set]').forEach(btn => {
      btn.addEventListener('click', () => {
        const t = btn.getAttribute('data-theme-set');
        localStorage.setItem(KEY, t);
        apply(t);
      });
    });

    /* Mark current page in sidenav. */
    const path = location.pathname.replace(/\/+$/, '') || '/';
    const map = { '/': 'marketplace', '/deals': 'deals', '/trader': 'trader', '/profile': 'profile' };
    const cur = map[path];
    // Observed link is active when we're on / with the #observe hash.
    const observeActive = path === '/' && location.hash === '#observe';
    document.querySelectorAll('[data-nav]').forEach(a => {
      const tag = a.getAttribute('data-nav');
      if (tag === 'observe') {
        a.classList.toggle('active', observeActive);
      } else if (tag === cur && !observeActive) {
        a.classList.add('active');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
