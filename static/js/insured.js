/* Insured value display in the card footer (.pc).
 *
 * A card must set `data-insured` and (optionally) `data-price`.
 * `renderForCard(card)` is idempotent – can be called multiple times.
 */
(function () {
  const { parseNum, fmtUSD } = window.CC;

  function renderForCard(card) {
    const el = card.querySelector('.pc');
    if (!el) return;
    const insured = parseNum(card.dataset.insured);
    const askUsd  = parseNum(card.dataset.askUsd);
    const ccPrice = parseNum(card.dataset.price);

    if (!insured) {
      el.innerHTML = '<span style="color:#888;">No insured value</span>';
      return;
    }

    // Comparison price prefers the real USD asking price (deals); otherwise listing price.
    const ask = askUsd != null ? askUsd : ccPrice;
    let delta = '';
    if (ask != null) {
      const diff = ((ask - insured) / insured) * 100;
      const cls = diff >= 0 ? 'pc-delta-up' : 'pc-delta-down';
      const sign = diff >= 0 ? '+' : '';
      delta = ` <span class="${cls}">(${sign}${diff.toFixed(0)}%)</span>`;
    }
    el.innerHTML =
      `<div class="pc-row"><span>Insured Value</span><b>${fmtUSD(insured)}${delta}</b></div>`;
  }

  function wire(nodes) { nodes.forEach(renderForCard); }
  window.CCInsured = { wire, renderForCard };
})();
