"""LLM-based card extractor.

Provider chain (first available wins):
  0. Ollama — local, zero rate limits (set OLLAMA_BASE_URL or just run `ollama serve`)
  1. Gemini — free tier, gemini-2.0-flash-lite (1M TPM, 30 RPM), key rotation
  2. Groq   — free tier, llama-3.1-8b-instant (20K TPM, 30 RPM), key rotation
  3. OpenAI — paid fallback, gpt-4o-mini, used when all other providers exhausted

Key env vars:
  GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3
  GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3
  OPENAI_API_KEY

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

# llama-3.1-8b-instant: 20,000 TPM on Groq free tier (vs 6,000 TPM for 70b).
# Set GROQ_MODEL=llama-3.3-70b-versatile in env to use the larger model (needs paid key).
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_RPM     = int(os.getenv("GROQ_RPM", "28"))   # free tier allows 30 RPM
_GROQ_MIN_IV = 60.0 / GROQ_RPM

_groq_keys:        list[str]        = []
_groq_idx:         int              = 0
_groq_exhausted:   set[int]         = set()   # keys that hit DAILY quota — skip for entire run
_groq_last_call:   dict[int, float] = {}
_groq_cooldown:    dict[int, float] = {}       # keys in per-minute cooldown — retry after timestamp


def _load_groq_keys() -> list[str]:
    keys = []
    for var in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    return keys


def _groq_wait(idx: int) -> None:
    # Respect per-minute cooldown from a previous rate-limit hit
    cooldown_until = _groq_cooldown.get(idx, 0.0)
    now = time.monotonic()
    if now < cooldown_until:
        wait_sec = cooldown_until - now
        log.info("Groq key #%d in cooldown — waiting %.0fs", idx + 1, wait_sec)
        time.sleep(wait_sec)
    # Enforce minimum interval between calls
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

    now = time.monotonic()
    available = [i for i in range(len(_groq_keys))
                 if i not in _groq_exhausted and now >= _groq_cooldown.get(i, 0.0)]

    if not available:
        # All keys in cooldown — don't wait, fall through to Gemini immediately.
        # Groq will be available again for the next card (cooldowns expire independently).
        log.info("Groq: all key(s) in rate-limit cooldown — handing off to Gemini")
        return None

    for idx in available:
        try:
            _groq_wait(idx)
            client = Groq(api_key=_groq_keys[idx], max_retries=1)
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
            if "413" in msg or "payload too large" in msg.lower() or "request entity too large" in msg.lower():
                log.warning("Groq key #%d: payload too large (%d chars) — skipping to next provider", idx + 1, len(user))
                return None   # fall through to Gemini with full content
            if "429" in msg or "rate_limit" in msg.lower() or "quota" in msg.lower():
                is_daily = any(w in msg.lower() for w in ("per_day", "daily", "day_limit"))
                if is_daily:
                    log.warning("Groq key #%d daily quota exhausted — skipping for this run", idx + 1)
                    _groq_exhausted.add(idx)
                else:
                    log.warning("Groq key #%d rate-limited — 65s cooldown", idx + 1)
                    _groq_cooldown[idx] = time.monotonic() + 65
                continue
            log.error("Groq call failed: %s", e)
            return None

    log.info("Groq: all available key(s) rate-limited — handing off to next provider")
    return None


# ── Gemini (optional fallback) ────────────────────────────────────────────────

GEMINI_MODEL  = os.getenv("LLM_MODEL", "gemini-2.0-flash-lite")
GEMINI_RPM    = int(os.getenv("GEMINI_RPM", "15"))
_GEM_MIN_IV   = 60.0 / GEMINI_RPM

_gem_keys:       list[str]        = []
_gem_idx:        int              = 0
_gem_exhausted:  set[int]         = set()
_gem_clients:    dict             = {}
_gem_last_call:  dict[int, float] = {}
_gem_cooldown:   dict[int, float] = {}


def _load_gemini_keys() -> list[str]:
    keys = []
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    return keys


def _gem_wait(idx: int) -> None:
    cooldown_until = _gem_cooldown.get(idx, 0.0)
    now = time.monotonic()
    if now < cooldown_until:
        wait_sec = cooldown_until - now
        log.info("Gemini key #%d in cooldown — waiting %.0fs", idx + 1, wait_sec)
        time.sleep(wait_sec)
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
        return None

    try:
        from google import genai
    except ImportError:
        return None

    now = time.monotonic()
    available_gem = [i for i in range(len(_gem_keys))
                     if i not in _gem_exhausted and now >= _gem_cooldown.get(i, 0.0)]
    if not available_gem:
        log.warning("Gemini: all key(s) in cooldown or exhausted")
        return None

    for idx in available_gem:
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
                log.warning("Gemini key #%d rate-limited — 65s cooldown", idx + 1)
                _gem_cooldown[idx] = time.monotonic() + 65
                continue
            elif "API_KEY" in msg or "401" in msg:
                log.error("Gemini key #%d auth error: %s", idx + 1, e)
                _gem_exhausted.add(idx)
                continue
            log.error("Gemini call failed: %s", e)
            return None

    log.warning("Gemini: all key(s) rate-limited or exhausted")
    return None


# ── Ollama (local, zero rate limits) ─────────────────────────────────────────
# Install: brew install ollama  →  ollama pull qwen2.5:7b  →  ollama serve
# Uses native Ollama structured-output (constrained decoding) so the model is
# forced to follow our exact JSON schema — no hallucinated field names.

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
_ollama_ok:  bool | None = None


_OLLAMA_SYSTEM = """You are a JSON extraction API. Extract Indian credit/debit card data from the web page.

Your response MUST start with {"cards":[ and end with ]} — no other text.

JSON structure (fill ALL fields from the page — null only if truly absent):
{"cards":[{"card_id":"<issuer>__<name-slug>","card_name":"<official name>","issuer_id":"<ISSUER_ID_HINT>","issuer_name":"<ISSUER_NAME_HINT>","category":"credit|debit|prepaid","source_url":"<SOURCE_URL>","network":"Visa|Mastercard|RuPay|Amex|Diners|null","segment":"entry|mid|premium|super-premium|null","fees":{"joining_fee_inr":<number|null>,"annual_fee_inr":<number|null>,"renewal_fee_inr":<number|null>,"fee_waiver_spend_inr":<number|null>,"fx_markup_pct":<number|null>,"gst_extra":<true|false>},"rewards":{"base_rate_pct":<number|null>,"currency":"cashback|points|miles|null","point_value_inr":<number|null>,"accelerated":[{"category":"<dining|travel|grocery|fuel|online|etc>","rate_pct":<number>,"cap_inr":<number|null>,"notes":"<brands or conditions>"}],"redemption_modes":["<mode>"],"exclusions":["<category>"]},"milestones":[{"spend_inr":<number>,"reward":"<description>","value_inr":<number|null>,"period":"monthly|quarterly|annual|null"}],"welcome_benefit":"<text|null>","partner_offers":[{"partner":"<brand>","benefit":"<full offer text>"}],"lounge_access":{"domestic_visits_year":<int|null>,"international_visits_year":<int|null>,"guest_allowed":<bool|null>,"program":"<Priority Pass|DreamFolks|null>","spend_unlock_inr":<number|null>},"insurance":{"air_accident_inr":<number|null>,"lost_card_inr":<number|null>,"purchase_protection_inr":<number|null>,"travel_inr":<number|null>},"perks":[{"kind":"<golf|movie|dining|spa|lounge|fuel|concierge|etc>","value":"<description>"}],"eligibility":{"min_age":<int|null>,"max_age":<int|null>,"min_income_inr_year":<number|null>,"salaried":<bool|null>,"self_employed":<bool|null>},"apply_url":"<url|null>"}]}

Rules:
- Read the page carefully. Extract ALL values present. Do NOT copy from examples.
- DETAIL PAGE: 1 card only (the main product). LISTING PAGE: all products.
- card_id = issuer_id + "__" + card name lowercased with hyphens"""


def _call_ollama(_system: str, user: str) -> str | None:
    """Call local Ollama with a small-model-friendly prompt. Returns raw JSON or None."""
    global _ollama_ok

    if _ollama_ok is False:
        return None

    try:
        import ollama as _ollama_lib
    except ImportError:
        _ollama_ok = False
        return None

    try:
        # Prefill the assistant response to force the model to start with {"cards":[
        # This is the most reliable way to enforce a specific JSON structure with local models.
        resp = _ollama_lib.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system",    "content": _OLLAMA_SYSTEM},
                {"role": "user",      "content": user},
                {"role": "assistant", "content": '{"cards":['},
            ],
            options={"temperature": 0},
        )
        _ollama_ok = True
        # Ollama serializes the continuation with escaped quotes — unescape then prepend prefix.
        continuation = resp.message.content.replace('\\"', '"').replace("\\'", "'")
        return '{"cards":[' + continuation
    except Exception as e:
        msg = str(e)
        if "connection" in msg.lower() or "refused" in msg.lower() or "connect" in msg.lower() \
                or "not found" in msg.lower():
            if _ollama_ok is None:
                log.debug("Ollama not running or model not found — skipping")
            _ollama_ok = False
            return None
        log.warning("Ollama error: %s", e)
        return None


# ── OpenAI (paid fallback) ────────────────────────────────────────────────────

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_RPM   = int(os.getenv("OPENAI_RPM", "500"))
_OAI_MIN_IV  = 60.0 / OPENAI_RPM

_oai_key:       str              = ""
_oai_last_call: float            = 0.0
_oai_exhausted: bool             = False


def _oai_wait() -> None:
    global _oai_last_call
    gap = time.monotonic() - _oai_last_call
    if gap < _OAI_MIN_IV:
        time.sleep(_OAI_MIN_IV - gap)
    _oai_last_call = time.monotonic()


def _call_openai(system: str, user: str) -> str | None:
    """OpenAI gpt-4o-mini fallback. Returns raw JSON string or None."""
    global _oai_key, _oai_exhausted

    if _oai_exhausted:
        return None

    if not _oai_key:
        _oai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not _oai_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai package not installed — skipping OpenAI fallback")
        return None

    try:
        _oai_wait()
        client = OpenAI(api_key=_oai_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
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
        # Check billing/quota BEFORE generic 429 — insufficient_quota also returns HTTP 429
        if "insufficient_quota" in msg or "billing" in msg.lower():
            log.error("OpenAI quota/billing error — disabling for this run: %s", e)
            _oai_exhausted = True
            return None
        if "429" in msg or "rate_limit" in msg.lower():
            log.warning("OpenAI rate-limited: %s", e)
            time.sleep(30)
            return None
        log.error("OpenAI call failed: %s", e)
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def extract_cards(batch: list[dict]) -> list[dict] | None:
    """Extract card records from a page via Ollama → Gemini → Groq → OpenAI.

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
        # No truncation — nav/header/footer stripped by Jina so pages are compact.
        # Listing pages (nav-stripped): ~5-8K chars for 20-30 cards.
        # Detail pages (nav-stripped): ~3-6K chars with full fees/rewards/perks.
        content = p["markdown"]
        parts.append(
            f"--- DOCUMENT {i} ---\n"
            f"SOURCE_URL: {p['source_url']}\n"
            f"ISSUER_ID_HINT: {p.get('issuer_id') or ''}\n"
            f"ISSUER_NAME_HINT: {p.get('issuer_name') or ''}\n"
            f"PAGE_TYPE: {hint}\n\n"
            f"{content}\n"
            f"--- END DOCUMENT {i} ---"
        )
    user_content = "\n\n".join(parts)
    provider     = "unknown"

    # 0. Ollama — local, zero rate limits (skipped silently if not running)
    raw = _call_ollama(SYSTEM, user_content)
    if raw is not None:
        provider = "ollama"
    else:
        # 1. Gemini — free tier, 1M TPM (fastest cloud option)
        raw = _call_gemini(f"{SYSTEM}\n\n{user_content}")
        if raw is not None:
            provider = "gemini"
        else:
            # 2. Groq — free tier, 20K TPM
            raw = _call_groq(SYSTEM, user_content)
            if raw is not None:
                provider = "groq"
            else:
                # 3. OpenAI — paid fallback
                raw = _call_openai(SYSTEM, user_content)
                if raw is not None:
                    provider = "openai"
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
