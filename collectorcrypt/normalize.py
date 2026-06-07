"""Parsing/formatting for CollectorCrypt cards.

Encapsulates everything we apply to raw cards from the CC API before they
go to the frontend or the scanner:

* :func:`normalize_card` – raw dict → unified UI schema.
* :func:`format_price`   – nicely formatted price string.
* :func:`to_usd`         – price × currency → USD (USDC/USD/SOL).
"""
from __future__ import annotations

import re
from typing import Any

from .config import COLLECTORCRYPT_ASSET_URL, LANGUAGE_TOKENS

# --------------------------------------------------------------------------- #
# Regex constants – compile only once.
# --------------------------------------------------------------------------- #
_GRADING_TOKEN_RE = re.compile(
    r"\b("
    r"PSA|CGC|CGS|Beckett|BGS|SGC|TAG|KSA|CSG|UDA|Steiner|BBCE|"
    r"Rare\s+Edition"
    r")\b",
    re.IGNORECASE,
)
_YEAR_PREFIX_RE = re.compile(r"^\s*(?:19|20)\d{2}\s+")
_HASH_NUMBER_RE = re.compile(r"#(\S+)")
_GRADE_NUM_RE = re.compile(r"(\d{1,2}(?:\.\d)?)")
_FULL_ART_SLASH_RE = re.compile(r"\bFull\s*Art\s*/\s*", re.IGNORECASE)
_FULL_ART_RE = re.compile(r"\bFull\s*Art\b\s*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def normalize_card(card: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw CC API card into the unified UI schema."""
    listing = card.get("listing") or {}
    images = card.get("images") or {}

    grade_str = card.get("grade") or ""
    item_name = card.get("itemName") or "Unnamed"
    set_name = card.get("set") or ""
    grading_company = card.get("gradingCompany") or ""

    card_name, card_number = _parse_item_name(item_name, grading_company)
    card_name = _strip_full_art(card_name)
    language = _detect_language(item_name, set_name, card.get("language") or "")
    nft = card.get("nftAddress", "") or ""

    return {
        "name": item_name,
        "card_id": card.get("id", "") or "",
        "card_name": card_name,
        "card_number": card_number,
        "language": language,
        "category": card.get("category") or "",
        "grading": " ".join(filter(None, [grading_company, grade_str])),
        "grading_company": grading_company,
        "grade_str": grade_str,
        "grade_num": card.get("gradeNum") or _extract_grade_num(grade_str),
        "year": card.get("year") or "",
        "set": set_name,
        "insured_value": _parse_insured_value(card.get("insuredValue")),
        "price": format_price(listing.get("price")),
        "price_raw": listing.get("price") or "",
        "currency": listing.get("currency") or "",
        "image": images.get("frontS") or images.get("front") or card.get("frontImage"),
        "image_full": (
            images.get("front") or images.get("frontM")
            or images.get("frontS") or card.get("frontImage")
        ),
        "image_back": (
            images.get("back") or images.get("backM")
            or images.get("backS") or card.get("backImage")
        ),
        "nft": nft,
        "blockchain": card.get("blockchain") or "",
        "marketplace": listing.get("marketplace") or "",
        "url": COLLECTORCRYPT_ASSET_URL.format(nft=nft),
    }


def format_price(value: Any) -> str:
    """Format prices as a string (`12,345` or `12.5`)."""
    if value in (None, ""):
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n >= 100:
        return f"{n:,.0f}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def to_usd(price: Any, currency: str, sol_rate: float) -> float | None:
    """Convert price to USD. USDC/USD 1:1, SOL via the current spot rate."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    cur = (currency or "").upper()
    if cur in ("USDC", "USD"):
        return p
    if cur == "SOL":
        return p * sol_rate
    return None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _parse_insured_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _detect_language(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    for lang in LANGUAGE_TOKENS:
        if re.search(rf"\b{lang}\b", blob, re.IGNORECASE):
            return lang
    return "English"


def _parse_item_name(item_name: str, grading_company: str) -> tuple[str, str]:
    """`(name_with_hash, number)` – name from `#` up to the grading token."""
    if not item_name:
        return "", ""
    m = _GRADING_TOKEN_RE.search(item_name)
    end = m.start() if m else len(item_name)
    hash_idx = item_name.find("#")
    if hash_idx == -1 or hash_idx >= end:
        head = _YEAR_PREFIX_RE.sub("", item_name[:end]).strip()
        return head, ""
    name = item_name[hash_idx:end].strip()
    num_m = _HASH_NUMBER_RE.match(name)
    return name, (num_m.group(1) if num_m else "")


def _strip_full_art(text: str) -> str:
    if not text:
        return text
    s = _FULL_ART_SLASH_RE.sub("", text)
    s = _FULL_ART_RE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _extract_grade_num(grade_str: str) -> str:
    if not grade_str:
        return ""
    m = _GRADE_NUM_RE.search(grade_str)
    return m.group(1) if m else ""
