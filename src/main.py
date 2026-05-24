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
from .extract import extract_cards
from .normalize import ensure_card_id, stamp, dedupe
from .diff import diff_card
from . import store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("main")
OUT = Path("out"); OUT.mkdir(exist_ok=True)

BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "10"))
GEMINI_RPM = int(os.getenv("GEMINI_RPM", "30"))
TEST_RUN   = int(os.getenv("TEST_RUN", "0"))
TEST_URLS  = os.getenv("TEST_URLS", "0") == "1"

_GEMINI_MIN_INTERVAL = 60.0 / GEMINI_RPM
_last_gemini_call    = 0.0

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
_SKIP_PATH_RE = re.compile(
    r"/(compare|offers?|loan-on|balance-on|other-benefits|faq|apply|"
    r"eligibility|fees?|charges?|contact|login|register|sitemap|privacy|"
    r"terms|legal|press|careers?|support|help|download|app|lounge|festive|"
    r"personal-loan|bill-payment|customer-care|negative-balance|"
    r"different-types|top-10|lounge-access|zero-forex-markup|"
    r"best-lifetime|best-international|best-secured|travel-credit-cards$)(/|$)",
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
    if _SKIP_PATH_RE.search(parsed.path): return False
    if is_discovery and root not in _ALLOWED_DISCOVERY_DOMAINS: return False
    return True


# ── Gemini circuit breaker ────────────────────────────────────────────────────

_gemini_failures  = 0
_GEMINI_THRESHOLD = 3

def _gemini_wait() -> None:
    global _last_gemini_call
    now = time.monotonic()
    gap = now - _last_gemini_call
    if gap < _GEMINI_MIN_INTERVAL:
        time.sleep(_GEMINI_MIN_INTERVAL - gap)
    _last_gemini_call = time.monotonic()

def _gemini_dead() -> bool:
    return _gemini_failures >= _GEMINI_THRESHOLD


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


# ── LLM flush ────────────────────────────────────────────────────────────────

def flush_batch(batch: list[dict]) -> list[dict]:
    global _gemini_failures
    if not batch:
        return []

    _gemini_wait()
    log.info("LLM batch: %d pages → 1 call", len(batch))
    cards = extract_cards(batch)

    if cards is None:
        _gemini_failures += 1
        log.warning("Gemini error #%d/%d — sources NOT marked",
                    _gemini_failures, _GEMINI_THRESHOLD)
        if _gemini_dead():
            log.error("Gemini quota exhausted — stopping LLM calls for this run.")
        return []

    _gemini_failures = 0

    if not cards:
        for page in batch:
            store.mark_source(page["source_url"], sha=page["sha"], etag=page["etag"])
        return []

    by_url: dict[str, list] = {p["source_url"]: [] for p in batch}
    for c in cards:
        src = c.get("source_url") or batch[0]["source_url"]
        by_url.setdefault(src, []).append(c)

    out = []
    for page in batch:
        url = page["source_url"]
        for c in by_url.get(url, []):
            c["raw_text_sha256"] = page["sha"]
            c = stamp(ensure_card_id(c))
            existing = store.get_existing(c["card_id"])
            if existing:
                c["first_seen_at"] = existing.get("first_seen_at", c["first_seen_at"])
                for change in diff_card(existing, c):
                    store.record_change(change)
                    log.info("CHANGE %s :: %s :: %s → %s",
                             change["card_id"], change["field"],
                             change["old_value"], change["new_value"])
            else:
                store.record_change({
                    "change_id":   f"{c['card_id']}#new#{c['last_scraped_at']}",
                    "card_id":     c["card_id"], "field": "_new",
                    "old_value":   None, "new_value": c.get("card_name"),
                    "change_type": "new_card",
                    "detected_at": c["last_scraped_at"],
                    "source_url":  url,
                })
            store.upsert_card(c)
            out.append(c)
        store.mark_source(url, sha=page["sha"], etag=page["etag"])

    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    started = time.time()
    urls    = gather_urls()
    log.info("processing %d urls (batch=%d%s)",
             len(urls), BATCH_SIZE,
             ", TEST_URLS" if TEST_URLS else f", TEST_RUN={TEST_RUN}" if TEST_RUN else "")

    all_cards: list[dict] = []
    batch:     list[dict] = []

    for i, row in enumerate(urls, 1):
        if _gemini_dead() and not batch:
            log.warning("circuit breaker — skipping remaining %d urls", len(urls) - i + 1)
            break

        log.info("[%d/%d] %s", i, len(urls), row["url"])
        try:
            page = fetch_page(row)
        except Exception as e:
            log.exception("fetch error %s: %s", row["url"], e)
            page = None

        if page:
            batch.append(page)

        if len(batch) >= BATCH_SIZE or (i == len(urls) and batch):
            try:
                all_cards.extend(flush_batch(batch))
            except Exception as e:
                log.exception("flush error: %s", e)
            batch = []

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("done. %d unique cards in %.1fs", len(final), time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())