"""LLM-based extractor — takes scraped markdown + page URL and returns
zero, one, or many normalized card records matching schema/card.schema.json.

Uses OpenAI structured output (function calling) for reliability. A page
may describe multiple cards (e.g. a listing page) — the schema is a list.
"""
from __future__ import annotations
import os, json, logging
from pathlib import Path
from openai import OpenAI
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)
SCHEMA = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
_client = None
def _oai():
    global _client
    if _client is None: _client = OpenAI()
    return _client

SYSTEM = """You extract Indian payment-card product details from web pages.

Return STRICT JSON: {"cards":[<card>, ...]} where each card matches the
IndianCard schema. RULES:

- If the page is not about a payment card (credit/debit/prepaid/forex/corporate),
  return {"cards": []}.
- A single page may describe many cards — emit one record per distinct card.
- Use INR numeric values (e.g. 12500, not "₹12,500"). Strip GST language
  but set fees.gst_extra=true if "+GST" / "exclusive of GST" appears.
- Convert reward-rate phrases to base_rate_pct (1 RP per ₹150 with RP=₹0.25
  ⇒ base_rate_pct = 0.25/150*100 = 0.167).
- Be conservative: leave a field null rather than guess. Set "confidence"
  between 0 and 1 reflecting how complete the page was.
- card_id = "<issuer_id>__<slug-of-card_name>".
"""

def extract_cards(markdown: str, *, source_url: str, issuer_id: str | None,
                  issuer_name: str | None) -> list[dict]:
    if not markdown or len(markdown) < 200:
        return []
    user = (f"SOURCE_URL: {source_url}\n"
            f"ISSUER_ID_HINT: {issuer_id or ''}\n"
            f"ISSUER_NAME_HINT: {issuer_name or ''}\n\n"
            f"PAGE_MARKDOWN (truncated to 25k chars):\n{markdown[:25000]}")
    resp = _oai().chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": user}],
    )
    try:
        payload = json.loads(resp.choices[0].message.content)
    except Exception as e:
        log.warning("LLM JSON parse failed for %s: %s", source_url, e)
        return []

    cards = payload.get("cards") or []
    out = []
    for c in cards:
        c.setdefault("issuer_id", issuer_id)
        c.setdefault("issuer_name", issuer_name)
        c["source_url"] = source_url
        errs = sorted(VALIDATOR.iter_errors(c), key=lambda e: e.path)
        if errs:
            log.info("schema fix-ups for %s: %s", c.get("card_id"), [e.message for e in errs[:3]])
            # keep going; downstream normalizer enforces required keys
        out.append(c)
    return out
