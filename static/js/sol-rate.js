/* SOL→USD spot price (cached in sessionStorage for 10 min).
 *
 * Other modules (insured.js, etc.) call window.CCSolRate.get() — which
 * returns a Promise<number|null>. They can also read .value synchronously
 * once it's been resolved.
 *
 * Refreshes once on page load, then once every 10 minutes while the
 * page stays open. Updates any element with `[data-sol-rate]` (e.g. the
 * deals topbar) automatically.
 */
(function () {
  const KEY = 'cc_sol_rate_v1';
  const TTL_MS = 10 * 60 * 1000;

  let value = null;
  let pending = null;

  function fromCache() {
    try {
      const raw = sessionStorage.getItem(KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (!o || typeof o.rate !== 'number') return null;
      if (Date.now() - (o.ts || 0) > TTL_MS) return null;
      return o.rate;
    } catch (e) { return null; }
  }

  function toCache(rate) {
    try { sessionStorage.setItem(KEY, JSON.stringify({ rate, ts: Date.now() })); }
    catch (e) {}
  }

  function paint() {
    document.querySelectorAll('[data-sol-rate]').forEach(el => {
      el.textContent = value ? `SOL/USD ${value.toFixed(2)}` : '';
    });
  }

  function reRenderCards() {
    if (!window.CCInsured) return;
    document.querySelectorAll('.card').forEach(c => {
      try { window.CCInsured.renderForCard(c); } catch (e) {}
    });
  }

  function refresh(force) {
    if (!force) {
      const cached = fromCache();
      if (cached) { value = cached; paint(); return Promise.resolve(value); }
    }
    if (pending) return pending;
    pending = fetch('/api/sol-rate', { headers: { Accept: 'application/json' } })
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (j && j.ok && typeof j.rate === 'number') {
          value = j.rate;
          toCache(value);
          paint();
          reRenderCards();
        }
        return value;
      })
      .catch(() => value)
      .finally(() => { pending = null; });
    return pending;
  }

  window.CCSolRate = {
    get value() { return value; },
    get: refresh,
    refresh: () => refresh(true),
  };

  function init() {
    refresh(false);
    setInterval(() => refresh(true), TTL_MS);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
