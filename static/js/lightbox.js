/* CC Lightbox – modulare Karten-Vorschau mit Flip + Parallax.
 *
 * Wird einmal pro Seite eingebunden, injiziert sich selbst (CSS + DOM)
 * und exponiert:
 *   window.CCLightbox.open(frontSrc, backSrc?)
 *   window.CCLightbox.close()
 *   window.CCLightbox.wire(elements, getSources)
 *     – elements: NodeList/Array von DOM-Knoten
 *     – getSources(el, event) -> { front, back } | null
 *       (Default liest data-image-full / data-image-back vom nächsten .card)
 */
(function () {
  if (window.CCLightbox) return;

  // --- CSS injizieren ----------------------------------------------------
  const CSS = `
    .cc-lb-overlay { position:fixed; inset:0; background:rgba(5,5,10,.85);
      display:none; align-items:center; justify-content:center; z-index:9999;
      cursor:zoom-out; backdrop-filter: blur(4px); perspective: 1200px; }
    .cc-lb-overlay.open { display:flex; }
    .cc-lb-stage { position:relative; max-width:min(92vw,820px);
      max-height:92vh; transform-style: preserve-3d;
      transition: transform .08s ease-out; will-change: transform; }
    .cc-lb-flipper { position:relative; width:100%; height:100%;
      transform-style: preserve-3d; transition: transform .6s ease;
      cursor:pointer; }
    .cc-lb-flipper.flipped { transform: rotateY(180deg); }
    .cc-lb-face { position:relative; backface-visibility: hidden;
      -webkit-backface-visibility: hidden; }
    .cc-lb-face.back { position:absolute; inset:0; transform: rotateY(180deg); }
    .cc-lb-face img { display:block; max-width:100%; max-height:92vh;
      border-radius:14px; box-shadow:0 30px 80px rgba(0,0,0,.7),
        0 0 0 1px rgba(255,255,255,.05); }
    .cc-lb-face.back img { width:100%; height:100%; object-fit:contain; background:#0b0b10; }
    .cc-lb-stage .cc-lb-gloss { position:absolute; inset:0; border-radius:14px;
      pointer-events:none; mix-blend-mode: overlay; opacity:.35;
      background: radial-gradient(circle at var(--gx,50%) var(--gy,50%),
        rgba(255,255,255,.55), rgba(255,255,255,0) 40%);
      transform: translateZ(1px); }
    .cc-lb-close { position:fixed; top:1rem; right:1.25rem; color:#ddd;
      font-size:1.6rem; cursor:pointer; opacity:.7; z-index:10000; }
    .cc-lb-close:hover { opacity:1; }
  `;
  const styleEl = document.createElement('style');
  styleEl.textContent = CSS;
  document.head.appendChild(styleEl);

  // --- DOM injizieren ----------------------------------------------------
  const overlay = document.createElement('div');
  overlay.className = 'cc-lb-overlay';
  overlay.innerHTML = `
    <div class="cc-lb-close" aria-label="schließen">×</div>
    <div class="cc-lb-stage">
      <div class="cc-lb-flipper">
        <div class="cc-lb-face front"><img alt=""></div>
        <div class="cc-lb-face back"><img alt=""></div>
      </div>
      <div class="cc-lb-gloss"></div>
    </div>
  `;
  const attach = () => {
    if (document.body) document.body.appendChild(overlay);
    else document.addEventListener('DOMContentLoaded', attach, { once: true });
  };
  attach();

  const stage   = overlay.querySelector('.cc-lb-stage');
  const flipper = overlay.querySelector('.cc-lb-flipper');
  const imgF    = overlay.querySelector('.cc-lb-face.front img');
  const imgB    = overlay.querySelector('.cc-lb-face.back  img');
  const gloss   = overlay.querySelector('.cc-lb-gloss');
  const closeBtn = overlay.querySelector('.cc-lb-close');

  function open(front, back) {
    if (!front) return;
    imgF.src = front;
    imgB.src = back || '';
    flipper.classList.remove('flipped');
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function close() {
    overlay.classList.remove('open');
    document.body.style.overflow = '';
    stage.style.transform = '';
    flipper.classList.remove('flipped');
    gloss.style.removeProperty('--gx');
    gloss.style.removeProperty('--gy');
  }

  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  flipper.addEventListener('click', e => {
    e.stopPropagation();
    if (imgB.src) flipper.classList.toggle('flipped');
  });

  // Parallax: Karte neigt sich Richtung Mauszeiger (max ~15°).
  const MAX_DEG = 15;
  stage.addEventListener('mousemove', e => {
    const r = stage.getBoundingClientRect();
    const dx = (e.clientX - (r.left + r.width  / 2)) / (r.width  / 2);
    const dy = (e.clientY - (r.top  + r.height / 2)) / (r.height / 2);
    const rx = Math.max(-1, Math.min(1, -dy)) * MAX_DEG;
    const ry = Math.max(-1, Math.min(1,  dx)) * MAX_DEG;
    stage.style.transform =
      `rotateX(${rx.toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg) scale(1.02)`;
    gloss.style.setProperty('--gx',
      (((e.clientX - r.left) / r.width)  * 100).toFixed(1) + '%');
    gloss.style.setProperty('--gy',
      (((e.clientY - r.top)  / r.height) * 100).toFixed(1) + '%');
  });
  stage.addEventListener('mouseleave', () => {
    stage.style.transform = '';
    gloss.style.removeProperty('--gx');
    gloss.style.removeProperty('--gy');
  });

  // Default-Quellen: data-image-full / data-image-back von .card-Vorfahr.
  function defaultSources(el) {
    const card = el.closest('.card');
    if (!card) return null;
    const front = card.dataset.imageFull || card.dataset.image;
    const back  = card.dataset.imageBack || '';
    return front ? { front, back } : null;
  }

  function wire(elements, getSources) {
    const fn = getSources || defaultSources;
    (elements || []).forEach(el => {
      if (el.__ccLbWired) return;
      el.__ccLbWired = true;
      el.style.cursor = el.style.cursor || 'zoom-in';
      el.addEventListener('click', ev => {
        const s = fn(el, ev);
        if (s && s.front) { ev.preventDefault(); open(s.front, s.back); }
      });
    });
  }

  window.CCLightbox = { open, close, wire };
})();
