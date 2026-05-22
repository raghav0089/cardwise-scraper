"""Orchestrator — invoked by GitHub Actions once a day.

Stages:
  1. issuer-scrape — every configured issuer's listing & detail pages
  2. discovery     — finance blogs/news for newly-launched or devalued cards
  3. Batch fetch   — download, hash check, and archive raw html
  4. Batch extract — process pages via multi-document LLM queries and update DB
"""
from __future__ import annotations
import os, json, logging, time, sys
from pathlib import Path
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


def gather_urls() -> list[dict]:
    mode = os.getenv("RUN_MODE", "all")
    if mode == "single":
        url = os.environ["SINGLE_URL"]
        return [{"url": url, "issuer_id": None, "issuer_name": None}]
    urls: list[dict] = []
    if mode in ("all", "issuers"):
        urls += collect_detail_urls()
    if mode in ("all", "discover"):
        urls += discover_candidate_urls()
    
    # dedupe by url, keep first hint
    seen, uniq = set(), []
    for r in urls:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    return uniq


def main() -> int:
    started = time.time()
    urls = gather_urls()
    log.info("processing %d urls", len(urls))
    
    # For a quick initial test, take only the first 6 candidate links
    test_queue = urls[:6]
    log.info("Running a targeted validation test using %d links", len(test_queue))
    
    all_cards: list[dict] = []
    
    # Drop batch size down to 2 for verification
    chunk_size = 2
    
    # Mocking out the payload structuring expected by extract_cards()
    extraction_queue = [
        {"source_url": row["url"], "issuer_id": row.get("issuer_id"), "markdown": "Sample test content"} 
        for row in test_queue
    ]

    for i in range(0, len(extraction_queue), chunk_size):
        current_chunk = extraction_queue[i : i + chunk_size]
        log.info("Processing test extraction slice [%d-%d/%d]", i, i + len(current_chunk), len(extraction_queue))
        
        try:
            # Fire batch request to Gemini API
            extracted_batch = extract_cards(current_chunk)
            
            for c in extracted_batch:
                ensure_card_id(c)
                stamp(c)
                existing = store.get_existing(c["card_id"])
                if existing:
                    for change in diff_card(existing, c):
                        store.record_change(change)
                else:
                    store.record_change({
                        "change_id": f"{c['card_id']}#new#{c['last_scraped_at']}",
                        "card_id": c["card_id"], "field": "_new",
                        "old_value": None, "new_value": c["card_name"],
                        "change_type": "new_card",
                        "detected_at": c["last_scraped_at"],
                        "source_url": c["source_url"],
                    })
                store.upsert_card(c)
                all_cards.append(c)

            # Mark processed on success
            for page_meta in current_chunk:
                store.mark_source(page_meta["source_url"], sha="test_sha", etag="test_etag")
                
        except Exception as e:
            log.exception("Validation test hit an error on this slice: %s", e)
            
        finally:
            # This ensures that even if Gemini rejects a request or the script crashes,
            # it pauses here to preserve your 15 requests-per-minute (RPM) barrier.
            log.info("Throttling pipeline for quota safety... sleeping 15 seconds.")
            time.sleep(15.0)

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("Test run complete. Extracted %d records.", len(final))
    return 0


if __name__ == "__main__":
    sys.exit(main())