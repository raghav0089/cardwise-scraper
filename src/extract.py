"""LLM-based extractor — rotates across up to 3 Gemini API keys to stay
within the free-tier daily quota (1,500 req/day per key).

Set keys via env vars:
    GEMINI_API_KEY      (required)
    GEMINI_API_KEY_2    (optional)
    GEMINI_API_KEY_3    (optional)

On a 429, the current key is marked exhausted and the next key is tried
automatically. If all keys are exhausted, returns None so the caller knows
not to mark sources as seen (they'll retry tomorrow).
"""
from __future__ import annotations
import os, json, logging
from pathlib import Path
from jsonschema import Draft7Validator
from google import genai

log = logging.getLogger(__name__)
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
MODEL     = os.getenv("LLM_MODEL", "gemini-2.0-flash-lite")

SYSTEM = """You extract Indian payment-card product details from web pages.

You will receive one or more pages, each wrapped in --- DOCUMENT <n> --- blocks.

Return STRICT JSON: {"cards":[<card>, ...]} — a flat list across ALL pages.
Each card must match the IndianCard schema. RULES:

- If a page is not about a specific payment card product
  (credit/debit/prepaid/forex/corporate), emit nothing for it.
- A single page may describe many cards — emit one record per distinct card.
- Use INR numeric values (e.g. 12500, not "₹12,500"). Strip GST language
  but set fees.gst_extra=true if "+GST" / "exclusive of GST" appears.
- Convert reward-rate phrases to base_rate_pct (1 RP per ₹150 with RP=₹0.25
  => base_rate_pct = 0.25/150*100 = 0.167).
- Be conservative: leave a field null rather than guess. Set "confidence"
  between 0 and 1 reflecting how complete the page was.
- card_id = "<issuer_id>__<slug-of-card_name>".
- Always set "source_url" in each card to the exact SOURCE_URL from its document block.
- Return ONLY the JSON object. No markdown fences, no preamble."""


# ── Key rotation ──────────────────────────────────────────────────────────────

def _load_keys() -> list[str]:
    keys = []
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    if not keys:
        raise RuntimeError("No Gemini API keys configured. Set GEMINI_API_KEY.")
    return keys

_keys:           list[str] = []
_key_index:      int       = 0        # which key we're currently using
_exhausted:      set[int]  = set()    # indices of quota-exhausted keys
_clients:        dict[int, genai.Client] = {}


def _get_client() -> tuple[int, genai.Client] | None:
    """Return (index, client) for the next available key, or None if all exhausted."""
    global _keys, _key_index
    if not _keys:
        _keys = _load_keys()

    # try current key first, then wrap around
    for _ in range(len(_keys)):
        if _key_index not in _exhausted:
            if _key_index not in _clients:
                _clients[_key_index] = genai.Client(api_key=_keys[_key_index])
            return _key_index, _clients[_key_index]
        _key_index = (_key_index + 1) % len(_keys)

    return None   # all keys exhausted


def _mark_exhausted(index: int) -> None:
    _exhausted.add(index)
    remaining = len(_keys) - len(_exhausted)
    log.warning("Gemini key #%d exhausted. %d key(s) remaining.", index + 1, remaining)
    global _key_index
    _key_index = (index + 1) % len(_keys)


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_cards(batch: list[dict]) -> list[dict] | None:
    """Extract card records from a batch of pages in one Gemini call.

    Rotates keys on 429. Returns:
        list[dict]  — extracted cards (may be empty)
        None        — all keys exhausted; caller should NOT mark sources as seen
    """
    valid = [p for p in batch if p.get("markdown") and len(p["markdown"]) >= 200]
    if not valid:
        return []

    parts = []
    for i, p in enumerate(valid, 1):
        parts.append(
            f"--- DOCUMENT {i} ---\n"
            f"SOURCE_URL: {p['source_url']}\n"
            f"ISSUER_ID_HINT: {p.get('issuer_id') or ''}\n"
            f"ISSUER_NAME_HINT: {p.get('issuer_name') or ''}\n\n"
            f"{p['markdown'][:12000]}\n"
            f"--- END DOCUMENT {i} ---"
        )
    prompt = f"{SYSTEM}\n\n" + "\n\n".join(parts)

    # try each available key
    while True:
        slot = _get_client()
        if slot is None:
            log.error("All %d Gemini key(s) exhausted for today. "
                      "Resets at midnight PT.", len(_keys))
            return None

        idx, client = slot
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            raw = resp.text
            break   # success — exit retry loop

        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
                _mark_exhausted(idx)
                continue   # try next key
            elif "API_KEY" in msg or "401" in msg:
                log.error("Gemini key #%d auth error — check GEMINI_API_KEY_%s: %s",
                          idx + 1, "" if idx == 0 else idx + 1, e)
                _mark_exhausted(idx)
                continue
            else:
                log.error("Gemini call failed: %s", e)
                return None

    try:
        clean   = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        payload = json.loads(clean)
    except Exception as e:
        log.warning("JSON parse failed for batch of %d pages: %s", len(valid), e)
        return []

    by_url = {p["source_url"]: p for p in valid}
    out = []
    for c in payload.get("cards") or []:
        src_url = c.get("source_url") or valid[0]["source_url"]
        page    = by_url.get(src_url, valid[0])
        c.setdefault("issuer_id",   page.get("issuer_id"))
        c.setdefault("issuer_name", page.get("issuer_name"))
        c["source_url"] = src_url
        errs = sorted(VALIDATOR.iter_errors(c), key=lambda e: e.path)
        if errs:
            log.info("schema warnings for %s: %s",
                     c.get("card_id"), [e.message for e in errs[:3]])
        out.append(c)

    log.info("extracted %d card(s) from %d page(s) using key #%d",
             len(out), len(valid), idx + 1)
    return out