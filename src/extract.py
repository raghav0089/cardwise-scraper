"""LLM-based card extractor.

Primary provider: Groq (llama-3.3-70b-versatile) — free, 14 400 RPD.
Optional fallback: Gemini (if GEMINI_API_KEY* are set).

Groq key rotation: GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3
Gemini key rotation: GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3

Returns None only if ALL providers are exhausted — caller should not mark
the source as seen so it retries on the next daily run.
"""
from __future__ import annotations
import os, json, logging, time
from pathlib import Path
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)

SYSTEM = """You extract Indian payment-card product details from web pages.

You will receive one or more pages in --- DOCUMENT --- blocks.
Return STRICT JSON: {"cards":[<card>, ...]} — a flat list.
Each card must match the IndianCard schema.

══════════════════════════════════════════════════════
CRITICAL RULES — read before extracting anything
══════════════════════════════════════════════════════
1. Only emit records for actual PAYMENT CARD PRODUCTS.
   A card product has a proper name like "Millennia Credit Card", "ACE Credit Card",
   "Regalia Gold Credit Card", "IndianOil HDFC Bank Credit Card", etc.

2. NEVER emit records for any of the following — even if they appear in the markdown:
   • Page sections / headings ("Additional Benefits", "Key Features", "Why Choose Us")
   • Savings / rewards calculators ("Swiggy Credit Card Savings Calculator")
   • Activation / onboarding prompts ("Have you activated your card?")
   • Promotional announcements ("We are happy to introduce the new…")
   • News about existing cardholders ("Existing cardholders will continue to enjoy…")
   • Cross-sell or comparison sidebars
   • FAQ entries, T&C references, eligibility calculators
   • Error / not-found pages ("Page Not Found", 404 error)
   • Navigation items, footer links, tab headers

3. DETAIL PAGE (PAGE_TYPE says DETAIL PAGE):
   • The page is dedicated to ONE specific card product.
   • Emit EXACTLY ONE record — for the primary card this page is about.
   • Look at the main heading (h1 / largest heading) to identify the card name.
   • Do NOT extract other cards mentioned in cross-sell sections, "You may also like",
     comparison tables, or "upgrade to" suggestions anywhere on the page.

4. LISTING PAGE (PAGE_TYPE says LISTING PAGE):
   • The page lists several card products — emit one record per DISTINCT named product.
   • Use only brief data visible on the listing (name, fees, key features shown).
   • Do NOT invent details not present on the listing page.

5. card_name = short official product name ONLY:
   ✓  "Millennia Credit Card"
   ✓  "ACE Credit Card"
   ✓  "Regalia Gold Credit Card"
   ✗  "ACE Credit Card: 5% Cashback on Bills, Lounge Access & Dining Offers | Axis Bank"
   ✗  "5% Cashback On Amazon, Flipkart, Myntra, Swiggy, Zomato And More"
   ✗  "Best Travel Credit Card 2024 | HDFC Bank"
   ✗  "Have You Activated Your New SBI Credit Card?"

6. card_id = "<issuer_id>__<slug-of-card_name>" (lowercase, hyphens only).
   Use ISSUER_ID_HINT from the document block as issuer_id.

7. Always set source_url to the exact SOURCE_URL from the document block.

8. category must be one of: credit, debit, prepaid, forex, corporate, business, other.

9. If a page is an error page (404 / "Page Not Found" / access denied), return {"cards": []}.

10. Return ONLY the JSON object — no markdown fences, no preamble, no explanation.

══════════════════════════════════════════════════════
FEES  (null if not stated — do not guess)
══════════════════════════════════════════════════════
All INR amounts as plain numbers (12500, not "₹12,500").
Set fees.gst_extra=true if "+GST" / "exclusive of GST" appears.
Capture: joining_fee_inr, annual_fee_inr, renewal_fee_inr, fee_waiver_spend_inr,
  addon_card_fee_inr, fx_markup_pct, cash_advance_fee_pct,
  finance_charge_pct_mo (monthly interest rate), fuel_surcharge_waiver (text).

══════════════════════════════════════════════════════
REWARDS  (be THOROUGH — every category mentioned)
══════════════════════════════════════════════════════
base_rate_pct: the default earn rate as % of spend.
  Examples: "1 RP per ₹150, 1 RP = ₹0.25" → 0.25/150*100 = 0.1667
            "5% cashback on all spends" → 5.0
For EVERY accelerated category add to rewards.accelerated:
  {category, rate_pct, cap_inr (monthly/annual cap if stated), notes}
  Categories: dining, travel, grocery, fuel, online, international,
  entertainment, UPI, utilities, insurance, movies, hotels, etc.
point_value_inr: INR value of 1 reward point / mile / coin.
currency: exact name (Reward Points / EDGE Miles / Cashback / NeuCoins / etc.)
redemption_modes: all ways to redeem (statement credit, flights, Amazon Pay, etc.)
expiry_months: months until points expire.
exclusions: spend categories that earn NO reward.

══════════════════════════════════════════════════════
MILESTONES  (every spend tier)
══════════════════════════════════════════════════════
{spend_inr, reward (exact text), value_inr, period: monthly/quarterly/annual/anniversary}
Examples: fee waiver at ₹1L spend, 5000 bonus points at ₹2L quarterly,
          free flight at ₹4L annual, ₹500 voucher at ₹50K monthly.

══════════════════════════════════════════════════════
WELCOME BENEFIT
══════════════════════════════════════════════════════
welcome_benefit: everything given on joining/first-use — points, vouchers,
  subscriptions, gifts. Be specific with quantities and conditions.

══════════════════════════════════════════════════════
PARTNER OFFERS
══════════════════════════════════════════════════════
For EVERY named brand offer: {partner, benefit}
benefit = full detail: "10% cashback up to ₹150/order, 3 orders/month, min ₹149"
Look for: Swiggy, Zomato, Amazon, Flipkart, BookMyShow, Myntra, Ola, Uber,
  IRCTC, MakeMyTrip, Cleartrip, Nykaa, BigBasket, Blinkit, PhonePe, Paytm,
  Tata Neu, CRED, Cult.fit, Lenskart, PVR, INOX, and any other named brand.
partner_offers value must be the EXACT offer text, NOT raw markdown or navigation links.

══════════════════════════════════════════════════════
LOUNGE ACCESS
══════════════════════════════════════════════════════
domestic_visits_year: total per year (multiply per-quarter × 4, per-month × 12).
international_visits_year: total per year.
guest_allowed: true/false.
program: Priority Pass / DreamFolks / LoungeKey / own network.
spend_unlock_inr: minimum spend per quarter/month to activate lounge benefit.

══════════════════════════════════════════════════════
INSURANCE
══════════════════════════════════════════════════════
air_accident_inr, lost_card_inr, purchase_protection_inr, travel_inr.

══════════════════════════════════════════════════════
PERKS  (catch-all — everything not captured above)
══════════════════════════════════════════════════════
Add {kind, value} for each benefit not covered above.
value must be a clean human-readable sentence — NOT raw markdown, URLs, or navigation text.
Kind examples: golf, golf_simulator, movie, dining, hotel, concierge, spa,
  railway_lounge, subscription, roadside_assist, forex_perk,
  emi_conversion, contactless, virtual_card, annual_voucher.

══════════════════════════════════════════════════════
ELIGIBILITY
══════════════════════════════════════════════════════
min_age, max_age, min_income_inr_year, min_credit_score, salaried, self_employed.

══════════════════════════════════════════════════════
OTHER
══════════════════════════════════════════════════════
apply_url: direct application link if present.
card_material: metal / plastic / virtual (only if explicitly stated).
network: Visa / Mastercard / RuPay / Amex / Diners (if stated).
segment: entry / mid / premium / super-premium / invite-only / student / secured / nri."""


# ── Groq (primary) ────────────────────────────────────────────────────────────

GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_RPM     = int(os.getenv("GROQ_RPM", "25"))   # conservative under 30 RPM free limit
_GROQ_MIN_IV = 60.0 / GROQ_RPM

_groq_keys:        list[str]        = []
_groq_idx:         int              = 0
_groq_exhausted:   set[int]         = set()
_groq_last_call:   dict[int, float] = {}


def _load_groq_keys() -> list[str]:
    keys = []
    for var in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    return keys


def _groq_wait(idx: int) -> None:
    last = _groq_last_call.get(idx, 0.0)
    gap  = time.monotonic() - last
    if gap < _GROQ_MIN_IV:
        time.sleep(_GROQ_MIN_IV - gap)
    _groq_last_call[idx] = time.monotonic()


def _call_groq(system: str, user: str) -> str | None:
    """Try each Groq key in rotation. Returns raw JSON string or None."""
    global _groq_keys, _groq_idx

    if not _groq_keys:
        _groq_keys = _load_groq_keys()
    if not _groq_keys:
        log.warning("No GROQ_API_KEY configured")
        return None

    from groq import Groq

    for _ in range(len(_groq_keys)):
        if _groq_idx in _groq_exhausted:
            _groq_idx = (_groq_idx + 1) % len(_groq_keys)
            continue
        idx = _groq_idx
        try:
            _groq_wait(idx)
            client = Groq(api_key=_groq_keys[idx])
            resp   = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            return resp.choices[0].message.content
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower() or "quota" in msg.lower():
                remaining = len(_groq_keys) - len(_groq_exhausted) - 1
                log.warning("Groq key #%d exhausted. %d key(s) remaining.", idx + 1, remaining)
                _groq_exhausted.add(idx)
                _groq_idx = (_groq_idx + 1) % len(_groq_keys)
                continue
            log.error("Groq call failed: %s", e)
            return None

    log.error("All Groq key(s) exhausted.")
    return None


# ── Gemini (optional fallback) ────────────────────────────────────────────────

GEMINI_MODEL  = os.getenv("LLM_MODEL", "gemini-2.0-flash-lite")
GEMINI_RPM    = int(os.getenv("GEMINI_RPM", "25"))
_GEM_MIN_IV   = 60.0 / GEMINI_RPM

_gem_keys:       list[str]        = []
_gem_idx:        int              = 0
_gem_exhausted:  set[int]         = set()
_gem_clients:    dict             = {}
_gem_last_call:  dict[int, float] = {}


def _load_gemini_keys() -> list[str]:
    keys = []
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    return keys


def _gem_wait(idx: int) -> None:
    last = _gem_last_call.get(idx, 0.0)
    gap  = time.monotonic() - last
    if gap < _GEM_MIN_IV:
        time.sleep(_GEM_MIN_IV - gap)
    _gem_last_call[idx] = time.monotonic()


def _call_gemini(prompt: str) -> str | None:
    """Try each Gemini key in rotation. Returns raw JSON string or None."""
    global _gem_keys, _gem_idx

    if not _gem_keys:
        _gem_keys = _load_gemini_keys()
    if not _gem_keys:
        return None   # no Gemini keys configured — silent skip

    try:
        from google import genai
    except ImportError:
        return None

    for _ in range(len(_gem_keys)):
        if _gem_idx in _gem_exhausted:
            _gem_idx = (_gem_idx + 1) % len(_gem_keys)
            continue
        idx = _gem_idx
        if idx not in _gem_clients:
            _gem_clients[idx] = genai.Client(api_key=_gem_keys[idx])
        try:
            _gem_wait(idx)
            resp = _gem_clients[idx].models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return resp.text
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
                remaining = len(_gem_keys) - len(_gem_exhausted) - 1
                log.warning("Gemini key #%d exhausted. %d key(s) remaining.", idx + 1, remaining)
                _gem_exhausted.add(idx)
                _gem_idx = (_gem_idx + 1) % len(_gem_keys)
                continue
            elif "API_KEY" in msg or "401" in msg:
                log.error("Gemini key #%d auth error: %s", idx + 1, e)
                _gem_exhausted.add(idx)
                _gem_idx = (_gem_idx + 1) % len(_gem_keys)
                continue
            log.error("Gemini call failed: %s", e)
            return None

    log.warning("All Gemini key(s) exhausted.")
    return None


# ── Public entry point ────────────────────────────────────────────────────────

def extract_cards(batch: list[dict]) -> list[dict] | None:
    """Extract card records from a page via Groq (primary) or Gemini (fallback).

    Returns:
        list[dict]  — extracted cards (may be empty if page has no card data)
        None        — all providers exhausted; do NOT mark source as seen
    """
    valid = [p for p in batch if p.get("markdown") and len(p["markdown"]) >= 200]
    if not valid:
        return []

    parts = []
    for i, p in enumerate(valid, 1):
        is_listing = p.get("is_listing", False)
        hint = "LISTING PAGE — extract ALL individual card products mentioned." \
               if is_listing else "DETAIL PAGE — extract the single card described."
        # Listing pages show 20+ cards; give them more room.
        window = 16000 if is_listing else 8000
        parts.append(
            f"--- DOCUMENT {i} ---\n"
            f"SOURCE_URL: {p['source_url']}\n"
            f"ISSUER_ID_HINT: {p.get('issuer_id') or ''}\n"
            f"ISSUER_NAME_HINT: {p.get('issuer_name') or ''}\n"
            f"PAGE_TYPE: {hint}\n\n"
            f"{p['markdown'][:window]}\n"
            f"--- END DOCUMENT {i} ---"
        )
    user_content = "\n\n".join(parts)
    provider     = "unknown"

    # 1. Groq — primary
    raw = _call_groq(SYSTEM, user_content)
    if raw is not None:
        provider = "groq"
    else:
        # 2. Gemini — optional fallback
        raw = _call_gemini(f"{SYSTEM}\n\n{user_content}")
        if raw is not None:
            provider = "gemini"
        else:
            return None   # all providers exhausted

    try:
        clean   = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        payload = json.loads(clean)
    except Exception as e:
        log.warning("JSON parse failed (%s): %s  raw=%s", provider, e, raw[:200])
        return []

    by_url = {p["source_url"]: p for p in valid}
    out    = []
    for c in payload.get("cards") or []:
        src_url = c.get("source_url") or valid[0]["source_url"]
        page    = by_url.get(src_url, valid[0])
        c.setdefault("issuer_id",   page.get("issuer_id"))
        c.setdefault("issuer_name", page.get("issuer_name"))
        c["source_url"] = src_url
        errs = sorted(VALIDATOR.iter_errors(c), key=lambda e: e.path)
        if errs:
            log.debug("schema warnings for %s: %s",
                      c.get("card_id"), [e.message for e in errs[:3]])
        out.append(c)

    log.info("extracted %d card(s) from %d page(s) via %s", len(out), len(valid), provider)
    return out
