/* Insured value display in the card footer (.pc).
 *
 * A card must set `data-insured` and (optionally) `data-price`.
 * `renderForCard(card)` is idempotent – can be called multiple times.
 *
 * Side effect: marks the card as `is-top-deal` when the asking price is
 * at least TOP_DEAL_THRESHOLD below the insured value. The deal badge
 * (the `.deal-badge` span injected by the template) becomes visible.
 */
(function () {
  const { parseNum, fmtUSD } = window.CC;
  const TOP_DEAL_THRESHOLD = -25; // ask <= -25 % vs insured  -> "DEAL"

  function ensureBadge(card) {
    let b = card.querySelector('[data-deal-badge]');
    if (!b) {
      b = document.createElement('span');
      b.className = 'deal-badge';
      b.setAttribute('data-deal-badge', '');
      b.textContent = 'DEAL';
      card.prepend(b);
    }
    return b;
  }

  function renderForCard(card) {
    const el = card.querySelector('.pc');
    if (!el) return;
    const insured = parseNum(card.dataset.insured);
    const askUsd  = parseNum(card.dataset.askUsd);
    const ccPrice = parseNum(card.dataset.price);
    const currency = (card.dataset.currency || '').toUpperCase();
    const solRate = window.CCSolRate ? window.CCSolRate.value : null;

    if (!insured) {
      el.innerHTML = '<span style="color:var(--text-dim);">No insured value</span>';
      card.classList.remove('is-top-deal');
      return;
    }

    // Comparison price prefers the real USD asking price (deals page).
    // Otherwise convert SOL→USD on the fly when we have a rate.
    let ask = askUsd;
    if (ask == null && ccPrice != null) {
      if (currency === 'SOL') {
        ask = solRate ? ccPrice * solRate : null;
      } else {
        // USDC / USD / unknown → treat as USD.
        ask = ccPrice;
      }
    }

    let delta = '';
    let diff  = null;
    if (ask != null) {
      diff = ((ask - insured) / insured) * 100;
      const cls = diff >= 0 ? 'pc-delta-up' : 'pc-delta-down';
      const sign = diff >= 0 ? '+' : '';
      delta = ` <span class="${cls}">(${sign}${diff.toFixed(0)}%)</span>`;
    } else if (currency === 'SOL' && !solRate) {
      // SOL price but rate not yet loaded
      delta = ' <span class="pc-loading">(loading…)</span>';
    }
    el.innerHTML =
      `<div class="pc-row"><span>Insured value</span><b>${fmtUSD(insured)}${delta}</b></div>`;

    // Top deal marker (only when we have a real diff)
    const isTopDeal = diff != null && diff <= TOP_DEAL_THRESHOLD;
    card.classList.toggle('is-top-deal', isTopDeal);
    if (isTopDeal) {
      const b = ensureBadge(card);
      b.textContent = `DEAL ${diff.toFixed(0)}%`;
    }
  }

  function wire(nodes) { nodes.forEach(renderForCard); }
  window.CCInsured = { wire, renderForCard };
})();
