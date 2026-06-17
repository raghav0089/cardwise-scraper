"""Orchestrator — invoked by GitHub Actions once a day.

Set TEST_URLS=1 to run full discovery for _SMOKE_ISSUERS only (fast validation).
Set TEST_RUN=N to cap total URLs processed from the normal full issuer list.
"""
from __future__ import annotations
import os, json, logging, time, sys, re
from pathlib import Path
from urllib.parse import urlparse
from .fetch import fetch, sha256
from .scrape_issuers import collect_detail_urls
from .discover import discover_candidate_urls
from .extract import extract_cards
from .normalize import ensure_card_id, stamp, dedupe
from . import store
from .banks import get_extractor as _get_bank_extractor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("main")
OUT = Path("out"); OUT.mkdir(exist_ok=True)

TEST_RUN      = int(os.getenv("TEST_RUN", "0"))
TEST_URLS     = os.getenv("TEST_URLS", "0") == "1"
MAX_CARDS     = int(os.getenv("MAX_CARDS", "0"))   # 0 = no limit
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "0") == "1"

# Issuers to scrape when TEST_URLS=1 — covers all card categories and
# exercises the full listing→harvest→extract pipeline, not just detail pages.
_SMOKE_ISSUERS = {"hdfc"}

# ── URL filters ───────────────────────────────────────────────────────────────

_JUNK_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico",
    ".pdf", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".css", ".js", ".xml",
})
_JUNK_DOMAINS = frozenset({
    "b-cdn.net", "wp.com", "bsmedia.business-standard.com",
    "static.bankbazaar.com", "images.moneycontrol.com",
    "stat1.moneycontrol.com", "rupay.co.in", "npci.org.in",
})
# Exact path-segment blocklist (e.g. /faq, /login, /cancellation)
_SKIP_SEGS = frozenset({
    "compare", "faq", "apply", "eligibility", "contact", "login", "register",
    "sitemap", "legal", "press", "support", "help", "download", "lounge",
    "cancellation", "referral", "experience", "calculator", "festive",
    "offers", "offer", "app", "careers", "career",
    # Static asset paths (images, fonts, scripts, etc.)
    "static", "static-resources", "static-pages", "img", "images", "assets",
    "fonts", "icons", "media", "svg",
    # Non-product helper pages
    "demo-videos", "demo-video",
    # Tracking / apply-redirect paths (e.g. sbicard.com /sprint/c/*)
    "sprint",
})
# Sub-string match within a segment (catches compound names like terms-and-conditions)
_SKIP_SEG_RE = re.compile(
    # Use \bemi\b so we don't accidentally block "premier", "premium", etc.
    r"\bemi\b|terms|fee|charge|privacy|personal-loan|bill-pay|"
    r"customer-care|negative-balance|do-not-call|credit-builder|"
    r"credit-card-service|different-type|top-\d|lounge-access|"
    r"zero-forex|best-lifetime|best-international|best-secured|"
    r"travel-credit-card|loan-on|balance-on|"
    r"report-block|lost-or-stolen|block-lost|stolen-card|"
    r"upgrade-your|manage-your|know-your-card|"
    r"how-to-apply|credit-card-tips|credit-card-guide|"
    r"credit-score|cardmember-agreement|"
    # Program/landing pages that are not individual card products
    r"referral|pre-approved-credit|lifetime-free-credit|"
    r"credit-card-against|best-credit-card|"
    # Utility / non-product pages
    r"apply-form|card-payment|network-guide|list-of-bc|"
    r"pin-change|generate.*pin|ways-to-generate|activation-credit|"
    # Informational / article pages (not card product pages)
    r"settlement|balance-transfer|balance-conversion|"
    r"paying-utility|utility-bill|what-is-upi|"
    r"maximise.*offer|bogo|fuel-surcharge|"
    r"income-tax|save.*dining|plan-your-trip",
    re.IGNORECASE,
)
_ALLOWED_DISCOVERY_DOMAINS = frozenset({
    "cardexpert.in", "cardinsider.com", "technofino.in",
    "bankbazaar.com", "paisabazaar.com", "economictimes.indiatimes.com",
    "livemint.com", "business-standard.com", "moneycontrol.com",
})

def _should_fetch(url: str, is_discovery: bool = False) -> bool:
    parsed = urlparse(url)
    host   = parsed.hostname or ""
    path   = parsed.path.lower()
    ext    = path.rsplit(".", 1)[-1] if "." in path.split("/")[-1] else ""
    if f".{ext}" in _JUNK_EXTENSIONS: return False
    root = ".".join(host.split(".")[-2:]) if host else ""
    if root in _JUNK_DOMAINS or host in _JUNK_DOMAINS: return False
    segments = [s for s in path.split("/") if s]
    if any(s in _SKIP_SEGS for s in segments): return False
    if any(_SKIP_SEG_RE.search(s) for s in segments): return False
    if is_discovery and root not in _ALLOWED_DISCOVERY_DOMAINS: return False
    return True




# ── URL gathering ─────────────────────────────────────────────────────────────

def gather_urls() -> list[dict]:
    if TEST_URLS:
        log.info("TEST_URLS mode: running full discovery for %s", sorted(_SMOKE_ISSUERS))
        urls = []
        for r in collect_detail_urls(issuer_ids=_SMOKE_ISSUERS):
            r["is_discovery"] = False
            urls.append(r)
        seen, uniq = set(), []
        for r in urls:
            if r["url"] not in seen:
                seen.add(r["url"]); uniq.append(r)
        log.info("smoke discovery: %d URLs to process", len(uniq))
        return uniq

    mode = os.getenv("RUN_MODE", "all")
    if mode == "single":
        return [{"url": os.environ["SINGLE_URL"], "issuer_id": None,
                 "issuer_name": None, "is_discovery": False}]

    urls: list[dict] = []
    if mode in ("all", "issuers"):
        for r in collect_detail_urls():
            r["is_discovery"] = False
            urls.append(r)
    if not TEST_RUN and mode in ("all", "discover"):
        for r in discover_candidate_urls():
            r["is_discovery"] = True
            urls.append(r)

    seen, uniq = set(), []
    for r in urls:
        if r["url"] not in seen:
            seen.add(r["url"]); uniq.append(r)

    if TEST_RUN:
        uniq = uniq[:TEST_RUN]
        log.info("TEST_RUN=%d: trimmed to %d urls", TEST_RUN, len(uniq))

    return uniq


# ── Fetch ─────────────────────────────────────────────────────────────────────

# Issuer config lookup for supplement pages (fees/eligibility split onto sub-pages).
try:
    from .scrape_issuers import CFG as _CFG
    _ISSUER_CFG = {i["id"]: i for i in _CFG.get("issuers", [])}
except Exception:
    _ISSUER_CFG = {}


def _append_supplements(url: str, text: str, issuer_id: str | None) -> str:
    """Fetch this issuer's configured supplement sub-pages (e.g. /fees-and-charges)
    for the card URL and append their text, so split fee/eligibility data lands on
    the same record. Best-effort: failures are ignored."""
    cfg = _ISSUER_CFG.get(issuer_id or "")
    suffixes = (cfg or {}).get("supplement_suffixes") or []
    # If the main page already states fee figures, the fee sub-page is redundant — skip
    # the extra fetch. Supplements exist to RESCUE cards whose main page omits fees.
    if suffixes and re.search(r"(joining|annual|renewal)[^\n]{0,40}(₹|rs\.?\s*\d|inr\s*\d)", text, re.I):
        return text
    base = url.rstrip("/")
    for suffix in suffixes:
        sup_url = base + suffix
        try:
            res = fetch(sup_url)
        except Exception as e:
            log.debug("supplement fetch error %s: %s", sup_url, e)
            continue
        sup = (res.text or res.html or "") if res and res.ok else ""
        if len(sup) < 200:
            continue
        first_line = sup[:200].split("\n")[0].lower()
        if any(p in first_line for p in ("page not found", "404", "does not exist")):
            continue
        label = suffix.strip("/").replace("-", " ").upper()
        text += f"\n\n## {label}\n{sup}"
        log.info("    appended supplement %s (%d chars)", suffix, len(sup))
    return text


def fetch_page(row: dict) -> dict | None:
    url          = row["url"]
    is_discovery = row.get("is_discovery", False)

    if not _should_fetch(url, is_discovery=is_discovery):
        log.debug("pre-filter skip: %s", url)
        return None
    bank_ext = _get_bank_extractor(row.get("issuer_id"))
    if bank_ext and bank_ext.skip_url(url):
        log.debug("bank-specific skip (%s): %s", row.get("issuer_id"), url)
        return None

    # Listing pages carry pre-fetched HTML from the URL-gather phase — reuse it
    prefetched = row.get("prefetched_html", "")
    if prefetched:
        text = prefetched
        etag = None
    else:
        result = fetch(url)
        if not result.ok:
            log.warning("fetch failed %s: %s", url, result.error)
            return None
        text = result.text or result.html or ""
        etag = result.etag

    if len(text) < 200:
        log.debug("skip near-empty page: %s", url)
        return None

    # Detect 404 pages returned as HTTP 200 — check only the first line (Jina title).
    # Checking too many chars causes false positives when navigation text says "page not found".
    first_line = text[:200].split("\n")[0].lower()
    if any(p in first_line for p in ("page not found", "404 error", "error 404",
                                     "page doesn't exist", "page does not exist")):
        log.info("404 content detected, skip: %s  (title: %r)", url, first_line[:80])
        return None

    # Some banks split fees / eligibility onto dedicated sub-pages (e.g. IndusInd,
    # HDFC: <card-url>/fees-and-charges). Fetch configured supplement pages for this
    # issuer and append them so the parser/LLM see that data on the same record.
    if not prefetched and not row.get("is_listing"):
        text = _append_supplements(url, text, row.get("issuer_id"))

    sha = sha256(text)
    if not FORCE_REFRESH and store.source_unchanged(url, sha):
        log.info("unchanged, skip: %s", url)
        return None

    if not prefetched:
        store.archive_raw(url, text)
    return {
        "source_url":  url,
        "issuer_id":   row.get("issuer_id"),
        "issuer_name": row.get("issuer_name"),
        "is_listing":  row.get("is_listing", False),
        "markdown":    text,
        "sha":         sha,
        "etag":        etag,
    }


# ── Card name validation ──────────────────────────────────────────────────────

# The word "card" must appear in a valid product name.
_CARD_KW_RE = re.compile(r'\bcard\b', re.I)
# Reject names that are page-section headings, announcements, or questions.
_BAD_NAME_RE = re.compile(
    r'^(page[\s-]?not[\s-]?found|404\b|have\s+you\b|we\s+(are|re|were)\b|'
    r'existing\s+card|additional\s+benefit|savings?\s+calculat|'
    r'new\s+feature|about\s+|faq\b|important[\s:]+|note[\s:]+|'
    r'introducing\b|happy\s+to|pleased\s+to|'
    r'report\s*(?:&|and)?\s*block|lost\s+or\s+stolen|block\s+(?:lost|stolen)|'
    r'how\s+to|manage\s+your|upgrade\s+your|apply\s+for\s+(?:a\s+)?(?:hdfc|sbi|icici|axis|kotak)|'
    # Generic page-section headings starting with "Credit/Debit Card ..."
    r'(?:credit|debit)\s+card\s+(?:benefits?|features?|faqs?|offers?|types?|blogs?|'
    r'basics?|rewards?|services?|tips?|guide|insurance|eligib|interest|limit\s+incr|'
    r'pin\b|block|closur|application|apply|vs\b|against\b|bill\s+pay)|'
    # Descriptive/educational headings that sneak "card" in
    r'understanding\s|outstanding\s+amount|instant\s+emi|below\s+are\s|'
    r'type\s+of\s+(?:credit|debit)|difference\s+between|what\s+is\s+credit|'
    # SEO page-title patterns that are not product names
    r'best\s+credit\s+card|make\s+your\s|track\s+your\s|instant\s+and\s+easy|'
    r'list\s+of\s+bc|list\s+of\s+business)',
    re.I,
)

# These words anywhere in the name mean it's a page section, not a product.
_REJECT_SUBSTR_RE = re.compile(
    r'\b(calculator|eligibility\s+check|comparison|activate|activation|'
    r'apply\s+now|know\s+more|click\s+here|apply\s+for\b|'
    r'report\s+(?:&|and)?\s*block|lost\s+or\s+stolen|quickly\b|'
    r'availability\s+guide|ways\s+to\s+generate|payments?\s+instantly)\b|'
    r'\bvs\.?\s+(?:debit|credit|bnpl|upi|prepaid)\b',
    re.I,
)

def _is_valid_card_name(name: str) -> bool:
    if not name:
        return False
    name = name.strip()
    if len(name) > 100:       # SEO titles are always long (>100 chars)
        return False
    if '?' in name:           # activation prompts, FAQ entries
        return False
    if "://" in name or name.startswith("//"):  # URL stored as card name
        return False
    if _BAD_NAME_RE.match(name):
        return False
    if _REJECT_SUBSTR_RE.search(name):
        return False
    if not _CARD_KW_RE.search(name):
        return False
    return True


# ── Cleanup ───────────────────────────────────────────────────────────────────

_LOWERCASE_ID_RE = re.compile(r'^[a-z0-9_]+$')

def purge_invalid_cards() -> None:
    """Delete DDB records whose card_name would fail validation (garbage from bad extractions)."""
    all_cards = store.scan_all_cards()
    if not all_cards:
        return
    deleted = 0
    for c in all_cards:
        name  = (c.get("card_name") or "").strip()
        raw_iid = c.get("issuer_id")
        # Non-string issuer_id (e.g. YAML boolean True from `id: yes`) — purge
        if not isinstance(raw_iid, str):
            store.delete_card(c["card_id"])
            log.info("purged non-string issuer_id record: %s  (issuer_id=%r name=%r)", c["card_id"], raw_iid, name)
            deleted += 1
            continue
        iid = raw_iid.strip()
        # Discovery-phase cards land with issuer_id=None → stored as "unknown" — purge them
        if not iid or iid == "unknown":
            store.delete_card(c["card_id"])
            log.info("purged unknown issuer_id record: %s  (name=%r)", c["card_id"], name)
            deleted += 1
            continue
        # LLM extractions sometimes write uppercase/malformed issuer_id — purge them
        if not _LOWERCASE_ID_RE.match(iid):
            store.delete_card(c["card_id"])
            log.info("purged bad issuer_id record: %s  (issuer_id=%r name=%r)", c["card_id"], iid, name)
            deleted += 1
            continue
        if not _is_valid_card_name(name):
            store.delete_card(c["card_id"])
            log.info("purged garbage record: %s  (name=%r)", c["card_id"], name)
            deleted += 1
    log.info("purge done: %d invalid record(s) removed from DynamoDB", deleted)


# ── Process one page ──────────────────────────────────────────────────────────

def process_page(page: dict) -> list[dict]:
    """Extract card(s) from a fetched page via LLM and persist them."""
    cards_raw = extract_cards([page])

    if cards_raw is None:
        # All LLM providers quota-exhausted — don't mark source so it retries tomorrow
        log.warning("LLM quota exhausted for %s — will retry on next run", page["source_url"])
        return []

    out = []
    for c in cards_raw:
        name = (c.get("card_name") or "").strip()
        if not _is_valid_card_name(name):
            log.info("rejected bad card_name %r from %s", name, page["source_url"])
            continue
        c["raw_text_sha256"] = page["sha"]
        # Always trust the configured issuer from our YAML, not whatever the LLM returns.
        # LLMs hallucinate wrong bank names when a listing page shows competitor card ads.
        if page.get("issuer_id"):
            c["issuer_id"] = page["issuer_id"]
        if page.get("issuer_name"):
            c["issuer_name"] = page["issuer_name"]
        c.setdefault("issuer_id",   None)
        c.setdefault("issuer_name", None)
        c = stamp(ensure_card_id(c))
        existing = store.get_existing(c["card_id"])
        if existing:
            c["first_seen_at"] = existing.get("first_seen_at", c["first_seen_at"])
        store.upsert_card(c)
        out.append(c)

    store.mark_source(page["source_url"], sha=page["sha"], etag=page["etag"])
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    started = time.time()
    log.info("DynamoDB enabled=%s  (region=%s  cards=%s  sources=%s)",
             store._AWS_ENABLED, store.REGION, store._CARDS_NAME, store._SOURCES_NAME)
    if FORCE_REFRESH:
        log.info("FORCE_REFRESH=1 — purging invalid DDB records and bypassing SHA cache")
        purge_invalid_cards()
    urls    = gather_urls()
    log.info("processing %d urls%s",
             len(urls),
             " (TEST_URLS)" if TEST_URLS else f" (TEST_RUN={TEST_RUN})" if TEST_RUN else "")

    all_cards: list[dict] = []

    for i, row in enumerate(urls, 1):
        if MAX_CARDS and len(all_cards) >= MAX_CARDS:
            log.info("MAX_CARDS=%d reached — stopping early", MAX_CARDS)
            break

        log.info("[%d/%d] %s", i, len(urls), row["url"])
        try:
            page = fetch_page(row)
        except Exception as e:
            log.exception("fetch error %s: %s", row["url"], e)
            page = None

        if page:
            try:
                cards = process_page(page)
                all_cards.extend(cards)
                log.info("  → %d card(s) extracted  (total so far: %d)", len(cards), len(all_cards))
                # Write incrementally so the file is useful even if the run is interrupted
                (OUT / "cards.json").write_text(
                    json.dumps(dedupe(all_cards), indent=2, default=str))
            except Exception as e:
                log.exception("parse error %s: %s", row["url"], e)

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("done. %d unique cards in %.1fs", len(final), time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())