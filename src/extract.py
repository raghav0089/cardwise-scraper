"""LLM-based extractor — takes scraped markdown + page URL and returns
zero, one, or many normalized card records matching schema/card.schema.json.

Provider: Google Gemini Flash (free tier: 1,500 req/day, no CC needed).
  → Get key: https://aistudio.google.com  →  set GEMINI_API_KEY

A page may describe multiple cards (e.g. a listing page) — the schema is a list.
"""
from __future__ import annotations
import os, json, logging
from pathlib import Path
from jsonschema import Draft7Validator
import google.generativeai as genai

log = logging.getLogger(__name__)
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
MODEL     = os.getenv("LLM_MODEL", "gemini-2.0-flash")

SYSTEM = """You extract Indian payment-card product details from web pages.

Return STRICT JSON: {"cards":[<card>, ...]} where each card matches the
IndianCard schema. RULES:

- If the page is not about a payment card (credit/debit/prepaid/forex/corporate),
  return {"cards": []}.
- A single page may describe many cards — emit one record per distinct card.
- Use INR numeric values (e.g. 12500, not "₹12,500"). Strip GST language
  but set fees.gst_extra=true if "+GST" / "exclusive of GST" appears.
- Convert reward-rate phrases to base_rate_pct (1 RP per ₹150 with RP=₹0.25
  => base_rate_pct = 0.25/150*100 = 0.167).
- Be conservative: leave a field null rather than guess. Set "confidence"
  between 0 and 1 reflecting how complete the page was.
- card_id = "<issuer_id>__<slug-of-card_name>".
- Return ONLY the JSON object. No markdown fences, no preamble."""

_client = None

def _model():
    global _client
    if _client is None:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        _client = genai.GenerativeModel(
            model_name=MODEL,
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
            system_instruction=SYSTEM,
        )
    return _client


def extract_cards(
    markdown: str,
    *,
    source_url: str,
    issuer_id: str | None,
    issuer_name: str | None,
) -> list[dict]:
    if not markdown or len(markdown) < 200:
        return []

    user_msg = (
        f"SOURCE_URL: {source_url}\n"
        f"ISSUER_ID_HINT: {issuer_id or ''}\n"
        f"ISSUER_NAME_HINT: {issuer_name or ''}\n\n"
        f"PAGE_MARKDOWN (truncated to 25k chars):\n{markdown[:25000]}"
    )

    try:
        resp = _model().generate_content(user_msg)
        raw  = resp.text
    except Exception as e:
        _log_error(source_url, e)
        return []

    try:
        clean   = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        payload = json.loads(clean)
    except Exception as e:
        log.warning("JSON parse failed for %s: %s", source_url, e)
        return []

    cards = payload.get("cards") or []
    out   = []
    for c in cards:
        c.setdefault("issuer_id",   issuer_id)
        c.setdefault("issuer_name", issuer_name)
        c["source_url"] = source_url
        errs = sorted(VALIDATOR.iter_errors(c), key=lambda e: e.path)
        if errs:
            log.info("schema fix-ups for %s: %s",
                     c.get("card_id"), [e.message for e in errs[:3]])
        out.append(c)
    return out


def _log_error(source_url: str, exc: Exception) -> None:
    msg = str(exc)
    if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        log.error(
            "Gemini free-tier daily quota hit (1,500 req/day). "
            "Resets at midnight PT — or upgrade at https://aistudio.google.com  "
            "Skipping %s", source_url,
        )
    elif "API_KEY" in msg or "401" in msg:
        log.error("Gemini auth error — check GEMINI_API_KEY. Skipping %s", source_url)
    else:
        log.error("Gemini call failed for %s: %s", source_url, exc)