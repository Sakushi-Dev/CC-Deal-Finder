/* Profile page: persist wallet in localStorage and auto-load it. */
(function () {
  const KEY = 'cc.profile.wallet';
  const input = document.getElementById('walletInput');
  const clearBtn = document.getElementById('clearWalletBtn');
  if (!input) return;

  const urlWallet = new URLSearchParams(location.search).get('wallet');

  // Persist the current wallet from the URL (when the form was submitted).
  if (urlWallet) {
    try { localStorage.setItem(KEY, urlWallet); } catch (_) {}
  } else {
    // No wallet in URL: pre-fill from localStorage and auto-redirect.
    let saved = '';
    try { saved = localStorage.getItem(KEY) || ''; } catch (_) {}
    if (saved) {
      input.value = saved;
      // Auto-load on first visit (only when no params at all).
      if (!location.search) {
        location.replace('/profile?wallet=' + encodeURIComponent(saved));
      }
    }
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      try { localStorage.removeItem(KEY); } catch (_) {}
      input.value = '';
      location.replace('/profile');
    });
  }

  // Wire observe/lightbox on profile cards (no observe grid here, pass a noop).
  const grid = document.getElementById('grid-profile');
  if (grid && window.CCObserveCards) {
    CCInsured.wire(grid.querySelectorAll('.card'));
    CCLightbox.wire(grid.querySelectorAll('.card .detail'));
    CCObserveCards.wireGrid(document.createElement('div'), {
      sourceCards: () => grid.querySelectorAll('.card'),
    });
  }
})();
