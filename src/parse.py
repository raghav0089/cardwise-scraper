"""Rule-based card data extractor for Jina-rendered markdown.

Replaces per-page LLM calls. Extracts fees, rewards, lounge, partner offers,
milestones, perks, eligibility — everything the schema supports.
"""
from __future__ import annotations
import re, logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _num(s: str) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except Exception:
        return None

def _window(md: str, match: re.Match, before: int = 60, after: int = 200) -> str:
    return md[max(0, match.start() - before): match.end() + after].replace("\n", " ")

# ── Card name ─────────────────────────────────────────────────────────────────

_NAME_PATTERNS = [
    re.compile(r"^Title:\s*(.+)", re.I | re.MULTILINE),
    re.compile(r"^#\s+(.{5,100})", re.MULTILINE),
    re.compile(r"^##\s+(.{5,80}card[^\n]*)", re.I | re.MULTILINE),
]

def _name(md: str) -> Optional[str]:
    for pat in _NAME_PATTERNS:
        m = pat.search(md)
        if m:
            val = m.group(1).strip().rstrip("|").strip()
            if 5 < len(val) < 120:
                return val
    return None

# ── Category / network ────────────────────────────────────────────────────────

_CATEGORY_RE = {
    "credit":  re.compile(r"credit\s+card", re.I),
    "debit":   re.compile(r"debit\s+card", re.I),
    "prepaid": re.compile(r"prepaid\s+card", re.I),
    "forex":   re.compile(r"forex\s+card|multi.?currency\s+card", re.I),
}
_NETWORK_RE = re.compile(r"\b(Visa|Mastercard|RuPay|Amex|American\s+Express|Diners)\b", re.I)
_NETWORK_NORM = {
    "american express": "Amex", "amex": "Amex",
    "visa": "Visa", "mastercard": "Mastercard",
    "rupay": "RuPay", "diners": "Diners",
}
_SEGMENT_RE = {
    "super-premium": re.compile(r"super.?premium|ultra.?premium", re.I),
    "premium":       re.compile(r"\bpremium\b", re.I),
    "mid":           re.compile(r"\bmid.?range\b", re.I),
    "entry":         re.compile(r"\bentry.?level\b|\bbasic\b", re.I),
    "student":       re.compile(r"\bstudent\b", re.I),
    "secured":       re.compile(r"\bsecured\b|\bagainst\s+fd\b", re.I),
}
_METAL_RE = re.compile(r"\bmetal\s+card\b", re.I)

# ── Fees ──────────────────────────────────────────────────────────────────────

_INR = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)"
_FEES = [
    ("joining_fee_inr",       re.compile(r"join(?:ing)?\s*fee[^₹\d\n]{0,40}" + _INR, re.I)),
    ("annual_fee_inr",        re.compile(r"annual\s*(?:fee|membership)[^₹\d\n]{0,40}" + _INR, re.I)),
    ("renewal_fee_inr",       re.compile(r"renewal\s*fee[^₹\d\n]{0,40}" + _INR, re.I)),
    ("fee_waiver_spend_inr",  re.compile(r"(?:fee\s+waiver|waived?\s+on\s+(?:annual\s+)?spend)[^₹\d\n]{0,50}" + _INR, re.I)),
    ("addon_card_fee_inr",    re.compile(r"(?:add.?on|supplementary)\s*card\s*fee[^₹\d\n]{0,40}" + _INR, re.I)),
    ("cash_advance_fee_pct",  re.compile(r"cash\s*advance\s*fee[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("finance_charge_pct_mo", re.compile(r"(?:finance\s*charge|monthly\s*interest|interest\s*rate)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("fx_markup_pct",         re.compile(r"(?:forex|foreign\s*currency|cross.?currency)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
]
_FUEL_RE    = re.compile(r"fuel\s+surcharge\s+waiver[^.\n]{0,120}", re.I)
_GST_RE     = re.compile(r"\+\s*gst|exclusive\s+of\s+gst|plus\s+applicable\s+taxes", re.I)

def _fees(md: str) -> dict:
    out: dict = {}
    for key, pat in _FEES:
        m = pat.search(md)
        if m:
            out[key] = _num(m.group(1))
    if m := _FUEL_RE.search(md):
        out["fuel_surcharge_waiver"] = m.group(0).strip()
    if _GST_RE.search(md):
        out["gst_extra"] = True
    return out

# ── Rewards ───────────────────────────────────────────────────────────────────

_RP_PER_SPEND = re.compile(
    r"(\d+)\s*(?:reward\s+)?points?\s+(?:per|for\s+every|on\s+every)\s+[₹rs.]*\s*([\d,]+)", re.I)
_CASHBACK_ALL = re.compile(r"([\d.]+)\s*%\s*cashback\s+on\s+all", re.I)
_POINT_VAL    = re.compile(r"1\s*(?:reward\s+)?point\s*[=:]\s*[₹rs.]*\s*([\d.]+)", re.I)
_CURRENCY_RE  = re.compile(
    r"\b(reward\s+points?|cashback|miles|neucoins|edge\s+miles|cred\s+coins)\b", re.I)
_ACC_CAT      = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:x|X|%)\s*(?:reward\s+points?|rp|cashback)?\s+on\s+([\w\s&/,\-]+?)(?=[,.\n(]|$)", re.I)
_EXPIRY_RE    = re.compile(r"(?:points?\s+(?:expire|valid)\s+for\s+)?(\d+)\s*months?", re.I)
_EXCL_RE      = re.compile(r"(?:no\s+rewards?|not\s+earned?)\s+on\s+([\w\s,&/]+?)(?=[.\n]|$)", re.I)
_REDEEM_RE    = re.compile(
    r"(?:redeem|redemption).*?(?:for|against|at|on)\s+([\w\s,&/\-]+?)(?=[.\n])", re.I)

def _rewards(md: str) -> dict:
    out: dict = {}

    pv = None
    if m := _POINT_VAL.search(md):
        pv = _num(m.group(1))
        out["point_value_inr"] = pv

    if m := _RP_PER_SPEND.search(md):
        rp, spend = float(m.group(1)), _num(m.group(2))
        if spend and spend > 0:
            rate = round((pv or 0.25) * rp / spend * 100, 4)
            out["base_rate_pct"] = rate
    elif m := _CASHBACK_ALL.search(md):
        out["base_rate_pct"] = float(m.group(1))
        out["currency"] = "cashback"

    if m := _CURRENCY_RE.search(md):
        raw = m.group(1).lower().replace(" ", "_")
        out.setdefault("currency", raw)

    acc = []
    seen_cats: set[str] = set()
    for m in _ACC_CAT.finditer(md):
        rate_str, cat = m.group(1).strip(), m.group(2).strip().rstrip(",.")
        if not cat or cat.lower() in seen_cats or len(cat) > 60:
            continue
        seen_cats.add(cat.lower())
        acc.append({"category": cat, "rate_pct": float(rate_str), "cap_inr": None, "notes": None})
    if acc:
        out["accelerated"] = acc

    excl = []
    for m in _EXCL_RE.finditer(md):
        excl.append(m.group(1).strip())
    if excl:
        out["exclusions"] = excl

    modes = []
    for m in _REDEEM_RE.finditer(md):
        modes.append(m.group(1).strip())
    if modes:
        out["redemption_modes"] = modes[:8]

    if m := _EXPIRY_RE.search(md):
        out["expiry_months"] = int(m.group(1))

    return out

# ── Welcome benefit ───────────────────────────────────────────────────────────

_WELCOME_RE = re.compile(
    r"(?:welcome|joining)\s+(?:benefit|gift|bonus|offer)[s:]*\s*([^.]{20,300})", re.I)

def _welcome(md: str) -> Optional[str]:
    m = _WELCOME_RE.search(md)
    return m.group(1).strip() if m else None

# ── Milestones ────────────────────────────────────────────────────────────────

_MILESTONE_RE = re.compile(
    r"(?:spend|spending|spends?)\s+(?:of\s+)?[₹rs.]*\s*([\d,]+)[^.\n]{0,80}"
    r"(?:get|earn|receive|enjoy|unlock|bonus)\s+([^.\n]{10,150})",
    re.I,
)

def _milestones(md: str) -> list:
    out = []
    seen: set[float] = set()
    for m in _MILESTONE_RE.finditer(md):
        spend = _num(m.group(1))
        if not spend or spend in seen:
            continue
        seen.add(spend)
        out.append({"spend_inr": spend, "reward": m.group(2).strip(),
                    "value_inr": None, "period": None})
    return out

# ── Lounge access ─────────────────────────────────────────────────────────────

_DOM_LOUNGE  = re.compile(r"(\d+)\s+(?:complimentary\s+)?domestic\s+(?:airport\s+)?lounge", re.I)
_INTL_LOUNGE = re.compile(r"(\d+)\s+(?:complimentary\s+)?international\s+(?:airport\s+)?lounge", re.I)
_LOUNGE_PROG = re.compile(r"\b(Priority\s+Pass|DreamFolks|LoungeKey|Lounge\s+Key)\b", re.I)
_LOUNGE_SPEND= re.compile(r"lounge[^.\n]{0,80}spend[^₹\d]{0,20}" + _INR, re.I)
_QTR_RE      = re.compile(r"per\s+quarter|quarterly", re.I)

def _lounge(md: str) -> dict:
    out: dict = {}
    if m := _DOM_LOUNGE.search(md):
        v = int(m.group(1))
        if _QTR_RE.search(_window(md, m, after=60)):
            v *= 4
        out["domestic_visits_year"] = v
    if m := _INTL_LOUNGE.search(md):
        v = int(m.group(1))
        if _QTR_RE.search(_window(md, m, after=60)):
            v *= 4
        out["international_visits_year"] = v
    if m := _LOUNGE_PROG.search(md):
        out["program"] = m.group(1).replace("  ", " ")
    if m := _LOUNGE_SPEND.search(md):
        out["spend_unlock_inr"] = _num(m.group(1))
    return out

# ── Insurance ─────────────────────────────────────────────────────────────────

_AIR_ACC_RE  = re.compile(r"air\s+accident[^₹\d\n]{0,50}" + _INR, re.I)
_LOST_RE     = re.compile(r"lost\s+card[^₹\d\n]{0,50}" + _INR, re.I)
_PURCH_RE    = re.compile(r"purchase\s+protection[^₹\d\n]{0,50}" + _INR, re.I)
_TRAVEL_RE   = re.compile(r"travel\s+insurance[^₹\d\n]{0,50}" + _INR, re.I)

def _insurance(md: str) -> dict:
    out: dict = {}
    for key, pat in [("air_accident_inr", _AIR_ACC_RE), ("lost_card_inr", _LOST_RE),
                     ("purchase_protection_inr", _PURCH_RE), ("travel_inr", _TRAVEL_RE)]:
        m = pat.search(md)
        if m:
            out[key] = _num(m.group(1))
    return out

# ── Partner offers ────────────────────────────────────────────────────────────

_PARTNERS = [
    "Swiggy", "Zomato", "Amazon", "Flipkart", "BookMyShow", "Myntra",
    "Ola", "Uber", "IRCTC", "MakeMyTrip", "Cleartrip", "Nykaa",
    "BigBasket", "Blinkit", "PhonePe", "Paytm", "Tata Neu", "Cred",
    "Cult.fit", "Lenskart", "Dominos", "Pizza Hut", "Starbucks",
    "Ajio", "Netmeds", "1mg", "Apollo", "Zepto",
]
_OFFER_SIGNAL = re.compile(r"%|₹|cashback|discount|off\b|voucher|free", re.I)

def _partner_offers(md: str) -> list:
    offers = []
    for partner in _PARTNERS:
        for m in re.finditer(re.escape(partner), md, re.I):
            snippet = _window(md, m, before=80, after=220)
            if _OFFER_SIGNAL.search(snippet):
                offers.append({"partner": partner, "benefit": snippet.strip()})
                break
    return offers

# ── Perks (catch-all) ─────────────────────────────────────────────────────────

_PERK_PATS = [
    ("golf",             re.compile(r"\bgolf\b", re.I)),
    ("movie",            re.compile(r"(?:movie|cinema|pvr|inox|bookmyshow\s+ticket)", re.I)),
    ("dining",           re.compile(r"(?:\bdining\b|restaurant\s+(?:offer|discount|benefit))", re.I)),
    ("concierge",        re.compile(r"\bconcierge\b", re.I)),
    ("spa",              re.compile(r"\bspa\b", re.I)),
    ("hotel",            re.compile(r"(?:complimentary\s+(?:hotel|night|room)|hotel\s+upgrade)", re.I)),
    ("subscription",     re.compile(r"(?:amazon\s+prime|netflix|hotstar|zee5|zomato\s+gold|swiggy\s+one|sonyliv)", re.I)),
    ("railway_lounge",   re.compile(r"railway\s+lounge", re.I)),
    ("roadside_assist",  re.compile(r"roadside\s+assist", re.I)),
    ("golf_simulator",   re.compile(r"golf\s+simulator", re.I)),
    ("annual_voucher",   re.compile(r"annual\s+(?:travel|shopping|lifestyle)\s+voucher", re.I)),
    ("forex_card_perk",  re.compile(r"zero\s+(?:cross.?currency|forex)\s+markup", re.I)),
]

def _perks(md: str) -> list:
    out = []
    for kind, pat in _PERK_PATS:
        m = pat.search(md)
        if m:
            out.append({"kind": kind, "value": _window(md, m, before=20, after=160).strip()})
    return out

# ── Eligibility ───────────────────────────────────────────────────────────────

_MIN_AGE    = re.compile(r"min(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)
_MAX_AGE    = re.compile(r"max(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)
_MIN_INC    = re.compile(r"(?:min(?:imum)?\s+)?(?:annual\s+)?income[^₹\d\n]{0,30}" + _INR, re.I)
_SALARIED   = re.compile(r"\bsalaried\b", re.I)
_SELFEMPL   = re.compile(r"self.?employed|business\s+owner", re.I)

def _eligibility(md: str) -> dict:
    out: dict = {}
    if m := _MIN_AGE.search(md): out["min_age"] = int(m.group(1))
    if m := _MAX_AGE.search(md): out["max_age"] = int(m.group(1))
    if m := _MIN_INC.search(md): out["min_income_inr_year"] = _num(m.group(1))
    if _SALARIED.search(md):     out["salaried"] = True
    if _SELFEMPL.search(md):     out["self_employed"] = True
    return out

# ── Apply URL ─────────────────────────────────────────────────────────────────

_APPLY_RE = re.compile(r"\[(?:apply|apply\s+now)[^\]]*\]\((https?://[^\s)]+)\)", re.I)

def _apply_url(md: str) -> Optional[str]:
    m = _APPLY_RE.search(md)
    return m.group(1) if m else None

# ── Main entry ────────────────────────────────────────────────────────────────

def parse_card(page: dict) -> dict:
    """Parse a Jina markdown page into a card dict matching the schema.

    Returns an empty dict if the page doesn't look like a card product page.
    """
    md = page.get("markdown", "")

    # Quick reject: must mention some kind of card
    if not re.search(r"credit\s+card|debit\s+card|prepaid|forex\s+card", md, re.I):
        return {}

    card: dict = {
        "issuer_id":   page.get("issuer_id"),
        "issuer_name": page.get("issuer_name"),
        "source_url":  page.get("source_url"),
        "category":    "credit",
    }

    if n := _name(md):
        card["card_name"] = n

    for cat, pat in _CATEGORY_RE.items():
        if pat.search(md):
            card["category"] = cat
            break

    if m := _NETWORK_RE.search(md):
        raw = m.group(1).lower().replace(" ", " ")
        card["network"] = _NETWORK_NORM.get(raw, m.group(1).title())

    for seg, pat in _SEGMENT_RE.items():
        if pat.search(md):
            card["segment"] = seg
            break

    if _METAL_RE.search(md):
        card["card_material"] = "metal"

    if fees := _fees(md):
        card["fees"] = fees

    if rw := _rewards(md):
        card["rewards"] = rw

    if wb := _welcome(md):
        card["welcome_benefit"] = wb

    if ms := _milestones(md):
        card["milestones"] = ms

    if lg := _lounge(md):
        card["lounge_access"] = lg

    if ins := _insurance(md):
        card["insurance"] = ins

    if po := _partner_offers(md):
        card["partner_offers"] = po

    if pk := _perks(md):
        card["perks"] = pk

    if el := _eligibility(md):
        card["eligibility"] = el

    if au := _apply_url(md):
        card["apply_url"] = au

    # Require at least a card name to be useful
    if not card.get("card_name"):
        log.debug("parse: no card name found for %s", page.get("source_url"))
        return {}

    return card
