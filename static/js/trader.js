/* Trading bot page: controls, live polling, P&L overview, settings. */
(function () {
  const { escapeHtml, fmtUSD } = window.CC;

  const errBox = document.getElementById('errorBox');
  let pollTimer = null;

  /* ---------------- helpers ---------------- */
  const $ = id => document.getElementById(id);
  const num = (v, d = 0) => (Number.isFinite(v) ? v : d);

  function fmtTime(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString();
  }

  function setText(id, val) { const el = $(id); if (el) el.textContent = val; }

  const shortAddr = a => (a ? a.slice(0, 4) + '…' + a.slice(-4) : '—');

  /* ---------------- live wallet balances ---------------- */
  async function fetchWallet() {
    try {
      const res = await fetch('/trader/wallet', { cache: 'no-store' });
      const j = await res.json();
      if (!j.ok) {
        if (j.error) { setText('kpiWallet', '—'); $('kpiWallet').title = j.error; }
        return;
      }
      setText('kpiWallet', shortAddr(j.wallet));
      $('kpiWallet').title = j.wallet || '';
      setText('kpiUsdc', fmtUSD(num(j.usdc_balance)));
      setText('kpiSol', num(j.sol_balance).toFixed(4));
      setText('kpiVolume', fmtUSD(num(j.available_volume)));
    } catch (e) { /* ignore */ }
  }

  /* ---------------- status pill ---------------- */
  function setStatus(s) {
    const pill = $('statusPill');
    if (s.running) {
      pill.className = 'status-pill status-running';
      pill.innerHTML = '<span class="spinner"></span>running';
    } else if (s.loop_active && s.paused) {
      pill.className = 'status-pill status-paused';
      pill.textContent = 'paused';
    } else if (s.loop_active) {
      pill.className = 'status-pill status-running';
      pill.textContent = 'looping';
    } else if (s.error) {
      pill.className = 'status-pill status-stopped';
      pill.textContent = 'error';
    } else {
      pill.className = 'status-pill status-idle';
      pill.textContent = 'idle';
    }
  }

  /* ---------------- KPI / report ---------------- */
  function renderReport(r) {
    const empty = $('overviewEmpty');
    if (!r) { empty.style.display = ''; return; }
    empty.style.display = 'none';

    $('modePill').textContent = (r.mode || 'dry-run').toLowerCase();
    $('modePill').className = 'status-pill ' +
      (r.mode === 'LIVE' ? 'status-running' : (r.demo ? 'status-paused' : 'status-idle'));
    if (r.sol_rate) $('solRate').textContent = `SOL/USD: ${r.sol_rate.toFixed(2)}`;

    setText('kpiWallet', r.wallet ? r.wallet.slice(0, 4) + '…' + r.wallet.slice(-4) : '—');    $('kpiWallet').title = r.wallet || '';
    setText('kpiUsdc', fmtUSD(num(r.usdc_balance)));
    setText('kpiSol', num(r.sol_balance).toFixed(4));
    setText('kpiVolume', fmtUSD(num(r.available_volume)));
    setText('kpiDirect', fmtUSD(num(r.direct_budget)));
    setText('kpiOffer', fmtUSD(num(r.offer_budget)));
    setText('kpiCap', fmtUSD(num(r.card_cap_usd)) + (r.escalated ? ' ⬆' : ''));

    const profit = num(r.planned_resell_profit) + num(r.planned_offer_resell_profit);
    const pEl = $('kpiProfit');
    pEl.textContent = (profit >= 0 ? '+' : '') + fmtUSD(profit);
    pEl.parentElement.classList.toggle('kpi-good', profit >= 0);
    pEl.parentElement.classList.toggle('kpi-bad', profit < 0);

    setText('statScanned', r.scanned || 0);
    setText('statCandidates', r.candidates || 0);
    setText('statBuys', r.planned_buys || 0);
    setText('statOffers', r.planned_offers || 0);

    renderTable('buysTable', 'buysEmpty', 'buysCount', (r.items || []).map((it, i) => `
      <tr><td>${i + 1}</td><td>${escapeHtml(it.name)}</td>
      <td>${escapeHtml(it.category || '')}</td>
      <td class="num">${fmtUSD(it.ask_usd)}</td>
      <td class="num">${fmtUSD(it.market_usd)}</td>
      <td class="num">${(it.discount_pct ?? 0).toFixed(1)}%</td>
      <td class="num">${fmtUSD(it.resell_usd)}</td>
      <td class="num good">+${fmtUSD(num(it.resell_profit))}</td></tr>`));

    renderTable('offersTable', 'offersEmpty', 'offersCount', (r.offers || []).map((it, i) => `
      <tr><td>${i + 1}</td><td>${escapeHtml(it.name)}</td>
      <td>${escapeHtml(it.category || '')}</td>
      <td class="num">${fmtUSD(it.ask_usd)}</td>
      <td class="num">${fmtUSD(it.offer_usd)}</td>
      <td class="num">${fmtUSD(it.market_usd)}</td>
      <td class="num">${fmtUSD(it.resell_usd)}</td>
      <td class="num good">+${fmtUSD(num(it.resell_profit))}</td></tr>`));

    renderTable('scanTable', 'scanEmpty', 'scanCount', (r.near_misses || []).map((it, i) => `
      <tr><td>${i + 1}</td><td>${escapeHtml(it.name)}</td>
      <td>${escapeHtml(it.category || '')}</td>
      <td class="num">${fmtUSD(it.ask_usd)}</td>
      <td class="num">${fmtUSD(it.market_usd)}</td>
      <td class="num">${(it.discount_pct ?? 0).toFixed(1)}%</td>
      <td class="num">${fmtUSD(it.resell_usd)}</td>
      <td class="${it.qualifies ? 'good' : 'bad'}">${it.qualifies ? '✓ qualifies' : escapeHtml(it.reason || '—')}</td></tr>`));

    renderTable('executedTable', 'executedEmpty', 'executedCount', (r.executed || []).map((it, i) => {
      const action = it.kind === 'offer' ? 'Offer' : 'Buy';
      const status = it.simulated ? 'simulated' : (it.ok ? 'filled' : 'failed');
      const profit = num(it.resell_usd) - num(it.price_usd);
      return `
      <tr><td>${i + 1}</td>
      <td><b>${action}</b></td>
      <td>${escapeHtml(it.name)}</td>
      <td>${escapeHtml(it.category || '')}</td>
      <td class="num">${fmtUSD(it.price_usd)}</td>
      <td class="num">${fmtUSD(it.market_usd)}</td>
      <td class="num">${fmtUSD(it.resell_usd)}</td>
      <td class="num good">+${fmtUSD(profit)}</td>
      <td class="${it.ok ? 'good' : 'bad'}">${status}</td></tr>`;
    }));
  }

  function renderTable(tableId, emptyId, countId, rows) {
    const tbody = document.querySelector(`#${tableId} tbody`);
    const empty = $(emptyId);
    const table = $(tableId);
    if (countId) setText(countId, rows.length);
    if (!rows.length) {
      tbody.innerHTML = '';
      table.style.display = 'none';
      empty.style.display = '';
      return;
    }
    table.style.display = '';
    empty.style.display = 'none';
    tbody.innerHTML = rows.join('');
  }

  /* ---------------- history ---------------- */
  function renderHistory(history, totals) {
    const rows = (history || []).slice().reverse().map(h => `
      <tr><td>${fmtTime(h.ts)}</td><td>${escapeHtml((h.mode || '').toLowerCase())}</td>
      <td class="num">${fmtUSD(num(h.available_volume))}</td>
      <td class="num">${h.scanned || 0}</td>
      <td class="num">${h.planned_buys || 0}</td>
      <td class="num">${fmtUSD(num(h.planned_cost))}</td>
      <td class="num good">+${fmtUSD(num(h.planned_resell_profit))}</td>
      <td class="num">${h.planned_offers || 0}</td></tr>`);
    renderTable('historyTable', 'historyEmpty', null, rows);

    const t = totals || {};
    const bar = $('totalsBar');
    if (t.cycles) {
      bar.style.display = 'flex';
      setText('totCycles', t.cycles);
      setText('totBuys', t.buys || 0);
      setText('totCost', fmtUSD(num(t.cost)));
      setText('totProfit', '+' + fmtUSD(num(t.resell_profit)));
    } else {
      bar.style.display = 'none';
    }
  }

  /* ---------------- main render ---------------- */
  function render(s) {
    if (!s) return;
    if (s.error) { errBox.textContent = s.error; errBox.style.display = 'block'; }
    else errBox.style.display = 'none';

    setStatus(s);
    setText('statCycles', s.cycles || 0);
    if (s.updated_at) setText('statUpdated', `last update: ${fmtTime(s.updated_at)}`);
    $('statsBar').style.display = 'flex';

    renderReport(s.report);
    renderHistory(s.history, s.totals);

    $('runBtn').disabled = !!s.running;
    $('runBtn').textContent = s.running ? 'Running …' : 'Run cycle now';
    $('startLoopBtn').disabled = s.loop_active;
    $('pauseBtn').disabled = !s.loop_active || s.paused;
    $('resumeBtn').disabled = !s.loop_active || !s.paused;
    $('stopBtn').disabled = !s.loop_active;
  }

  /* ---------------- polling ---------------- */
  async function poll() {
    try {
      const res = await fetch('/trader/status', { cache: 'no-store' });
      render(await res.json());
    } catch (e) { /* retry next tick */ }
  }
  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = setInterval(poll, 4000);
  }

  /* ---------------- controls ---------------- */
  async function post(path, body) {
    const opts = { method: 'POST' };
    if (body) opts.body = body;
    const res = await fetch(path, opts);
    const j = await res.json().catch(() => ({}));
    if (j && j.state) render(j.state);
    return j;
  }

  $('runBtn').addEventListener('click', () => { post('/trader/run'); startPolling(); });
  $('demoBtn').addEventListener('click', async () => {
    const msg = $('demoMsg');
    const fd = new FormData();
    fd.append('volume', $('demoVolume').value || '0');
    const j = await post('/trader/demo', fd);
    if (j && j.ok === false) { msg.textContent = j.error || 'Error'; msg.style.color = 'var(--bad)'; }
    else { msg.textContent = 'Simulating …'; msg.style.color = 'var(--text-muted)'; setTimeout(() => { msg.textContent = ''; }, 2500); }
    startPolling();
  });
  $('startLoopBtn').addEventListener('click', () => {
    const fd = new FormData();
    fd.append('interval', $('intervalInput').value || '300');
    post('/trader/loop/start', fd); startPolling();
  });
  $('pauseBtn').addEventListener('click', () => post('/trader/loop/pause'));
  $('resumeBtn').addEventListener('click', () => post('/trader/loop/resume'));
  $('stopBtn').addEventListener('click', () => post('/trader/loop/stop'));

  /* ---------------- tabs ---------------- */
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const name = tab.dataset.tab;
      document.querySelectorAll('.panel').forEach(p => {
        p.classList.toggle('active', p.dataset.panel === name);
      });
    });
  });

  /* ---------------- settings ---------------- */
  $('settingsToggle').addEventListener('click', () => {
    const p = $('settingsPanel');
    p.style.display = p.style.display === 'none' ? 'flex' : 'none';
  });
  $('settingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {};
    new FormData(e.target).forEach((v, k) => { payload[k] = v; });
    // Multiselect category dropdown has no form name; collect it by hand.
    document.querySelectorAll('[data-cat-field]').forEach(sel => {
      const env = sel.id.replace('set_', '');
      const picked = [...sel.selectedOptions].map(o => o.value);
      payload[env] = picked.join(',');
    });
    const msg = $('settingsMsg');
    try {
      const res = await fetch('/trader/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const j = await res.json();
      if (!j.ok) { msg.textContent = j.error || 'Error'; msg.style.color = 'var(--bad)'; return; }
      msg.textContent = 'Saved ✓'; msg.style.color = 'var(--good)';
      // A new address / reserve may change balances -> refresh immediately.
      fetchWallet();
      setTimeout(() => { msg.textContent = ''; }, 2500);
    } catch (err) {
      msg.textContent = 'Request failed'; msg.style.color = 'var(--bad)';
    }
  });

  /* ---------------- allocation linking ----------------
     Direct% + Offer% are shares of the available volume. If their sum exceeds
     100 the engine normalizes them proportionally, which surprises users (e.g.
     setting Offer to 100 while Direct is still 100 yields a 50/50 split). Keep
     the two inputs from ever summing above 100 by nudging the other one down. */
  (function linkAllocation() {
    const directEl = $('set_TRADER_DIRECT_BUY_PCT');
    const offerEl = $('set_TRADER_OFFER_PCT');
    if (!directEl || !offerEl) return;
    const clamp = (changed, other) => {
      let a = Math.max(0, Math.min(100, parseFloat(changed.value) || 0));
      changed.value = a;
      const b = Math.max(0, parseFloat(other.value) || 0);
      if (a + b > 100) other.value = 100 - a;
    };
    directEl.addEventListener('input', () => clamp(directEl, offerEl));
    offerEl.addEventListener('input', () => clamp(offerEl, directEl));
  })();

  /* ---------------- boot ---------------- */
  render(window.__TRADER_STATE__);
  fetchWallet();
  startPolling();
})();