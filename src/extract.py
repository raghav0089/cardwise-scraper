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
import os, json, logging, time
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
Each card must match the IndianCard schema.

──────────────────────────────────────────────────────
GENERAL
──────────────────────────────────────────────────────
- Skip pages not about a specific payment-card product (credit/debit/prepaid/forex/corporate).
- One page may describe many cards — emit one record per distinct card.
- card_id = "<issuer_id>__<slug-of-card_name>" (lowercase, hyphens).
- Always set "source_url" to the exact SOURCE_URL from the document block.
- Return ONLY the JSON object — no markdown fences, no preamble.

──────────────────────────────────────────────────────
FEES  (be accurate; leave null if not stated)
──────────────────────────────────────────────────────
- All INR amounts as plain numbers (12500, not "₹12,500").
- Set fees.gst_extra=true if "+GST" / "exclusive of GST" appears.
- Capture: joining_fee_inr, annual_fee_inr, renewal_fee_inr, fee_waiver_spend_inr,
  addon_card_fee_inr, fx_markup_pct, cash_advance_fee_pct,
  finance_charge_pct_mo (monthly interest), fuel_surcharge_waiver.

──────────────────────────────────────────────────────
REWARDS  (be THOROUGH — capture every category mentioned)
──────────────────────────────────────────────────────
- Convert reward phrases → base_rate_pct:
    "1 RP per ₹150, 1 RP = ₹0.25"  → 0.25/150*100 = 0.167
    "5% cashback"                   → 5.0
    "2X points on all spends"       → multiply base by 2
- For EVERY accelerated category (dining, travel, grocery, fuel, online, international,
  entertainment, UPI, utilities, insurance, etc.) add an entry to rewards.accelerated:
  {category, rate_pct, cap_inr (monthly/annual cap if stated), notes}.
- List ALL redemption modes (statement credit, flights, hotels, Amazon Pay,
  vouchers, merchandise, miles transfer, etc.) in rewards.redemption_modes.
- Capture: currency (points/miles/cashback/neucoins/…), point_value_inr,
  expiry_months, exclusions (categories that earn no reward).

──────────────────────────────────────────────────────
MILESTONES  (capture every tier)
──────────────────────────────────────────────────────
- For each spend threshold that unlocks a reward, add a milestone:
  {spend_inr, reward (full text), value_inr, period (monthly/quarterly/annual/anniversary)}.
- Examples: fee waiver on ₹1L spend, 5000 bonus points at ₹2L quarterly spend,
  free flight voucher on ₹4L annual spend.

──────────────────────────────────────────────────────
WELCOME BENEFIT
──────────────────────────────────────────────────────
- welcome_benefit: a single string listing EVERYTHING given on joining/activation —
  bonus points, vouchers, gift cards, cashback, subscriptions, merchandise.
  Use the exact text from the page; do not truncate.

──────────────────────────────────────────────────────
PARTNER OFFERS  (brand-specific cashback / discounts)
──────────────────────────────────────────────────────
- For EVERY named brand offer, add {partner, benefit} to partner_offers.
- Partners to look for: Swiggy, Zomato, Amazon, Flipkart, BookMyShow, Myntra,
  Ola, Uber, IRCTC, MakeMyTrip, Cleartrip, Nykaa, BigBasket, Blinkit, PhonePe,
  Paytm, Google Pay, Tata Neu, Cred, Cult.fit, Lenskart, and any other named brand.
- benefit must include: cashback %, max cashback INR, min spend INR (if stated),
  how many times per month/quarter, and any other conditions.
  Example: "10% cashback up to ₹100 per order, max 3 orders/month, min spend ₹149"

──────────────────────────────────────────────────────
LOUNGE ACCESS
──────────────────────────────────────────────────────
- domestic_visits_year, international_visits_year (total per year or per quarter×4).
- guest_allowed (true/false), program (Priority Pass / DreamFolks / LoungeKey / own).
- spend_unlock_inr: minimum quarterly/monthly spend to activate lounge benefit.

──────────────────────────────────────────────────────
INSURANCE
──────────────────────────────────────────────────────
- air_accident_inr, lost_card_inr, purchase_protection_inr, travel_inr.

──────────────────────────────────────────────────────
PERKS  (catch-all — be THOROUGH, use exact page wording)
──────────────────────────────────────────────────────
For every benefit not captured above, add {kind, value} to perks.
Kinds to look for (not exhaustive):
  golf            – complimentary rounds, green-fee waivers
  movie           – free tickets/month, discount at PVR/INOX/BookMyShow
  dining          – discount % at partner restaurants, complimentary meals
  hotel           – complimentary nights, upgrade, late checkout
  concierge       – 24/7 concierge, travel desk
  spa             – complimentary treatments, discount at partner spas
  railway_lounge  – domestic railway lounge visits
  subscription    – free/discounted OTT, Zomato Gold, Amazon Prime, etc.
  roadside_assist – roadside breakdown assistance
  golf_simulator  – indoor golf simulator access
  forex_card_perk – zero cross-currency markup, emergency cash abroad
  emi_conversion  – no-cost EMI, instant EMI on large purchases
  contactless     – tap-to-pay, UPI linkage
  virtual_card    – instant virtual card on approval
  annual_voucher  – yearly lifestyle/travel/shopping voucher

──────────────────────────────────────────────────────
ELIGIBILITY
──────────────────────────────────────────────────────
- min_age, max_age, min_income_inr_year, min_credit_score, salaried, self_employed.

──────────────────────────────────────────────────────
OTHER FIELDS
──────────────────────────────────────────────────────
- apply_url: the direct application URL if present on the page.
- card_material: "metal" / "plastic" / "virtual" if explicitly stated.
- confidence: 0–1 reflecting how complete the extracted data is.
- Be CONSERVATIVE about fees (null if unsure).
  Be COMPREHENSIVE about benefits (capture everything mentioned)."""


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
_last_call:      dict[int, float] = {}  # per-key last-call monotonic timestamp

GEMINI_RPM       = int(os.getenv("GEMINI_RPM", "25"))   # conservative under 30 free-tier limit
_MIN_INTERVAL    = 60.0 / GEMINI_RPM


def _key_wait(idx: int) -> None:
    """Enforce per-key rate limit before making a call with key `idx`."""
    last = _last_call.get(idx, 0.0)
    gap  = time.monotonic() - last
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call[idx] = time.monotonic()


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


# ── Groq fallback ─────────────────────────────────────────────────────────────

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

def _try_groq(prompt: str) -> str | None:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content
    except Exception as e:
        log.error("Groq call failed: %s", e)
        return None


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
            f"{p['markdown'][:6000]}\n"
            f"--- END DOCUMENT {i} ---"
        )
    prompt = f"{SYSTEM}\n\n" + "\n\n".join(parts)

    # try each available Gemini key, then fall back to Groq
    idx = -1
    while True:
        slot = _get_client()
        if slot is None:
            log.warning("All Gemini key(s) exhausted — trying Groq fallback")
            raw = _try_groq(prompt)
            if raw is None:
                log.error("All LLM providers exhausted.")
                return None
            break

        idx, client = slot
        try:
            _key_wait(idx)
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

    provider = "groq" if idx == -1 else f"gemini key #{idx + 1}"
    log.info("extracted %d card(s) from %d page(s) via %s", len(out), len(valid), provider)
    return out