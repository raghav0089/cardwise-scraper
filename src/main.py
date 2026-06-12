"""Orchestrator — invoked by GitHub Actions once a day.

Set TEST_URLS=1 to run against a hardcoded small list (bypasses issuer crawl).
Set TEST_RUN=N to take first N URLs from the normal issuer list.
"""
from __future__ import annotations
import os, json, logging, time, sys, re
from pathlib import Path
from urllib.parse import urlparse
from .fetch import fetch, sha256
from .scrape_issuers import collect_detail_urls
from .discover import discover_candidate_urls
from .parse import parse_cards
from .normalize import ensure_card_id, stamp, dedupe
from . import store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("main")
OUT = Path("out"); OUT.mkdir(exist_ok=True)

TEST_RUN  = int(os.getenv("TEST_RUN", "0"))
TEST_URLS = os.getenv("TEST_URLS", "0") == "1"
MAX_CARDS = int(os.getenv("MAX_CARDS", "0"))   # 0 = no limit

# Hardcoded smoke-test URLs — one from each major issuer type
_SMOKE_TEST_URLS = [
    {"url": "https://www.hdfcbank.com/personal/pay/cards/credit-cards",        "issuer_id": "hdfc",       "issuer_name": "HDFC Bank",         "is_discovery": False},
    {"url": "https://www.icicibank.com/personal-banking/cards/consumer-cards/credit-card", "issuer_id": "icici", "issuer_name": "ICICI Bank", "is_discovery": False},
    {"url": "https://www.axisbank.com/retail/cards/credit-card",               "issuer_id": "axis",       "issuer_name": "Axis Bank",         "is_discovery": False},
    {"url": "https://www.sbicard.com/en/personal/credit-cards.page",           "issuer_id": "sbi_card",   "issuer_name": "SBI Card",          "is_discovery": False},
    {"url": "https://www.kotak.com/en/personal-banking/cards/credit-cards.html","issuer_id": "kotak",     "issuer_name": "Kotak Mahindra Bank","is_discovery": False},
    {"url": "https://www.sc.com/in/credit-cards/",                             "issuer_id": "standard_chartered", "issuer_name": "Standard Chartered", "is_discovery": False},
    {"url": "https://www.getonecard.app/",                                     "issuer_id": "onecard",    "issuer_name": "OneCard",           "is_discovery": False},
    {"url": "https://www.scapia.cards/",                                       "issuer_id": "scapia",     "issuer_name": "Scapia",            "is_discovery": False},
    {"url": "https://www.axisbank.com/retail/cards/credit-card/flipkart-axis-bank-credit-card", "issuer_id": "flipkart_axis", "issuer_name": "Flipkart Axis", "is_discovery": False},
    {"url": "https://www.hdfcbank.com/personal/pay/cards/credit-cards/swiggy-hdfc-bank-credit-card", "issuer_id": "swiggy_hdfc", "issuer_name": "Swiggy HDFC", "is_discovery": False},
]

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
})
# Sub-string match within a segment (catches compound names like terms-and-conditions)
_SKIP_SEG_RE = re.compile(
    r"emi|terms|fee|charge|privacy|personal-loan|bill-pay|"
    r"customer-care|negative-balance|do-not-call|credit-builder|"
    r"credit-card-service|different-type|top-\d|lounge-access|"
    r"zero-forex|best-lifetime|best-international|best-secured|"
    r"travel-credit-card|loan-on|balance-on",
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
        log.info("TEST_URLS mode: using %d hardcoded smoke-test URLs", len(_SMOKE_TEST_URLS))
        return _SMOKE_TEST_URLS

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

def fetch_page(row: dict) -> dict | None:
    url          = row["url"]
    is_discovery = row.get("is_discovery", False)

    if not _should_fetch(url, is_discovery=is_discovery):
        log.debug("pre-filter skip: %s", url)
        return None

    result = fetch(url)
    if not result.ok:
        log.warning("fetch failed %s: %s", url, result.error)
        return None

    text = result.text or result.html or ""
    if len(text) < 200:
        log.debug("skip near-empty page: %s", url)
        return None

    sha = sha256(text)
    if store.source_unchanged(url, sha):
        log.info("unchanged, skip: %s", url)
        return None

    store.archive_raw(url, result.html)
    return {
        "source_url":  url,
        "issuer_id":   row.get("issuer_id"),
        "issuer_name": row.get("issuer_name"),
        "markdown":    text,
        "sha":         sha,
        "etag":        result.etag,
    }


# ── Process one page ─────────────────────────────────────────────────────────

def process_page(page: dict) -> list[dict]:
    """Parse a fetched page and persist the extracted card(s)."""
    cards_raw = parse_cards(page)

    out = []
    for c in cards_raw:
        c["raw_text_sha256"] = page["sha"]
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