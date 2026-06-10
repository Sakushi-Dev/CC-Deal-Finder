"""CollectorCrypt trading-client transport tests.

The client is the only thing that talks to CC's trading endpoints. Two
properties are safety-critical for live mode and pinned hard here:

* **Writes are NEVER auto-retried** (buy/offer/list/broadcast) — a silent retry
  could double-spend. Only idempotent reads retry on 429/5xx/network.
* **No secret ever reaches a log sink** — :func:`redact` masks every sensitive
  key, recursively.
"""
from __future__ import annotations

import pytest
import requests

from collectorcrypt.trader import ccapi
from collectorcrypt.trader.auth import NullSessionProvider
from collectorcrypt.trader.ccapi import (CCAuthError, CCClientError,
                                         CCNetworkError, CCRateLimitError,
                                         CCServerError, CCTradingClient, redact)

from .conftest import FakeSessionProvider


# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    """Scripted requests.Session replacement that records every call."""

    def __init__(self, responses):
        # responses: list of FakeResponse or Exception to raise
        self._responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def request(self, method, url, *, params=None, json=None, headers=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers})
        item = self._responses.pop(0) if self._responses else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually sleep during retry/backoff tests."""
    monkeypatch.setattr(ccapi, "_sleep", lambda *_a, **_k: None)


def make_client(responses, *, provider=None, max_retries=3):
    http = FakeHTTP(responses)
    client = CCTradingClient(
        session_provider=provider or FakeSessionProvider(),
        http=http, max_retries=max_retries)
    return client, http


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
SENSITIVE_KEYS = ["authorization", "token", "secret", "bearer", "signature",
                  "signedTransaction", "signedTx", "privateKey", "password",
                  "cookie", "Authorization", "TOKEN", "Bearer"]


@pytest.mark.parametrize("key", SENSITIVE_KEYS)
def test_redact_masks_sensitive_key(key):
    out = redact({key: "supersecretvalue12345"})
    assert out[key] != "supersecretvalue12345"


@pytest.mark.parametrize("key", ["nft", "price", "currency", "status", "id",
                                 "name", "amount"])
def test_redact_keeps_safe_keys(key):
    out = redact({key: "visiblevalue"})
    assert out[key] == "visiblevalue"


def test_redact_recurses_into_nested_dict():
    out = redact({"outer": {"token": "abcdefghij"}})
    assert out["outer"]["token"] != "abcdefghij"


def test_redact_recurses_into_list():
    out = redact([{"secret": "abcdefghij"}, {"nft": "ok"}])
    assert out[0]["secret"] != "abcdefghij"
    assert out[1]["nft"] == "ok"


def test_redact_masks_short_value_fully():
    out = redact({"token": "short"})
    assert out["token"] == "***"


def test_redact_masks_long_value_partial():
    out = redact({"token": "abcdefghijklmnop"})
    assert out["token"] == "abcd…op"


def test_redact_non_string_secret_becomes_stars():
    out = redact({"token": 12345})
    assert out["token"] == "***"


def test_redact_does_not_mutate_input():
    src = {"token": "abcdefghij", "nft": "x"}
    redact(src)
    assert src["token"] == "abcdefghij"


# --------------------------------------------------------------------------- #
# Successful reads
# --------------------------------------------------------------------------- #
def test_check_listing_status_returns_payload():
    client, _ = make_client([FakeResponse(200, {"exists": True})])
    assert client.check_listing_status(nft="N", wallet="W") == {"exists": True}


def test_check_listing_status_uses_rpc_v2():
    client, http = make_client([FakeResponse(200, {"exists": False})])
    client.check_listing_status(nft="N1", wallet="W1")
    assert http.calls[0]["method"] == "POST"
    assert http.calls[0]["url"].endswith("/v2")
    assert http.calls[0]["json"] == {
        "method": "checkListingStatus",
        "params": {"nftAddress": "N1", "wallet": "W1"}}


def test_calc_listing_fee_params():
    client, http = make_client([FakeResponse(200, {"fee": 1.0})])
    client.calc_listing_fee(nft="N", price=10.0)
    assert http.calls[0]["params"]["nftAddress"] == "N"
    assert http.calls[0]["params"]["price"] == 10.0


def test_non_dict_body_wrapped_in_data():
    client, _ = make_client([FakeResponse(200, [1, 2, 3])])
    assert client.check_listing_status(nft="N", wallet="W") == {"data": [1, 2, 3]}


def test_bare_text_body_wrapped_in_data():
    # marketplace/buy returns a bare base64 tx string (not JSON).
    client, _ = make_client([FakeResponse(200, None, text="BASE64TX==")])
    assert client.initiate_buy(nft="N", price=1.0, wallet="W") == {
        "data": "BASE64TX=="}


def test_auth_header_attached():
    client, http = make_client([FakeResponse(200, {"ok": True})],
                               provider=FakeSessionProvider(token="tok42"))
    client.check_listing_status(nft="N", wallet="W")
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tok42"


# --------------------------------------------------------------------------- #
# Write body shapes
# --------------------------------------------------------------------------- #
def test_initiate_buy_body():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.initiate_buy(nft="NFT", price=12.5, wallet="WALLET")
    body = http.calls[0]["json"]
    assert body == {"currency": "USDC", "nftAddress": "NFT", "price": 12.5,
                    "wallet": "WALLET", "fundingSource": "wallet"}


def test_initiate_buy_funding_source_escrow():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.initiate_buy(nft="NFT", price=12.5, wallet="W",
                        funding_source="escrow")
    assert http.calls[0]["json"]["fundingSource"] == "escrow"


def test_make_offer_body():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.make_offer(nft="NFT", card_id="CARD1", price=8.0, wallet="W")
    assert http.calls[0]["url"].endswith("marketplace/make-offer")
    assert http.calls[0]["json"] == {"cardId": "CARD1", "currency": "USDC",
                                     "nftAddress": "NFT", "price": 8.0,
                                     "wallet": "W"}


def test_update_offer_body():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.update_offer(nft="NFT", price=9.5, wallet="W")
    assert http.calls[0]["url"].endswith("marketplace/update-offer")
    assert http.calls[0]["json"] == {"buyer": "W", "currency": "USDC",
                                     "nftAddress": "NFT", "price": 9.5,
                                     "wallet": "W"}


def test_create_listing_body():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.create_listing(nft="NFT", card_id="CARD1", price=25.0, wallet="W")
    assert http.calls[0]["url"].endswith("marketplace/list")
    assert http.calls[0]["json"] == {"cardId": "CARD1", "currency": "USDC",
                                     "nftAddress": "NFT", "price": 25.0,
                                     "wallet": "W"}


def test_broadcast_body():
    client, http = make_client([FakeResponse(200, {"signature": "s"})])
    client.broadcast(signed_tx="SIGNEDTX")
    assert http.calls[0]["json"] == {"signedTransaction": "SIGNEDTX"}


def test_broadcast_body_with_wallet_and_nft():
    client, http = make_client([FakeResponse(200, {"signature": "s"})])
    client.broadcast(signed_tx="SIGNEDTX", wallet="W", nft="N")
    assert http.calls[0]["json"] == {"signedTransaction": "SIGNEDTX",
                                     "wallet": "W", "nftAddress": "N"}


def test_cancel_listing_body():
    client, http = make_client([FakeResponse(200, {"ok": True})])
    client.cancel_listing(nft="NFT", wallet="W")
    assert http.calls[0]["url"].endswith("marketplace/cancel-listing")
    assert http.calls[0]["json"] == {"coin": "USDC", "seller": "W",
                                     "tokenMint": "NFT", "wallet": "W"}


def test_cancel_offer_body():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.cancel_offer(nft="NFT", wallet="W")
    assert http.calls[0]["url"].endswith("marketplace/cancel-offer")
    assert http.calls[0]["json"] == {"coin": "USDC", "keepInEscrow": False,
                                     "nftAddress": "NFT", "wallet": "W"}


def test_broadcast_parses_success_and_signature():
    client, http = make_client([FakeResponse(
        200, {"success": True, "signature": "SIG9",
              "message": "Transaction broadcast successfully"})])
    resp = client.broadcast(signed_tx="SIGNEDTX", wallet="W", nft="N")
    assert resp["success"] is True
    assert resp["signature"] == "SIG9"


def test_update_listing_body():
    client, http = make_client([FakeResponse(200, {"data": "tx"})])
    client.update_listing(nft="NFT", price=15.0, wallet="W1")
    assert http.calls[0]["url"].endswith("marketplace/update-listing")
    assert http.calls[0]["json"] == {"coin": "USDC", "newPrice": 15.0,
                                     "seller": "W1", "tokenMint": "NFT",
                                     "wallet": "W1"}


def test_accept_offer_body():
    client, http = make_client([FakeResponse(200, {"data": "tx"})])
    client.accept_offer(nft="NFT", buyer="B1", price=114.7, wallet="W1")
    assert http.calls[0]["url"].endswith("marketplace/accept-offer")
    assert http.calls[0]["json"] == {"buyer": "B1", "currency": "USDC",
                                     "nftAddress": "NFT", "price": 114.7,
                                     "wallet": "W1"}


def test_get_card_activity_path_and_params():
    client, http = make_client([FakeResponse(200, [{"action": "Offer Made"}])])
    client.get_card_activity(nft="N1")
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"].endswith("card-activity/N1")
    assert http.calls[0]["params"] == {"day": 60, "v2": "true"}


def test_get_card_activity_wraps_array_as_data():
    feed = [{"action": "Offer Made", "amount": 10.0}]
    client, _ = make_client([FakeResponse(200, feed)])
    assert client.get_card_activity(nft="N") == {"data": feed}


def test_get_card_activity_retries_as_read():
    # A read GET is idempotent -> retried on a transient 5xx then succeeds.
    client, http = make_client([
        FakeResponse(503, {"error": "busy"}),
        FakeResponse(200, [{"action": "Offer Made"}]),
    ])
    assert client.get_card_activity(nft="N") == {"data": [{"action": "Offer Made"}]}
    assert len(http.calls) == 2


def test_get_owned_cards_path_and_params():
    client, http = make_client([FakeResponse(200, {"filterNFtCard": []})])
    client.get_owned_cards(wallet="WALLET1")
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"].endswith("cards/WALLET1/")
    assert http.calls[0]["params"] == {"page": 1, "step": 96,
                                       "orderBy": "dateDesc"}


def test_get_owned_cards_returns_payload():
    payload = {"totalCards": 1, "totalPages": 1,
               "filterNFtCard": [{"nftAddress": "N1", "listing": None}]}
    client, _ = make_client([FakeResponse(200, payload)])
    assert client.get_owned_cards(wallet="W") == payload


def test_get_owned_cards_retries_as_read():
    # A read GET is idempotent -> retried on a transient 5xx then succeeds.
    client, http = make_client([
        FakeResponse(503, {"error": "busy"}),
        FakeResponse(200, {"filterNFtCard": []}),
    ])
    assert client.get_owned_cards(wallet="W") == {"filterNFtCard": []}
    assert len(http.calls) == 2


def test_get_wallet_activity_path_and_params():
    # Wallet-wide feed: bare `card-activity` (no path segment) with wallet+v2
    # only. `day` is omitted by default — the probe showed it silently drops
    # most of the feed on the wallet-wide variant.
    client, http = make_client([FakeResponse(200, [{"action": "Sale"}])])
    client.get_wallet_activity(wallet="WALLET1")
    assert http.calls[0]["method"] == "GET"
    assert http.calls[0]["url"].endswith("/card-activity")
    assert http.calls[0]["params"] == {"wallet": "WALLET1", "v2": "true"}


def test_get_wallet_activity_passes_day_when_given():
    client, http = make_client([FakeResponse(200, [])])
    client.get_wallet_activity(wallet="W", day=30)
    assert http.calls[0]["params"] == {"wallet": "W", "day": 30, "v2": "true"}


def test_get_wallet_activity_wraps_array_as_data():
    feed = [{"action": "Offer Made", "amount": 20.0}]
    client, _ = make_client([FakeResponse(200, feed)])
    assert client.get_wallet_activity(wallet="W") == {"data": feed}


def test_get_wallet_activity_retries_as_read():
    # A read GET is idempotent -> retried on a transient 5xx then succeeds.
    client, http = make_client([
        FakeResponse(503, {"error": "busy"}),
        FakeResponse(200, [{"action": "Sale"}]),
    ])
    assert client.get_wallet_activity(wallet="W") == {"data": [{"action": "Sale"}]}
    assert len(http.calls) == 2


def test_accept_offer_not_retried(monkeypatch):
    # A state-changing write is never auto-retried (no double-accept).
    client, http = make_client([
        FakeResponse(503, {"error": "busy"}),
        FakeResponse(200, {"data": "tx"}),
    ])
    with pytest.raises(CCServerError):
        client.accept_offer(nft="N", buyer="B", price=10.0, wallet="W")
    assert len(http.calls) == 1


def test_update_listing_not_retried(monkeypatch):
    client, http = make_client([
        FakeResponse(503, {"error": "busy"}),
        FakeResponse(200, {"data": "tx"}),
    ])
    with pytest.raises(CCServerError):
        client.update_listing(nft="N", price=10.0, wallet="W")
    assert len(http.calls) == 1


def test_write_extra_merged():
    client, http = make_client([FakeResponse(200, {"transaction": "tx"})])
    client.initiate_buy(nft="N", price=1.0, wallet="W", extra={"slippage": 5})
    assert http.calls[0]["json"]["slippage"] == 5


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,exc", [
    (400, CCClientError), (404, CCClientError), (409, CCClientError),
    (422, CCClientError),
])
def test_4xx_maps_to_client_error(status, exc):
    client, _ = make_client([FakeResponse(status, {"message": "bad"})])
    with pytest.raises(exc):
        client.check_listing_status(nft="N", wallet="W")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_maps_to_auth_error(status):
    client, _ = make_client([FakeResponse(status, {"message": "no"})])
    with pytest.raises(CCAuthError):
        client.check_listing_status(nft="N", wallet="W")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_maps_to_server_error_after_retries(status):
    client, _ = make_client([FakeResponse(status, {"message": "boom"})] * 4)
    with pytest.raises(CCServerError):
        client.check_listing_status(nft="N", wallet="W")


def test_429_maps_to_rate_limit_after_retries():
    client, _ = make_client([FakeResponse(429, {"message": "slow"})] * 4)
    with pytest.raises(CCRateLimitError):
        client.check_listing_status(nft="N", wallet="W")


def test_network_error_maps():
    client, _ = make_client([requests.ConnectionError("down")] * 4)
    with pytest.raises(CCNetworkError):
        client.check_listing_status(nft="N", wallet="W")


def test_error_message_extracted():
    client, _ = make_client([FakeResponse(400, {"error": "specific reason"})])
    with pytest.raises(CCClientError) as ei:
        client.check_listing_status(nft="N", wallet="W")
    assert "specific reason" in str(ei.value)


# --------------------------------------------------------------------------- #
# Retry semantics — reads retry, writes NEVER retry
# --------------------------------------------------------------------------- #
def test_read_retries_on_5xx_then_succeeds():
    client, http = make_client([
        FakeResponse(500, {"message": "x"}),
        FakeResponse(200, {"exists": True}),
    ])
    assert client.check_listing_status(nft="N", wallet="W") == {"exists": True}
    assert len(http.calls) == 2


def test_read_retries_on_429_then_succeeds():
    client, http = make_client([
        FakeResponse(429, {"message": "x"}, headers={"Retry-After": "0"}),
        FakeResponse(200, {"exists": True}),
    ])
    client.check_listing_status(nft="N", wallet="W")
    assert len(http.calls) == 2


def test_read_retries_on_network_then_succeeds():
    client, http = make_client([
        requests.ConnectionError("down"),
        FakeResponse(200, {"exists": True}),
    ])
    client.check_listing_status(nft="N", wallet="W")
    assert len(http.calls) == 2


def test_read_exhausts_max_retries():
    client, http = make_client([FakeResponse(500, {"message": "x"})] * 4,
                               max_retries=3)
    with pytest.raises(CCServerError):
        client.check_listing_status(nft="N", wallet="W")
    assert len(http.calls) == 4  # 1 + 3 retries


@pytest.mark.parametrize("call", [
    lambda c: c.initiate_buy(nft="N", price=1.0, wallet="W"),
    lambda c: c.make_offer(nft="N", card_id="C", price=1.0, wallet="W"),
    lambda c: c.update_offer(nft="N", price=1.0, wallet="W"),
    lambda c: c.cancel_offer(nft="N", wallet="W"),
    lambda c: c.create_listing(nft="N", card_id="C", price=1.0, wallet="W"),
    lambda c: c.broadcast(signed_tx="S"),
])
def test_write_never_retries_on_5xx(call):
    client, http = make_client([FakeResponse(500, {"message": "x"})] * 4)
    with pytest.raises(CCServerError):
        call(client)
    assert len(http.calls) == 1  # NO retry for a write


@pytest.mark.parametrize("call", [
    lambda c: c.initiate_buy(nft="N", price=1.0, wallet="W"),
    lambda c: c.make_offer(nft="N", card_id="C", price=1.0, wallet="W"),
    lambda c: c.update_offer(nft="N", price=1.0, wallet="W"),
    lambda c: c.cancel_offer(nft="N", wallet="W"),
    lambda c: c.broadcast(signed_tx="S"),
])
def test_write_never_retries_on_429(call):
    client, http = make_client([FakeResponse(429, {"message": "x"})] * 4)
    with pytest.raises(CCRateLimitError):
        call(client)
    assert len(http.calls) == 1


@pytest.mark.parametrize("call", [
    lambda c: c.initiate_buy(nft="N", price=1.0, wallet="W"),
    lambda c: c.broadcast(signed_tx="S"),
])
def test_write_never_retries_on_network(call):
    client, http = make_client([requests.ConnectionError("down")] * 4)
    with pytest.raises(CCNetworkError):
        call(client)
    assert len(http.calls) == 1


# --------------------------------------------------------------------------- #
# Auth-failure handling
# --------------------------------------------------------------------------- #
def test_401_invalidates_session_and_does_not_retry():
    provider = FakeSessionProvider(token="tok")
    client, http = make_client([FakeResponse(401, {"message": "stale"})] * 4,
                               provider=provider)
    with pytest.raises(CCAuthError):
        client.check_listing_status(nft="N", wallet="W")
    assert provider.invalidated == 1
    assert len(http.calls) == 1  # auth failure never retried


def test_null_provider_refuses_before_any_http():
    http = FakeHTTP([FakeResponse(200, {"ok": True})])
    client = CCTradingClient(session_provider=NullSessionProvider(), http=http)
    with pytest.raises(CCAuthError):
        client.check_listing_status(nft="N", wallet="W")
    assert len(http.calls) == 0  # never reached the network


def test_provider_error_propagates_on_write():
    provider = FakeSessionProvider(error=CCAuthError("no session"))
    http = FakeHTTP([FakeResponse(200, {"transaction": "tx"})])
    client = CCTradingClient(session_provider=provider, http=http)
    with pytest.raises(CCAuthError):
        client.initiate_buy(nft="N", price=1.0, wallet="W")
    assert len(http.calls) == 0


# --------------------------------------------------------------------------- #
# Retry-After parsing
# --------------------------------------------------------------------------- #
def test_retry_after_header_used(monkeypatch):
    slept = []
    monkeypatch.setattr(ccapi, "_sleep", lambda s: slept.append(s))
    client, _ = make_client([
        FakeResponse(429, {"message": "x"}, headers={"Retry-After": "2"}),
        FakeResponse(200, {"ok": True}),
    ])
    client.check_listing_status(nft="N", wallet="W")
    assert 2.0 in slept
