"""Final pass before DDB: enforce card_id, ISO timestamps, dedupe."""
from __future__ import annotations
import re, datetime as dt
from rapidfuzz import fuzz

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

def dedupe(cards: list[dict]) -> list[dict]:
    """Merge duplicates by card_id, prefer record with more populated fields."""
    by_id: dict[str, dict] = {}
    for c in cards:
        cid = c["card_id"]
        if cid not in by_id:
            by_id[cid] = c; continue
        a, b = by_id[cid], c
        by_id[cid] = a if _populated(a) >= _populated(b) else b
    # near-duplicate name collapse within same issuer
    final, used = [], set()
    items = list(by_id.values())
    for i, a in enumerate(items):
        if i in used: continue
        for j in range(i + 1, len(items)):
            b = items[j]
            if a["issuer_id"] == b["issuer_id"] and \
               fuzz.token_set_ratio(a["card_name"], b["card_name"]) >= 92:
                used.add(j)
        final.append(a)
    return final

def _populated(c: dict) -> int:
    return sum(1 for v in c.values() if v not in (None, "", [], {}))
