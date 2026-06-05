/* Deals page: scan controls, live polling, card/list view. */
(function () {
  const { escapeHtml, fmtUSD, fmtPrice } = window.CC;

  const grid    = document.getElementById('grid-deals');
  const emptyEl = document.getElementById('deals-empty');
  const errBox  = document.getElementById('errorBox');

  const knownKeys = new Set();
  let pollTimer = null;

  /* Highlight thresholds (positive pct = discount vs insured value). */
  const GOOD_DEAL_PCT = 10;
  const MEGA_DEAL_PCT = 30;

  function pctClass(pct) {
    if (pct >= MEGA_DEAL_PCT) return 'good';
    if (pct >= GOOD_DEAL_PCT) return 'good';
    if (pct >= 0)             return 'mid';
    return 'bad';
  }
  const dealKey = d => d.nft || d.url;

  function dealToCard(d) {
    const k = dealKey(d);
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.nft        = d.nft || '';
    card.dataset.title      = d.name || '';
    card.dataset.category   = d.category || '';
    card.dataset.price      = d.price_raw || '';
    card.dataset.askUsd     = d.ask_usd != null ? String(d.ask_usd) : '';
    card.dataset.currency   = d.currency || '';
    card.dataset.year       = d.year || '';
    card.dataset.grading    = d.grading || '';
    card.dataset.grade      = d.grade_num || '';
    card.dataset.company    = d.grading_company || '';
    card.dataset.gradeStr   = d.grade_str || '';
    card.dataset.number     = d.card_number || '';
    card.dataset.language   = d.language || '';
    card.dataset.cardName   = d.card_name || '';
    card.dataset.set        = d.set || '';
    card.dataset.insured    = d.insured_value != null ? String(d.insured_value) : '';
    card.dataset.marketplace= d.marketplace || '';
    card.dataset.blockchain = d.blockchain || '';
    card.dataset.image      = d.image || '';
    card.dataset.imageFull  = d.image_full || d.image || '';
    card.dataset.imageBack  = d.image_back || '';
    card.dataset.ccUrl      = d.url;
    card.dataset.key        = k;

    const obsOn = CCObserve.has(k) ? ' on' : '';
    const obsLbl = CCObserve.has(k) ? '★' : 'observe';

    const cls = pctClass(d.pct);
    /* Discount is reported as positive pct by the scanner. We display it
     * with a leading minus sign to match the user's mental model
     * ("-25% vs insured value"). */
    const dispPct = -Math.abs(d.pct);
    const deltaStr = (d.delta >= 0 ? '-' : '+') + fmtUSD(Math.abs(d.delta));
    const askDisplay = fmtUSD(d.ask_usd) +
      ` <span class="currency">${escapeHtml(d.currency || '')}</span>`;

    /* Highlight classes: blue edge from ≥10% discount, mega-shimmer from ≥30%. */
    if (d.pct >= MEGA_DEAL_PCT)      card.classList.add('is-mega-deal');
    else if (d.pct >= GOOD_DEAL_PCT) card.classList.add('is-good-deal');

    /* The marketplace gold DEAL badge would also fire here (insured.js).
     * On the deals page we use the blue/mega highlight instead — strip it. */
    const badgeLabel = d.pct >= MEGA_DEAL_PCT ? `MEGA -${d.pct.toFixed(0)}%`
                     : d.pct >= GOOD_DEAL_PCT ? `-${d.pct.toFixed(0)}%`
                     : '';

    card.innerHTML = `
      ${badgeLabel ? `<span class="deal-badge deal-badge-static">${badgeLabel}</span>` : ''}
      <button type="button" class="obs-btn${obsOn}" title="observe">${obsLbl}</button>
      <div class="detail">
        ${d.image ? `<img src="${escapeHtml(d.image)}" alt="" loading="lazy">` : ''}
        <div class="body">
          <div class="name">${escapeHtml(d.name)}</div>
          <div class="sub">${[d.year, d.grading, d.category]
            .filter(Boolean).map(escapeHtml).join(' · ')}</div>
          <div class="price-row">
            <span class="price">${askDisplay}</span>
            <a class="cc-link" href="${d.url}" target="_blank" rel="noopener"
               onclick="event.stopPropagation()">↗</a>
          </div>
        </div>
      </div>
      <div class="pc">
        <div class="pc-row"><span>Insured value</span><b>${fmtUSD(d.market_usd)}</b></div>
        <div class="pc-row"><span>Δ USD</span>
          <b class="deal-pct ${cls}">${deltaStr}</b></div>
        <div class="pc-row"><span>Discount</span>
          <b class="deal-pct ${cls}">${dispPct.toFixed(1)}%</b></div>
      </div>`;
    return card;
  }

  function renderDeals(deals) {
    grid.innerHTML = '';
    /* Filter out non-discount listings: ask price above the insured value
     * is not a deal — hide it. The server still counts them in "scanned",
     * we only suppress the visual noise. */
    const onlyDiscounts = deals.filter(d => d.pct >= 0);
    onlyDiscounts.sort((a, b) => b.pct - a.pct);
    const seen = new Set();
    onlyDiscounts.forEach(d => {
      const k = dealKey(d);
      const card = dealToCard(d);
      if (!knownKeys.has(k)) card.classList.add('new-card');
      seen.add(k);
      grid.appendChild(card);
    });
    knownKeys.clear();
    seen.forEach(k => knownKeys.add(k));

    /* Note: we DON'T call CCInsured.wire here. The deals page has its own
     * (richer) footer and its own highlight classes — running the
     * marketplace top-deal logic would only re-add the gold border. */
    CCLightbox.wire(grid.querySelectorAll('.card .detail'));
    CCObserveCards.wireGrid(document.createElement('div'), {
      sourceCards: () => grid.querySelectorAll('.card'),
    });
  }

  function setStatus(state) {
    const pill = document.getElementById('statusPill');
    if (state.running && state.paused) {
      pill.className = 'status-pill status-paused';
      pill.textContent = 'paused';
    } else if (state.running) {
      pill.className = 'status-pill status-running';
      pill.innerHTML = '<span class="spinner"></span>running';
    } else if (state.done && state.stop_requested) {
      pill.className = 'status-pill status-stopped';
      pill.textContent = 'stopped';
    } else if (state.done) {
      pill.className = 'status-pill status-done';
      pill.textContent = 'done';
    } else {
      pill.className = 'status-pill status-idle';
      pill.textContent = 'ready';
    }
  }

  function render(state) {
    if (state.error) { errBox.textContent = state.error; errBox.style.display = 'block'; }
    else { errBox.style.display = 'none'; }

    document.getElementById('solRate').textContent =
      state.sol_rate ? `SOL/USD: ${state.sol_rate.toFixed(2)}` : '';

    const hasAny = state.running || state.done || (state.deals && state.deals.length);
    document.getElementById('statsBar').style.display = hasAny ? 'flex' : 'none';
    setStatus(state);

    document.getElementById('statPage').textContent = state.page || 0;
    document.getElementById('statTotalPages').textContent = state.total_pages || 0;
    document.getElementById('statScanned').textContent = state.scanned || 0;
    document.getElementById('statInRange').textContent = state.in_range || 0;
    document.getElementById('statMatched').textContent = state.matched || 0;
    document.getElementById('statRange').textContent =
      (state.min_usd != null && state.max_usd != null)
        ? `$${state.min_usd.toFixed(0)} – $${state.max_usd.toFixed(0)}` : '–';
    const catEl = document.getElementById('statCategory');
    if (state.category) {
      catEl.style.display = '';
      catEl.textContent = `Category: ${state.category}`;
    } else {
      catEl.style.display = 'none';
    }
    if (state.updated_at) {
      const dt = new Date(state.updated_at * 1000);
      document.getElementById('statUpdated').textContent =
        `last update: ${dt.toLocaleTimeString()}`;
    }
    const pageErr = document.getElementById('statPageErr');
    const failed = (state.failed_pages || []).length;
    if (failed) {
      pageErr.style.display = '';
      pageErr.textContent = `Failed pages: ${failed}` +
        (state.last_page_error ? ` (last: ${state.last_page_error})` : '');
    } else {
      pageErr.style.display = 'none';
    }

    const btn = document.getElementById('scanBtn');
    btn.disabled = !!state.running;
    btn.textContent = state.running ? 'Scan running …' : 'Start scan';

    document.getElementById('pauseBtn').disabled  = !state.running || state.paused || state.stop_requested;
    document.getElementById('resumeBtn').disabled = !state.running || !state.paused;
    document.getElementById('stopBtn').disabled   = !state.running || state.stop_requested;

    const deals = state.deals || [];
    const discounts = deals.filter(d => d.pct >= 0);
    if (!discounts.length) {
      grid.innerHTML = '';
      emptyEl.style.display = '';
      emptyEl.innerHTML = state.running
        ? '<span class="spinner"></span> No discounts yet – scan running …'
        : (state.done
            ? (deals.length
                ? `Scanned ${deals.length} listings – none priced below their insured value.`
                : 'No cards in this price range with an insured value found.')
            : 'Enter a price range and start the scan.');
      knownKeys.clear();
      return;
    }
    emptyEl.style.display = 'none';
    renderDeals(deals);
  }

  async function poll() {
    try {
      const res = await fetch('/deals/status', { cache: 'no-store' });
      render(await res.json());
    } catch (e) { /* next tick will try again */ }
  }
  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = setInterval(poll, 5000);
  }

  document.getElementById('scanForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const res = await fetch('/deals/start', { method: 'POST', body: new FormData(e.target) });
    const j = await res.json();
    if (!j.ok) {
      errBox.textContent = j.error || 'Error while starting.';
      errBox.style.display = 'block';
      return;
    }
    knownKeys.clear();
    render(j.state);
    startPolling();
  });

  async function sendControl(path) {
    try {
      const res = await fetch(path, { method: 'POST' });
      const j = await res.json();
      if (j && j.state) render(j.state);
      startPolling();
    } catch (e) {}
  }
  document.getElementById('pauseBtn').addEventListener('click', () => sendControl('/deals/pause'));
  document.getElementById('resumeBtn').addEventListener('click', () => sendControl('/deals/resume'));
  document.getElementById('stopBtn').addEventListener('click', () => {
    if (confirm('Really stop the scan? Existing hits will be kept.')) {
      sendControl('/deals/stop');
    }
  });

  render(window.__INITIAL_STATE__ || {});
  if ((window.__INITIAL_STATE__ || {}).running) startPolling();
})();
