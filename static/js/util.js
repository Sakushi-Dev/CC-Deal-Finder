/* Kleine, gemeinsam genutzte Helfer. */
(function () {
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => (
      { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[ch]
    ));
  }
  function parseNum(v) {
    if (v == null || v === '') return null;
    const n = parseFloat(String(v).replace(/[^0-9.\-]/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  function fmtUSD(v) {
    if (!Number.isFinite(v)) return '—';
    return '$' + v.toLocaleString('en-US',
      { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 });
  }
  function fmtPrice(v) {
    const n = parseNum(v);
    if (n == null) return '—';
    if (n >= 100) return n.toLocaleString('en-US', { maximumFractionDigits: 0 });
    return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }
  window.CC = Object.assign(window.CC || {},
    { escapeHtml, parseNum, fmtUSD, fmtPrice });
})();
