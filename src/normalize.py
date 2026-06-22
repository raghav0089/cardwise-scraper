"""Final pass before DDB: enforce card_id, ISO timestamps, dedupe."""
from __future__ import annotations
import re, datetime as dt
from rapidfuzz import fuzz

# Words that are NOT distinguishing when comparing card names within the same issuer.
# If the only difference between two names is these words, treat them as the same card.
_NAME_STOPWORDS = frozenset({
    "card", "credit", "debit", "prepaid", "forex",
    "rbl", "bank", "the", "a", "an", "of", "for",
})

def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "unknown"

def ensure_card_id(c: dict) -> dict:
    issuer = c.get("issuer_id") or slugify(c.get("issuer_name") or "")
    name = c.get("card_name") or ""
    c["issuer_id"] = issuer
    if not c.get("card_id"):
        c["card_id"] = f"{issuer}__{slugify(name)}"
    return c

def stamp(c: dict) -> dict:
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    c["last_scraped_at"] = now
    c.setdefault("first_seen_at", now)
    c.setdefault("status", "active")
    return c

def _similar_names(a: str, b: str) -> bool:
    """True when two card names refer to the same product.

    token_set_ratio scores 100 whenever one name's tokens are a subset of the
    other's, even for unrelated cards that share generic words like "Max" or
    "Plus". Guard: if either side has unique meaningful tokens (not in
    _NAME_STOPWORDS), the names describe different products.
    """
    if not a or not b:
        return False
    if a.lower() == b.lower():
        return True
    a_tok = set(a.lower().split())
    b_tok = set(b.lower().split())
    only_a = (a_tok - b_tok) - _NAME_STOPWORDS
    only_b = (b_tok - a_tok) - _NAME_STOPWORDS
    # Both sides have unique meaningful words → clearly different products
    if only_a and only_b:
        return False
    # One side is a superset: merge only if the extra words are all stopwords
    if only_a or only_b:
        if (a_tok ^ b_tok) - _NAME_STOPWORDS:
            return False
    return fuzz.token_set_ratio(a, b) >= 92


def dedupe(cards: list[dict]) -> list[dict]:
    """Merge duplicates by card_id, prefer record with more populated fields."""
    by_id: dict[str, dict] = {}
    for c in cards:
        cid = c["card_id"]
        if cid not in by_id:
            by_id[cid] = c; continue
        a, b = by_id[cid], c
        by_id[cid] = a if _populated(a) >= _populated(b) else b
    # near-duplicate name collapse within same issuer (skip cards without a name)
    final, used = [], set()
    items = list(by_id.values())
    for i, a in enumerate(items):
        if i in used: continue
        a_name = a.get("card_name") or ""
        for j in range(i + 1, len(items)):
            b = items[j]
            b_name = b.get("card_name") or ""
            if a_name and b_name and \
               a.get("issuer_id") == b.get("issuer_id") and \
               _similar_names(a_name, b_name):
                used.add(j)
        final.append(a)
    return final

def _populated(c: dict) -> int:
    return sum(1 for v in c.values() if v not in (None, "", [], {}))
