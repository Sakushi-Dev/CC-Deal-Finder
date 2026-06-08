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
      // For live orders show the real lifecycle status; dry-run/demo are tagged
      // "simulated". Confirmed/open = good, failed = bad, in-flight = neutral.
      const status = it.simulated
        ? 'simulated'
        : (it.status || (it.ok ? 'filled' : 'failed'));
      const cls = it.simulated ? '' : (it.ok ? 'good' : (it.status === 'failed' ? 'bad' : ''));
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
      <td class="${cls}" title="${escapeHtml(it.detail || '')}">${escapeHtml(status)}</td></tr>`;
    }));
  }

  /* ---------------- live exit / relisting + status sync (ETAPPE 6) ------- */
  function renderExit(report) {
    report = report || {};
    const section = $('exitSection');
    const relisted = report.relisted;
    const sync = report.status_sync;
    // Only live cycles carry these keys; hide the whole block otherwise.
    if (!section) return;
    if (relisted === undefined && sync === undefined) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';

    // Status-sync summary bar.
    const syncBar = $('syncBar');
    if (sync && !sync.error) {
      syncBar.style.display = 'flex';
      setText('syncChecked', sync.checked || 0);
      setText('syncConfirmed', sync.confirmed || 0);
      setText('syncCancelled', sync.cancelled || 0);
      setText('syncSpawned', sync.relisted_spawned || 0);
      setText('syncUnresolved', sync.unresolved || 0);
      setText('syncErrors', sync.errors || 0);
      const errEl = $('syncErrors');
      if (errEl) errEl.className = num(sync.errors) > 0 ? 'bad' : '';
    } else {
      syncBar.style.display = 'none';
    }

    // Relisted cards table.
    renderTable('relistTable', 'relistEmpty', 'relistCount', (relisted || []).map((it, i) => {
      const status = it.status || (it.ok ? 'listed' : 'failed');
      const cls = it.ok ? 'good' : (status === 'failed' ? 'bad' : '');
      return `
      <tr><td>${i + 1}</td>
      <td>${escapeHtml(it.name || '')}</td>
      <td>${escapeHtml(it.category || '')}</td>
      <td class="num">${fmtUSD(it.price_usd)}</td>
      <td class="num">${fmtUSD(it.market_usd)}</td>
      <td class="${cls}" title="${escapeHtml(it.detail || '')}">${escapeHtml(status)}</td></tr>`;
    }));
  }

  /* ---------------- holdings inventory + blacklist (ETAPPE 7) ----------- */
  function renderHoldings(s) {
    s = s || {};
    const holdings = s.holdings || [];
    const blacklist = s.blacklist || [];
    const report = s.report || {};

    // Inventory table.
    renderTable('holdingsTable', 'holdingsEmpty', 'holdingsCount', holdings.map((h, i) => {
      const status = h.status || 'held';
      const cls = status === 'sold' ? 'good' : (h.blacklisted ? 'bad' : '');
      return `
      <tr><td>${i + 1}</td>
      <td>${escapeHtml(h.name || '')}</td>
      <td>${escapeHtml(h.category || '')}</td>
      <td class="num">${fmtUSD(h.cost_usd)}</td>
      <td class="num">${fmtUSD(h.market_usd_at_buy)}</td>
      <td class="num">${h.list_price_usd != null ? fmtUSD(h.list_price_usd) : '—'}</td>
      <td class="num">${num(h.markdown_steps)}</td>
      <td class="${cls}">${escapeHtml(status)}</td></tr>`;
    }));

    // Maintenance summary bar (the ETAPPE 6 passes; live cycles only).
    const maintBar = $('maintBar');
    const hasMaint = report.bumped !== undefined || report.marked_down !== undefined;
    if (maintBar) {
      if (hasMaint) {
        maintBar.style.display = 'flex';
        setText('maintBumped', (report.bumped || []).length);
        setText('maintCancelled', (report.cancelled || []).length);
        setText('maintMarkedDown', (report.marked_down || []).length);
        setText('maintAccepted', (report.offers_accepted || []).length);
        const rc = report.market_recheck || {};
        setText('maintRecheck', rc.note ? `re-check: ${rc.note}` : '');
      } else {
        maintBar.style.display = 'none';
      }
    }

    // Bumped offers (offer-penetration bump counter).
    const bumpSection = $('bumpSection');
    const bumped = report.bumped;
    if (bumpSection) {
      if (bumped === undefined) {
        bumpSection.style.display = 'none';
      } else {
        bumpSection.style.display = '';
        renderTable('bumpTable', 'bumpEmpty', 'bumpCount', (bumped || []).map((it, i) => {
          const status = it.status || '—';
          return `
          <tr><td>${i + 1}</td>
          <td>${escapeHtml(it.name || it.nft || '')}</td>
          <td class="num">${fmtUSD(it.new_price_usd)}</td>
          <td class="num">${num(it.bump_count)}</td>
          <td title="${escapeHtml(it.detail || '')}">${escapeHtml(status)}</td></tr>`;
        }));
      }
    }

    // Unpopular blacklist (always visible; per-row clear button).
    renderTable('blacklistTable', 'blacklistEmpty', 'blacklistCount', blacklist.map((nft, i) => `
      <tr><td>${i + 1}</td>
      <td title="${escapeHtml(nft)}">${escapeHtml(nft)}</td>
      <td class="num"><button type="button" class="link-btn" data-clear-nft="${escapeHtml(nft)}">Clear</button></td></tr>`));
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

  /* ---------------- live / safety posture ---------------- */
  function renderLive(auth, counts, recon) {
    auth = auth || {}; counts = counts || {}; recon = recon || {};
    $('liveBar').style.display = 'flex';

    const armPill = $('armPill');
    if (auth.armed) {
      armPill.className = 'status-pill status-running';
      armPill.textContent = 'live armed';
    } else {
      armPill.className = 'status-pill status-idle';
      armPill.textContent = 'dry-run (safe)';
    }
    setText('authProvider', auth.provider || 'none');

    const active = num(counts.submitted) + num(counts.signed) +
                   num(counts.pending) + num(counts.open);
    setText('ordActive', active);
    setText('ordOpen', counts.open || 0);
    setText('ordPending', counts.pending || 0);
    setText('ordFailed', counts.failed || 0);
    const failedEl = $('ordFailed');
    if (failedEl) failedEl.className = num(counts.failed) > 0 ? 'bad' : '';

    const reconPill = $('reconPill');
    const issues = (recon.stale ? recon.stale.length : 0) +
                   (recon.inconsistencies ? recon.inconsistencies.length : 0);
    if (recon.error) {
      reconPill.className = 'status-pill status-stopped';
      reconPill.textContent = 'reconcile error';
    } else if (recon.healthy === false || issues > 0) {
      reconPill.className = 'status-pill status-paused';
      reconPill.textContent = `${issues} to review`;
    } else {
      reconPill.className = 'status-pill status-idle';
      reconPill.textContent = 'reconciled ✓';
    }

    const reasons = auth.blocked_reasons || [];
    setText('armReasons',
      auth.armed ? '' : (reasons.length ? 'blocked: ' + reasons.join('; ') : ''));
  }

  /* ---------------- risk limits / kill switch (ETAPPE 7) ---------------- */
  function renderRisk(risk) {
    risk = risk || {};
    const bar = $('riskBar');
    if (!bar) return;
    if (risk.error) {
      bar.style.display = 'flex';
      const pill = $('riskPill');
      pill.className = 'status-pill status-stopped';
      pill.textContent = 'risk error';
      setText('riskBreaches', escapeHtml(risk.error));
      return;
    }
    bar.style.display = 'flex';
    const limits = risk.limits || {};
    const usage = risk.usage || {};

    // Pill: halted (kill switch) > active limits > no limits.
    const pill = $('riskPill');
    if (risk.halted) {
      pill.className = 'status-pill status-stopped';
      pill.textContent = 'HALTED';
    } else if (risk.enabled) {
      pill.className = 'status-pill status-running';
      pill.textContent = 'limits active';
    } else {
      pill.className = 'status-pill status-idle';
      pill.textContent = 'no limits';
    }

    const capTxt = (v) => (v && v > 0) ? ` / ${fmtUSD(v)}` : '';
    const numCap = (v) => (v && v > 0) ? ` / ${v}` : '';
    setText('riskSpendDay', fmtUSD(usage.spend_today));
    setText('riskSpendCap', capTxt(limits.max_spend_per_day_usd));
    setText('riskOpen', usage.open_positions || 0);
    setText('riskOpenCap', numCap(limits.max_open_positions));
    setText('riskFails', usage.consecutive_failures || 0);
    setText('riskFailCap', numCap(limits.max_consecutive_failures));

    const failsEl = $('riskFails');
    if (failsEl) failsEl.className = (risk.halted) ? 'bad' : '';

    const breaches = risk.breaches || [];
    setText('riskBreaches',
      risk.halt_reason ? risk.halt_reason
        : (breaches.length ? 'blocked: ' + breaches.join('; ') : ''));
  }

  /* ---------------- crash recovery (ETAPPE 8) ---------------- */
  function renderRecovery(rec) {
    const bar = $('recoveryBar');
    if (!bar) return;
    rec = rec || {};
    // Only surface when there is something worth telling the operator: orders
    // were restored from a previous session, or the loop was auto-resumed.
    const inFlight = rec.in_flight || 0;
    if (!rec.performed || (!inFlight && !rec.resumed)) {
      bar.style.display = 'none';
      return;
    }
    bar.style.display = 'flex';
    const pill = $('recoveryPill');
    const parts = [];
    if (rec.resumed) {
      pill.className = 'status-pill status-running';
      pill.textContent = 'loop resumed';
      parts.push('auto-resumed the loop from the last session');
    } else {
      pill.className = 'status-pill status-idle';
      pill.textContent = 'recovered';
    }
    if (inFlight) {
      parts.push(`${inFlight} order${inFlight === 1 ? '' : 's'} in-flight restored`);
    }
    setText('recoveryDetail', parts.join(' · '));
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
    renderLive(s.auth, s.order_counts, s.reconciliation);
    renderExit(s.report);
    // The report's risk posture (live cycle) is richer (breaches/halt); fall
    // back to the snapshot's read-only posture before any cycle has run.
    renderRisk((s.report && s.report.risk) || s.risk);
    renderRecovery(s.recovery);
    renderHoldings(s);

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

  /* ---------------- blacklist clear (event-delegated) ---------------- */
  const blacklistTable = $('blacklistTable');
  if (blacklistTable) {
    blacklistTable.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-clear-nft]');
      if (!btn) return;
      const fd = new FormData();
      fd.append('nft', btn.dataset.clearNft);
      post('/trader/blacklist/clear', fd);
    });
  }

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
  // Per-setting info buttons: toggle the explanation under the field.
  const settingsForm = $('settingsForm');
  if (settingsForm) {
    settingsForm.addEventListener('click', (e) => {
      const btn = e.target.closest('.info-btn');
      if (!btn) return;
      e.preventDefault();
      const help = btn.closest('.set-field')?.querySelector('.set-help');
      if (!help) return;
      const open = help.hidden;
      help.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
    });
  }

  /* ---- category checkbox dropdowns ---- */
  function catSummary(dd) {
    const picked = [...dd.querySelectorAll('.cat-menu input:checked')]
      .map(c => c.value);
    const lbl = dd.querySelector('.cat-toggle-label');
    if (!picked.length) { lbl.textContent = 'All categories'; }
    else if (picked.length <= 2) { lbl.textContent = picked.join(', '); }
    else { lbl.textContent = `${picked.length} selected`; }
  }
  function closeAllCatMenus(except) {
    document.querySelectorAll('[data-cat-dropdown]').forEach(dd => {
      if (dd === except) return;
      dd.querySelector('.cat-menu').hidden = true;
      dd.querySelector('.cat-toggle').setAttribute('aria-expanded', 'false');
    });
  }
  document.querySelectorAll('[data-cat-dropdown]').forEach(dd => {
    const toggle = dd.querySelector('.cat-toggle');
    const menu = dd.querySelector('.cat-menu');
    catSummary(dd);
    toggle.addEventListener('click', (e) => {
      e.preventDefault();
      const willOpen = menu.hidden;
      closeAllCatMenus(dd);
      menu.hidden = !willOpen;
      toggle.setAttribute('aria-expanded', String(willOpen));
    });
    menu.addEventListener('change', () => catSummary(dd));
    menu.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-cat-select-all],[data-cat-clear]');
      if (!btn) return;
      e.preventDefault();
      const checkboxes = menu.querySelectorAll('input[type=checkbox]');
      if (btn.hasAttribute('data-cat-select-all')) {
        checkboxes.forEach(cb => { cb.checked = true; });
      } else {
        checkboxes.forEach(cb => { cb.checked = false; });
      }
      catSummary(dd);
    });
  });
  // Click outside any dropdown closes the open menu.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('[data-cat-dropdown]')) closeAllCatMenus(null);
  });

  /* ---- strategy profiles ---- */
  // Push a server settings list ({env,value,type,...}) back into the form.
  function applySettingsToForm(settings) {
    (settings || []).forEach(f => {
      if (f.type === 'multiselect') {
        const dd = document.querySelector(`[data-cat-dropdown][data-env="${f.env}"]`);
        if (!dd) return;
        const picked = String(f.value || '').split(',').map(s => s.trim()).filter(Boolean);
        dd.querySelectorAll('.cat-menu input').forEach(cb => {
          cb.checked = picked.includes(cb.value);
        });
        catSummary(dd);
      } else {
        const input = $(`set_${f.env}`);
        if (input) input.value = f.value;
      }
    });
  }

  (function profiles() {
    const sel = $('profileSelect');
    if (!sel) return;
    const desc = $('profileDesc');
    const delBtn = $('profileDeleteBtn');
    const msg = $('profileMsg');
    const flash = (text, ok = true) => {
      msg.textContent = text;
      msg.style.color = ok ? 'var(--good)' : 'var(--bad)';
      if (text) setTimeout(() => { if (msg.textContent === text) msg.textContent = ''; }, 2800);
    };
    const post = async (url, body) => {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      return res.json();
    };
    const refreshCustom = (names) => {
      const grp = $('profileCustom');
      grp.innerHTML = '';
      (names || []).forEach(n => {
        const o = document.createElement('option');
        o.value = `custom:${n}`; o.textContent = n;
        grp.appendChild(o);
      });
      grp.hidden = !(names && names.length);
    };

    sel.addEventListener('change', () => {
      const opt = sel.selectedOptions[0];
      const isCustom = sel.value.startsWith('custom:');
      delBtn.hidden = !isCustom;
      const d = opt ? opt.dataset.desc : '';
      if (d) { desc.textContent = d; desc.hidden = false; }
      else { desc.textContent = ''; desc.hidden = true; }
    });

    $('profileApplyBtn').addEventListener('click', async () => {
      if (!sel.value) { flash('Pick a profile first', false); return; }
      const [kind, ...rest] = sel.value.split(':');
      const name = rest.join(':');
      try {
        const j = await post('/trader/profiles/apply', { type: kind, name });
        if (!j.ok) { flash(j.error || 'Error', false); return; }
        applySettingsToForm(j.settings);
        flash('Profile applied — review and Save settings to confirm');
      } catch (err) { flash('Request failed', false); }
    });

    $('profileSaveBtn').addEventListener('click', async () => {
      const name = (prompt('Save current settings as profile:') || '').trim();
      if (!name) return;
      try {
        const j = await post('/trader/profiles/save', { name });
        if (!j.ok) { flash(j.error || 'Error', false); return; }
        refreshCustom(j.custom);
        sel.value = `custom:${name}`;
        sel.dispatchEvent(new Event('change'));
        flash(`Saved “${name}” ✓`);
      } catch (err) { flash('Request failed', false); }
    });

    delBtn.addEventListener('click', async () => {
      if (!sel.value.startsWith('custom:')) return;
      const name = sel.value.slice('custom:'.length);
      if (!confirm(`Delete profile “${name}”?`)) return;
      try {
        const j = await post('/trader/profiles/delete', { name });
        if (!j.ok) { flash(j.error || 'Error', false); return; }
        refreshCustom(j.custom);
        sel.value = '';
        sel.dispatchEvent(new Event('change'));
        flash(`Deleted “${name}”`);
      } catch (err) { flash('Request failed', false); }
    });
  })();

  $('settingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {};
    new FormData(e.target).forEach((v, k) => { payload[k] = v; });
    // Category dropdowns have no form name; collect their ticks by hand.
    document.querySelectorAll('[data-cat-dropdown]').forEach(dd => {
      const env = dd.dataset.env;
      const picked = [...dd.querySelectorAll('.cat-menu input:checked')]
        .map(c => c.value);
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