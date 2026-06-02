/* Observe-Logik für Card-Grids: Button-Wiring, Snapshot-Mapping, Live-Refresh.
 *
 * Verwendung pro Seite:
 *   CCObserveCards.wireGrid(gridEl, {
 *     emptyEl, countEl,           // optional UI-Elemente
 *     onSnapshot,                 // (card) -> snap (Datenextraktion)
 *     renderObserveCard(snap),    // erstellt DOM-Element fürs Observe-Grid
 *     refreshFromApi: true,       // /api/card/<nft> Aufruf für Live-Daten
 *   })
 */
(function () {
  const { escapeHtml, fmtPrice } = window.CC;
  const Observe = window.CCObserve;

  /** Standard-Snapshot aus `data-*`-Attributen einer Card. */
  function defaultSnapshot(card) {
    const d = card.dataset;
    return {
      nft: d.nft, title: d.title, category: d.category,
      price: d.price, currency: d.currency, year: d.year,
      grading: d.grading, grade: d.grade, company: d.company,
      gradeStr: d.gradeStr, number: d.number,
      language: d.language, cardName: d.cardName, set: d.set,
      insured: d.insured,
      marketplace: d.marketplace, blockchain: d.blockchain,
      image: d.image, imageFull: d.imageFull, imageBack: d.imageBack,
      ccUrl: d.ccUrl,
    };
  }

  /** Standard-Renderer: identische Card-Optik wie Marketplace. */
  function defaultRender(snap) {
    const el = document.createElement('div');
    el.className = 'card';
    Object.entries(snap).forEach(([k, v]) => { el.dataset[k] = v || ''; });
    el.innerHTML = `
      <button type="button" class="obs-btn on" title="observe entfernen">observe</button>
      <div class="detail">
        ${snap.image ? `<img src="${snap.image}" alt="" loading="lazy">` : ''}
        <div class="body">
          <div class="name">${escapeHtml(snap.title || '')}</div>
          <div class="sub">${[snap.year, snap.grading, snap.category]
            .filter(Boolean).map(escapeHtml).join(' · ')}</div>
          <div class="price-row">
            <span class="price">${fmtPrice(snap.price)}<span class="currency">${escapeHtml(snap.currency || '')}</span></span>
            <a class="cc-link" href="${snap.ccUrl}" target="_blank" rel="noopener"
               onclick="event.stopPropagation()">↗</a>
          </div>
        </div>
      </div>
      <div class="pc"></div>`;
    return el;
  }

  /** Mapping API-Response (/api/card) → Snapshot. */
  function snapFromApi(c) {
    return {
      nft: c.nft, title: c.name, category: c.category,
      price: c.price_raw, currency: c.currency, year: c.year,
      grading: c.grading, grade: c.grade_num, company: c.grading_company,
      gradeStr: c.grade_str, number: c.card_number,
      language: c.language, cardName: c.card_name, set: c.set,
      insured: c.insured_value != null ? String(c.insured_value) : '',
      marketplace: c.marketplace, blockchain: c.blockchain,
      image: c.image, imageFull: c.image_full, imageBack: c.image_back,
      ccUrl: c.url,
    };
  }

  function cardId(card) { return card.dataset.nft || card.dataset.ccUrl; }

  function wireGrid(gridEl, opts) {
    opts = opts || {};
    const snapshot = opts.onSnapshot || defaultSnapshot;
    const render   = opts.renderObserveCard || defaultRender;
    const sourceCards = opts.sourceCards || (() => []);
    const emptyEl = opts.emptyEl;
    const countEl = opts.countEl;

    function updateCount() {
      if (countEl) countEl.textContent = Observe.count();
    }
    function setEmpty() {
      if (emptyEl) emptyEl.style.display = Observe.count() ? 'none' : '';
    }

    function wireButton(card, btn) {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const id = cardId(card);
        if (!id) return;
        if (Observe.has(id)) {
          Observe.remove(id);
          btn.classList.remove('on');
          btn.textContent = 'observe';
          if (card.parentElement === gridEl) {
            card.remove();
            setEmpty();
          }
        } else {
          Observe.set(id, snapshot(card));
          btn.classList.add('on');
        }
        updateCount();
      });
    }

    /** Buttons in einem (Quell-)Grid verkabeln + Status anzeigen. */
    function wireCards(cards) {
      cards.forEach(card => {
        const id = cardId(card);
        const btn = card.querySelector('.obs-btn');
        if (id && btn && Observe.has(id)) btn.classList.add('on');
        if (btn) wireButton(card, btn);
      });
    }

    /** Observe-Grid neu aufbauen. */
    function rebuild() {
      gridEl.innerHTML = '';
      const keys = Observe.keys();
      setEmpty();
      keys.forEach(k => {
        const snap = Observe.get(k);
        const card = render(snap);
        gridEl.appendChild(card);
      });
      const cards = gridEl.querySelectorAll('.card');
      cards.forEach(c => {
        const btn = c.querySelector('.obs-btn');
        if (btn) wireButton(c, btn);
      });
      if (window.CCInsured) window.CCInsured.wire(cards);
      if (window.CCLightbox) {
        window.CCLightbox.wire(gridEl.querySelectorAll('.card .detail'));
      }
      if (opts.refreshFromApi) refreshFromApi();
    }

    /** Karten frisch von der CC-API holen (Preis/Insured aktualisieren). */
    function refreshFromApi() {
      gridEl.querySelectorAll('.card').forEach(card => {
        const nft = card.dataset.nft;
        if (!nft) return;
        fetch('/api/card/' + encodeURIComponent(nft), { cache: 'no-store' })
          .then(r => r.json())
          .then(data => {
            if (!data || !data.found || !data.card) {
              const pc = card.querySelector('.pc');
              if (pc) pc.innerHTML = '<span style="color:#888;">Nicht mehr gelistet</span>';
              return;
            }
            const snap = snapFromApi(data.card);
            const id = cardId(card);
            if (id) Observe.update(id, snap);
            Object.entries(snap).forEach(([k, v]) => { card.dataset[k] = v || ''; });
            const nameEl = card.querySelector('.name');
            if (nameEl) nameEl.textContent = snap.title || '';
            const subEl = card.querySelector('.sub');
            if (subEl) {
              subEl.textContent = [snap.year, snap.grading, snap.category]
                .filter(Boolean).join(' · ');
            }
            const priceEl = card.querySelector('.price');
            if (priceEl) {
              priceEl.innerHTML =
                `${fmtPrice(snap.price)}<span class="currency">` +
                `${escapeHtml(snap.currency || '')}</span>`;
            }
            if (window.CCInsured) window.CCInsured.renderForCard(card);
          })
          .catch(() => { /* offline: bestehende Anzeige bleibt */ });
      });
    }

    updateCount();
    wireCards(sourceCards());
    return { rebuild, refreshFromApi, updateCount, setEmpty };
  }

  window.CCObserveCards = { wireGrid, snapFromApi, defaultSnapshot, defaultRender };
})();
