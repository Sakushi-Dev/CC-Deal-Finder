/* Marketplace-Filterleiste (Marketplace, Blockchain, Grade, Language,
 * Preis, Categories) – persistent in localStorage.
 *
 *   CCFilters.init({ gridSelector, cardSelector })
 */
(function () {
  const KEY = 'cc_filters_v1';
  const DEFAULTS = {
    marketplace: '', blockchain: '', language: '',
    gradeMin: '', gradeMax: '', priceMin: '', priceMax: '',
    categories: [],
  };
  const $ = id => document.getElementById(id);

  function init(opts) {
    opts = opts || {};
    const cards = Array.from(document.querySelectorAll(
      (opts.gridSelector || '#grid-marketplace') + ' ' + (opts.cardSelector || '.card')
    ));
    let state = { ...DEFAULTS };
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) state = { ...DEFAULTS, ...JSON.parse(raw) };
    } catch (e) {}

    const fillSelect = (sel, values) => {
      const cur = sel.value;
      [...sel.querySelectorAll('option:not([value=""])')].forEach(o => o.remove());
      [...values].sort().forEach(v => {
        const o = document.createElement('option');
        o.value = v; o.textContent = v;
        sel.appendChild(o);
      });
      sel.value = cur;
    };
    const mpSet = new Set(), bcSet = new Set();
    cards.forEach(c => {
      if (c.dataset.marketplace) mpSet.add(c.dataset.marketplace);
      if (c.dataset.blockchain)  bcSet.add(c.dataset.blockchain);
    });
    fillSelect($('f-marketplace'), mpSet);
    fillSelect($('f-blockchain'), bcSet);

    $('f-marketplace').value = state.marketplace || '';
    $('f-blockchain').value  = state.blockchain  || '';
    $('f-language').value    = state.language    || '';
    $('f-grade-min').value   = state.gradeMin    || '';
    $('f-grade-max').value   = state.gradeMax    || '';
    $('f-price-min').value   = state.priceMin    || '';
    $('f-price-max').value   = state.priceMax    || '';
    document.querySelectorAll('#f-categories input').forEach(cb => {
      cb.checked = state.categories.includes(cb.value);
    });

    const num = v => { const n = parseFloat(v); return isFinite(n) ? n : null; };

    function apply() {
      const gMin = num(state.gradeMin), gMax = num(state.gradeMax);
      const pMin = num(state.priceMin), pMax = num(state.priceMax);
      const cats = new Set(state.categories);
      cards.forEach(c => {
        let show = true;
        if (state.marketplace && c.dataset.marketplace !== state.marketplace) show = false;
        if (state.blockchain  && c.dataset.blockchain  !== state.blockchain)  show = false;
        if (state.language    && c.dataset.language    !== state.language)    show = false;
        if (cats.size && !cats.has(c.dataset.category))                       show = false;
        const grade = num(c.dataset.grade);
        if (gMin !== null && (grade === null || grade < gMin)) show = false;
        if (gMax !== null && (grade === null || grade > gMax)) show = false;
        const price = num(c.dataset.price);
        if (pMin !== null && (price === null || price < pMin)) show = false;
        if (pMax !== null && (price === null || price > pMax)) show = false;
        c.classList.toggle('hidden', !show);
      });
    }
    const save = () => localStorage.setItem(KEY, JSON.stringify(state));
    const bind = (id, key, parse = v => v) => {
      $(id).addEventListener('input', () => {
        state[key] = parse($(id).value); save(); apply();
      });
    };
    bind('f-marketplace', 'marketplace');
    bind('f-blockchain', 'blockchain');
    bind('f-language', 'language');
    bind('f-grade-min', 'gradeMin');
    bind('f-grade-max', 'gradeMax');
    bind('f-price-min', 'priceMin');
    bind('f-price-max', 'priceMax');
    document.querySelectorAll('#f-categories input').forEach(cb => {
      cb.addEventListener('change', () => {
        state.categories = [...document.querySelectorAll('#f-categories input:checked')]
          .map(x => x.value);
        save(); apply();
      });
    });
    $('f-reset').addEventListener('click', () => {
      state = { ...DEFAULTS };
      save();
      ['f-marketplace','f-blockchain','f-language','f-grade-min',
       'f-grade-max','f-price-min','f-price-max'].forEach(i => $(i).value = '');
      document.querySelectorAll('#f-categories input').forEach(cb => cb.checked = false);
      apply();
    });
    apply();
  }

  window.CCFilters = { init };
})();
