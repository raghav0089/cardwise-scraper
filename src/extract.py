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
import os, json, logging, re, time  # re used for post-processing in extract_cards
from pathlib import Path
from jsonschema import Draft7Validator
from .parse import parse_cards, _clean_card_name, _WB_JUNK_RE, _WB_VALUE_RE
from .banks import get_extractor

log = logging.getLogger(__name__)
SCHEMA    = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)

# Paid cloud LLMs (Gemini / Groq / OpenAI) cost money. They are OFF by default
# so a run can never incur charges. Local Ollama is always allowed (free).
# Set ALLOW_PAID_LLM=1 to enable the paid fallback chain.
ALLOW_PAID_LLM = os.getenv("ALLOW_PAID_LLM", "0") == "1"

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


# Grounded prompt — NO example values (small models copy example numbers verbatim)
# and NO assistant prefill (it makes them regurgitate a plausible-but-fake card).
# The instruction is purely about faithfully transcribing what the page states.
_OLLAMA_SYSTEM = """You extract structured data about ONE Indian payment card from the web page text given to you.

ABSOLUTE RULES:
- Use ONLY facts written in the PAGE text. Never invent, guess, or recall a card from memory.
- The card_name MUST be the product name that literally appears in the PAGE. If you cannot find a card name in the PAGE, return {"cards":[]}.
- issuer_id and issuer_name MUST be exactly the ISSUER_ID_HINT and ISSUER_NAME_HINT given.
- source_url MUST be exactly the SOURCE_URL given.
- For any field not stated in the PAGE, use null (or [] for lists). Do not fill placeholders.
- Output ONLY a JSON object, no prose.

Output shape:
{"cards":[{"card_name":string,"issuer_id":string,"issuer_name":string,"category":"credit|debit|prepaid|forex|business","source_url":string,"network":string|null,"segment":string|null,"fees":{"joining_fee_inr":number|null,"annual_fee_inr":number|null,"renewal_fee_inr":number|null,"fee_waiver_spend_inr":number|null,"fx_markup_pct":number|null,"gst_extra":boolean},"rewards":{"base_rate_pct":number|null,"currency":string|null,"point_value_inr":number|null,"accelerated":[{"category":string,"rate_pct":number,"cap_inr":number|null,"notes":string|null}],"redemption_modes":[string],"exclusions":[string]},"milestones":[{"spend_inr":number,"reward":string,"value_inr":number|null,"period":string|null}],"welcome_benefit":string|null,"partner_offers":[{"partner":string,"benefit":string}],"lounge_access":{"domestic_visits_year":number|null,"international_visits_year":number|null,"guest_allowed":boolean|null,"program":string|null,"spend_unlock_inr":number|null},"insurance":{"air_accident_inr":number|null,"lost_card_inr":number|null,"purchase_protection_inr":number|null,"travel_inr":number|null},"perks":[{"kind":string,"value":string}],"eligibility":{"min_age":number|null,"max_age":number|null,"min_income_inr_year":number|null,"salaried":boolean|null,"self_employed":boolean|null},"apply_url":string|null}]}

All INR amounts are plain numbers (50000, not "₹50,000"). A LISTING PAGE may yield several cards; a DETAIL PAGE yields exactly one."""


def _call_ollama(_system: str, user: str) -> str | None:
    """Call local Ollama with a grounded prompt + JSON format. Returns raw JSON or None."""
    global _ollama_ok

    if _ollama_ok is False:
        return None

    try:
        import ollama as _ollama_lib
    except ImportError:
        _ollama_ok = False
        return None

    try:
        resp = _ollama_lib.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": _OLLAMA_SYSTEM},
                {"role": "user",   "content": user},
            ],
            format="json",                 # constrained JSON output
            options={"temperature": 0},
        )
        _ollama_ok = True
        return resp.message.content
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


# ── Anti-hallucination guard ──────────────────────────────────────────────────

# Generic tokens that don't help prove a name came from the page.
_NAME_GENERIC = frozenset({
    "card", "credit", "debit", "prepaid", "forex", "the", "bank", "co", "branded",
    "rupay", "visa", "mastercard", "amex", "diners", "and", "of", "plus",
})

def _name_grounded_in_page(name: str | None, md: str) -> bool:
    """True if the card name's distinctive words actually appear in the page text.

    Protects against small local models inventing a card that isn't on the page.
    Requires that the meaningful (non-generic) tokens of the name are present in
    the source markdown.
    """
    if not name:
        return False
    if not md:
        return True   # nothing to check against — don't over-reject
    md_low = md.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower())
              if len(t) > 2 and t not in _NAME_GENERIC]
    if not tokens:
        # Name was entirely generic words ("Credit Card") — accept only if that exact
        # phrase is present; otherwise it's not a real distinct product name.
        return name.lower() in md_low
    hits = sum(1 for t in tokens if t in md_low)
    # Most distinctive tokens must be present (allow one OCR/spelling slip).
    return hits >= max(1, len(tokens) - 1)


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

    # Bank-specific extractor takes priority over generic rule-based + LLM pipeline
    issuer_id = valid[0].get("issuer_id")
    bank_ext  = get_extractor(issuer_id)
    if bank_ext:
        result = bank_ext.extract(valid)
        log.info("extracted %d card(s) via %s bank extractor", len(result), issuer_id)
        return result

    # Primary: rule-based parser — no API calls, always runs first
    rule_cards: list[dict] = []
    for p in valid:
        rule_cards.extend(parse_cards(p))
    if rule_cards:
        log.info("extracted %d card(s) from %d page(s) via rule-based parser",
                 len(rule_cards), len(valid))
        return rule_cards

    # Skip LLM fallback for listing pages: their individual detail URLs are already
    # in the queue and will be scraped separately. Passing a listing page to the LLM
    # produces hallucinated data mixing cards from multiple issuers.
    if all(p.get("is_listing") for p in valid):
        log.debug("listing page(s) produced no rule-based cards — skipping LLM")
        return []

    # Fallback: LLM chain (Ollama → Gemini → Groq → OpenAI)
    # Cap per-document length: small local models (qwen2.5:7b / llama3.2) degrade or
    # return empty on very long contexts. The product info (name/fees/rewards) is
    # almost always in the first part of the page; the tail is FAQs / T&C / footer.
    max_chars = int(os.getenv("LLM_MAX_CHARS", "9000"))
    parts = []
    for i, p in enumerate(valid, 1):
        is_listing = p.get("is_listing", False)
        hint = "LISTING PAGE — extract ALL individual card products mentioned." \
               if is_listing else "DETAIL PAGE — extract the single card described."
        content = p["markdown"]
        if len(content) > max_chars:
            content = content[:max_chars]
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

    # 0. Ollama — local, zero rate limits, FREE (skipped silently if not running)
    raw = _call_ollama(SYSTEM, user_content)
    if raw is not None:
        provider = "ollama"
    elif not ALLOW_PAID_LLM:
        # Paid cloud LLMs disabled — don't spend money. Return [] (not None) so the
        # source is marked seen and we don't endlessly retry a page Ollama couldn't do.
        log.info("no local LLM result and ALLOW_PAID_LLM=0 — skipping paid providers for %s",
                 valid[0]["source_url"])
        return []
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
        # Always trust the configured issuer — LLMs hallucinate wrong bank names when
        # a page contains competitor card ads or cross-sell sections.
        if page.get("issuer_id"):
            c["issuer_id"] = page["issuer_id"]
        if page.get("issuer_name"):
            c["issuer_name"] = page["issuer_name"]
        c.setdefault("issuer_id",   None)
        c.setdefault("issuer_name", None)
        c["source_url"] = src_url

        # Clean card_name: LLMs sometimes return full SEO titles or markdown link artifacts
        if raw_name := c.get("card_name"):
            cleaned = _clean_card_name(raw_name)
            if cleaned:
                c["card_name"] = cleaned

        # Anti-hallucination guard: a small local model can invent a plausible card
        # that isn't on the page at all. Require the card name to actually appear in
        # the source text, or drop the record entirely.
        if not _name_grounded_in_page(c.get("card_name"), page.get("markdown", "")):
            log.info("dropped ungrounded LLM card %r (name not found on %s)",
                     c.get("card_name"), src_url)
            continue

        # Filter garbage welcome_benefit values the LLM copies verbatim from markdown
        wb = c.get("welcome_benefit")
        if wb and isinstance(wb, str):
            wb = wb.strip().strip('"').strip()
            if (not wb or wb.lower() == "null"
                    or _WB_JUNK_RE.search(wb)
                    or (not _WB_VALUE_RE.search(wb) and len(wb) < 30)
                    or re.match(r'^!', wb)):
                c["welcome_benefit"] = None
            else:
                c["welcome_benefit"] = wb

        errs = sorted(VALIDATOR.iter_errors(c), key=lambda e: e.path)
        if errs:
            log.debug("schema warnings for %s: %s",
                      c.get("card_id"), [e.message for e in errs[:3]])
        out.append(c)

    log.info("extracted %d card(s) from %d page(s) via %s", len(out), len(valid), provider)
    return out
