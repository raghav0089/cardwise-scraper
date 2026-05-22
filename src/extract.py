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
from google import genai

log = logging.getLogger(__name__)

def _sanitize_schema_for_gemini(schema: dict) -> dict:
    """Recursively converts standard JSON Schema (Draft-07) layouts to plain single-type
    definitions that the Google GenAI SDK can parse without Pydantic verification failures.
    """
    if not isinstance(schema, dict):
        return schema

    res = {}
    for k, v in schema.items():
        # Rule 1: Strip meta-schema validator flags
        if k == "$schema":
            continue

        # Rule 2: Transform validation arrays like ["string", "null"] or ["number", "null"]
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            if non_null:
                res[k] = non_null[0].upper()
            else:
                res[k] = "STRING"
            continue

        # Rule 3: Ensure basic types match strict uppercase enum expectations
        if k == "type" and isinstance(v, str):
            res[k] = v.upper()
            continue

        # Rule 4: Strip empty or Null options out of strict enum constraint paths
        if k == "enum" and isinstance(v, list):
            clean_enums = [str(x) for x in v if x is not None and x != ""]
            if clean_enums:
                res[k] = clean_enums
            continue

        if isinstance(v, dict):
            res[k] = _sanitize_schema_for_gemini(v)
        elif isinstance(v, list):
            res[k] = [_sanitize_schema_for_gemini(x) if isinstance(x, dict) else x for x in v]
        else:
            res[k] = v
    return res

# Read and validate using master rules downstream, but build a clean runtime view for Gemini
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
GEMINI_COMPATIBLE_SCHEMA = _sanitize_schema_for_gemini(SCHEMA)

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
- Always populate the "source_url" field inside each card record with the exact matching URL from its corresponding document header section.
- Return ONLY the JSON object. No markdown fences, no preamble."""

_client = None

def _model():
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"]
        )
    return _client


def extract_cards(batch: list[dict]) -> list[dict] | None:
    """Extracts payment card items from a batch of markdown structures simultaneously.
    
    Returns None if an API error or validation issue occurs, allowing the orchestrator
    to avoid caching the source and handle automated daily retries.
    """
    if not batch:
        return []

    batch_contents = []
    for idx, row in enumerate(batch):
        md = row.get("markdown") or ""
        batch_contents.append(
            f"--- DOCUMENT MATCH INDEX: {idx} ---\n"
            f"SOURCE_URL: {row['source_url']}\n"
            f"ISSUER_ID_HINT: {row.get('issuer_id') or ''}\n"
            f"ISSUER_NAME_HINT: {row.get('issuer_name') or ''}\n\n"
            f"PAGE_MARKDOWN (truncated):\n{md[:15000]}\n"
            f"--- END DOCUMENT INDEX {idx} ---\n"
        )
    
    user_msg = "\n".join(batch_contents)

    try:
        resp = _model().models.generate_content(
            model=MODEL,
            contents=f"{SYSTEM}\n\n{user_msg}",
            config={
                "response_mime_type": "application/json",
                "response_schema": GEMINI_COMPATIBLE_SCHEMA,
            }
        )
        raw = resp.text
    except Exception as e:
        _log_error(batch[0]["source_url"] if batch else "unknown_batch", e)
        return None

    try:
        payload = json.loads(raw.strip())
    except Exception as e:
        log.warning("JSON parse failed on aggregated batch payload: %s", e)
        return None

    cards = payload.get("cards") or []
    out   = []
    for c in cards:
        url_ref = c.get("source_url") or (batch[0]["source_url"] if batch else "")
        matched_row = next((r for r in batch if r["source_url"] == url_ref), batch[0] if batch else {})

        c.setdefault("issuer_id",   matched_row.get("issuer_id"))
        c.setdefault("issuer_name", matched_row.get("issuer_name"))
        c["source_url"] = url_ref
        
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
            "Resets at midnight PT — or upgrade at [https://aistudio.google.com](https://aistudio.google.com)  "
            "Skipping %s", source_url,
        )
    elif "API_KEY" in msg or "401" in msg:
        log.error("Gemini auth error — check GEMINI_API_KEY. Skipping %s", source_url)
    else:
        log.error("Gemini call failed for %s: %s", source_url, exc)