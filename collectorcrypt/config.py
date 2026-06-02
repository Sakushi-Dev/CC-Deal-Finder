"""Application constants (URLs, limits, retry policy)."""
from __future__ import annotations

API_BASE = "https://api.collectorcrypt.com"
MARKETPLACE_URL = f"{API_BASE}/marketplace"
PUBLIC_NFT_URL_TEMPLATE = f"{API_BASE}/cards/publicNft/{{nft}}"
COLLECTORCRYPT_ASSET_URL = "https://collectorcrypt.com/assets/solana/{nft}"

COINBASE_SOL_URL = "https://api.coinbase.com/v2/prices/SOL-USD/spot"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Marketplace listing
DEFAULT_STEP = 30
MAX_STEP = 100
MIN_STEP = 6

# Scanner (deals)
SCAN_STEP = 100

# HTTP
REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 30
RETRY_DELAYS = (3, 10, 30)
RETRY_STATUSES = frozenset({403, 429, 502, 503, 504})

# Allowed languages, otherwise fallback "English"
LANGUAGE_TOKENS = ("Japanese", "Korean", "Chinese", "Spanish")
