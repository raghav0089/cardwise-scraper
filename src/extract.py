"""LLM-based extractor — takes a batch of scraped pages and returns
normalized card records matching schema/card.schema.json.

Provider: Google Gemini (free tier).
  → Get key: https://aistudio.google.com  →  set GEMINI_API_KEY

Model choice (set via LLM_MODEL env var):
  gemini-2.0-flash-lite  →  1,500 req/MIN free  ← default, use this
  gemini-2.0-flash       →  15 req/MIN free (1,500/day)
  gemini-1.5-flash-8b    →  alternative free option
"""
from __future__ import annotations
import os, json, logging
from pathlib import Path
from jsonschema import Draft7Validator
from google import genai

log = logging.getLogger(__name__)
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)

# gemini-2.0-flash-lite: 1,500 req/min free vs gemini-2.0-flash: 15 req/min / 1,500 req/day
MODEL = os.getenv("LLM_MODEL", "gemini-2.0-flash-lite")

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

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def extract_cards(batch: list[dict]) -> list[dict] | None:
    """Extract card records from a batch of pages in one Gemini call.

    Each element of `batch` must have:
        source_url  : str
        markdown    : str
        issuer_id   : str | None
        issuer_name : str | None

    Returns:
        list[dict]  — extracted cards (may be empty)
        None        — API error; caller should NOT mark sources as seen
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

    try:
        resp = _get_client().models.generate_content(
            model=MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        raw = resp.text
    except Exception as e:
        _log_error(valid[0]["source_url"], e)
        return None     # signals caller: don't cache, retry tomorrow

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
    return out


def _log_error(label: str, exc: Exception) -> None:
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        log.error(
            "Gemini quota hit. If using gemini-2.0-flash, switch to "
            "gemini-2.0-flash-lite (LLM_MODEL=gemini-2.0-flash-lite) — "
            "it allows 1,500 req/MIN free vs 1,500/day. Skipping: %s", label,
        )
    elif "API_KEY" in msg or "401" in msg:
        log.error("Gemini auth error — check GEMINI_API_KEY. Skipping %s", label)
    else:
        log.error("Gemini call failed for %s: %s", label, exc)