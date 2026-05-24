"""Orchestrator — invoked by GitHub Actions once a day.

Stages:
  1. issuer-scrape — every configured issuer's listing & detail pages
  2. discovery     — finance blogs/news for newly-launched or devalued cards
  3. fetch + hash-check each URL, archive HTML to S3
  4. batch extract via Gemini → normalize → diff → upsert

Key behaviours:
  - URLs are filtered BEFORE fetching (extensions, off-topic domains, known junk paths)
  - Gemini quota exhaustion triggers a circuit breaker: remaining fetches are
    skipped so we don't burn Jina calls pointlessly
  - GEMINI_RPM env var throttles calls to stay within free-tier rate limits
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
_GEMINI_MIN_INTERVAL = 60.0 / GEMINI_RPM
_last_gemini_call    = 0.0

# ── URL filters ───────────────────────────────────────────────────────────────

# File extensions that are never card content
_JUNK_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico",
    ".pdf", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
    ".css", ".js", ".json", ".xml",
})

# Domains we never want to send to the LLM (CDNs, image hosts, unrelated news)
_JUNK_DOMAINS = frozenset({
    "b-cdn.net", "wp.com", "bsmedia.business-standard.com",
    "static.bankbazaar.com", "images.moneycontrol.com",
    "stat1.moneycontrol.com", "img.etimg.com",
    "rupay.co.in",          # network promo pages, not card products
    "npci.org.in",          # always times out, not a card issuer page
})

# Path segments that indicate non-product pages
_SKIP_PATH_RE = re.compile(
    r"/(compare|offers?|loan-on|balance-on|other-benefits|"
    r"faq|apply|eligibility|fees?|charges?|contact|login|register|"
    r"sitemap|privacy|terms|legal|press|careers?|support|help|"
    r"download|app|lounge|festive|dearness|personal-loan|"
    r"debit-card[/-]?$|bill-payment|customer-care|"
    r"negative-balance|secured-credit-card$|"
    r"different-types|top-10|lounge-access|"
    r"zero-forex-markup|best-lifetime|best-international|"
    r"best-secured|travel-credit-cards$)(/|$)",
    re.IGNORECASE,
)

# Discovery sources we trust to have card-product links (others are too noisy)
_ALLOWED_DISCOVERY_DOMAINS = frozenset({
    "cardexpert.in", "cardinsider.com", "cardinside.in",
    "technofino.in", "bankbazaar.com", "paisabazaar.com",
    "economictimes.indiatimes.com", "livemint.com",
    "business-standard.com", "moneycontrol.com",
    "financialexpress.com", "ndtv.com", "hindustantimes.com",
})


def _should_fetch(url: str, is_discovery: bool = False) -> bool:
    """Return False for URLs we know are junk before even hitting Jina."""
    parsed = urlparse(url)
    host   = parsed.hostname or ""
    path   = parsed.path.lower()

    # reject by extension
    ext = path.rsplit(".", 1)[-1] if "." in path.split("/")[-1] else ""
    if f".{ext}" in _JUNK_EXTENSIONS:
        return False

    # reject known junk domains
    root = ".".join(host.split(".")[-2:]) if host else ""
    if root in _JUNK_DOMAINS or host in _JUNK_DOMAINS:
        return False

    # reject known junk paths
    if _SKIP_PATH_RE.search(parsed.path):
        return False

    # for discovery URLs, only trust known good domains
    if is_discovery and root not in _ALLOWED_DISCOVERY_DOMAINS:
        log.debug("skip unknown discovery domain: %s", url)
        return False

    return True


# ── Gemini circuit breaker ────────────────────────────────────────────────────

_gemini_consecutive_failures = 0
_GEMINI_FAILURE_THRESHOLD    = 3   # stop trying after this many consecutive 429s


def _gemini_wait() -> None:
    global _last_gemini_call
    now = time.monotonic()
    gap = now - _last_gemini_call
    if gap < _GEMINI_MIN_INTERVAL:
        time.sleep(_GEMINI_MIN_INTERVAL - gap)
    _last_gemini_call = time.monotonic()


def _gemini_dead() -> bool:
    return _gemini_consecutive_failures >= _GEMINI_FAILURE_THRESHOLD


# ── URL gathering ─────────────────────────────────────────────────────────────

def gather_urls() -> list[dict]:
    mode = os.getenv("RUN_MODE", "all")
    if mode == "single":
        return [{"url": os.environ["SINGLE_URL"], "issuer_id": None,
                 "issuer_name": None, "is_discovery": False}]
    urls: list[dict] = []
    if mode in ("all", "issuers"):
        for r in collect_detail_urls():
            r["is_discovery"] = False
            urls.append(r)
    if mode in ("all", "discover"):
        for r in discover_candidate_urls():
            r["is_discovery"] = True
            urls.append(r)
    seen, uniq = set(), []
    for r in urls:
        if r["url"] not in seen:
            seen.add(r["url"]); uniq.append(r)
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
    global _gemini_consecutive_failures

    if not batch:
        return []

    _gemini_wait()
    log.info("LLM batch: %d pages → 1 call", len(batch))

    cards = extract_cards(batch)

    if cards is None:
        _gemini_consecutive_failures += 1
        log.warning("Gemini error #%d/%d — sources NOT marked",
                    _gemini_consecutive_failures, _GEMINI_FAILURE_THRESHOLD)
        if _gemini_dead():
            log.error(
                "Gemini quota exhausted (%d consecutive failures). "
                "Stopping LLM calls for this run. "
                "Remaining URLs will be retried tomorrow.",
                _gemini_consecutive_failures,
            )
        return []

    _gemini_consecutive_failures = 0   # reset on success

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
                    "card_id":     c["card_id"],
                    "field":       "_new",
                    "old_value":   None,
                    "new_value":   c.get("card_name"),
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
    log.info("gathered %d candidate urls (batch=%d, rpm=%d)",
             len(urls), BATCH_SIZE, GEMINI_RPM)

    all_cards: list[dict] = []
    batch:     list[dict] = []

    for i, row in enumerate(urls, 1):
        # Stop fetching entirely once Gemini is confirmed dead for this run
        if _gemini_dead() and not batch:
            log.warning("circuit breaker open — skipping remaining %d urls", len(urls) - i + 1)
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