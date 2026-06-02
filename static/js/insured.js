/* Insured-Value-Anzeige im Karten-Footer (.pc).
 *
 * Eine Karte muss `data-insured` und (optional) `data-price` setzen.
 * `renderForCard(card)` ist idempotent – mehrfach aufrufbar.
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
      el.innerHTML = '<span style="color:#888;">Kein Insured Value</span>';
      return;
    }

    // Vergleichspreis bevorzugt der echte USD-Asking-Preis (Deals); sonst Listing-Preis.
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
