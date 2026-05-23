"""Orchestrator — invoked by GitHub Actions once a day.

Stages:
  1. issuer-scrape — every configured issuer's listing & detail pages
  2. discovery     — finance blogs/news for newly-launched or devalued cards
  3. fetch + hash-check each URL, archive HTML to S3
  4. batch extract via Gemini (10 pages per call) → normalize → diff → upsert

Rate limiting:
  - Jina:   3.5s between calls (enforced in fetch.py, ~17 req/min)
  - Gemini: GEMINI_RPM cap enforced here via token-bucket sleep
            gemini-2.0-flash-lite free tier = 30 req/min
            Set GEMINI_RPM=30 (default) or lower if still hitting 429s
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

BATCH_SIZE  = int(os.getenv("LLM_BATCH_SIZE", "10"))
GEMINI_RPM  = int(os.getenv("GEMINI_RPM", "30"))          # req/min free tier cap
_GEMINI_MIN_INTERVAL = 60.0 / GEMINI_RPM                   # seconds between calls
_last_gemini_call    = 0.0

_SKIP_RE = re.compile(
    r"/(compare|offers?|rewards?|loan-on|balance-on|other-benefits|"
    r"features?|faq|apply|eligibility|fees?|charges?|contact|"
    r"login|register|sitemap|privacy|terms|legal|news|blog|press|"
    r"about|careers?|support|help|download|app)(/|$)",
    re.IGNORECASE,
)

def _should_extract(url: str) -> bool:
    return not bool(_SKIP_RE.search(urlparse(url).path))


def _gemini_wait() -> None:
    """Token-bucket throttle — ensures we never exceed GEMINI_RPM."""
    global _last_gemini_call
    now = time.monotonic()
    gap = now - _last_gemini_call
    if gap < _GEMINI_MIN_INTERVAL:
        sleep_for = _GEMINI_MIN_INTERVAL - gap
        log.debug("gemini throttle: sleeping %.1fs", sleep_for)
        time.sleep(sleep_for)
    _last_gemini_call = time.monotonic()


def gather_urls() -> list[dict]:
    mode = os.getenv("RUN_MODE", "all")
    if mode == "single":
        return [{"url": os.environ["SINGLE_URL"], "issuer_id": None, "issuer_name": None}]
    urls: list[dict] = []
    if mode in ("all", "issuers"):
        urls += collect_detail_urls()
    if mode in ("all", "discover"):
        urls += discover_candidate_urls()
    seen, uniq = set(), []
    for r in urls:
        if r["url"] not in seen:
            seen.add(r["url"]); uniq.append(r)
    return uniq


def fetch_page(row: dict) -> dict | None:
    """Fetch one URL. Returns page dict for batching, or None to skip."""
    url = row["url"]
    if not _should_extract(url):
        log.debug("skip non-product URL: %s", url)
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


def flush_batch(batch: list[dict]) -> list[dict]:
    """Send a batch of pages to Gemini, persist results, return card list."""
    if not batch:
        return []

    _gemini_wait()
    log.info("LLM batch: %d pages → 1 call", len(batch))

    cards = extract_cards(batch)
    if cards is None:
        log.warning("batch returned None (API error) — sources NOT marked; will retry tomorrow")
        return []
    if not cards:
        # success but no cards found — still mark sources so we don't re-scrape
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


def main() -> int:
    started = time.time()
    urls    = gather_urls()
    log.info("gathered %d urls (batch=%d, gemini_rpm=%d)", len(urls), BATCH_SIZE, GEMINI_RPM)

    all_cards: list[dict] = []
    batch:     list[dict] = []

    for i, row in enumerate(urls, 1):
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