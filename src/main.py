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
        if r["url"] in seen: continue
        seen.add(r["url"]); uniq.append(r)
    return uniq


def main() -> int:
    started = time.time()
    urls = gather_urls()
    log.info("processing %d urls", len(urls))
    
    # STAGE 1: Fast URL fetch and caching cycle
    fetched_pages: list[dict] = []
    
    for i, row in enumerate(urls, 1):
        log.info("[%d/%d] Fetching raw data: %s", i, len(urls), row["url"])
        try:
            url = row["url"]
            md, html, status, etag = fetch(url)
            
            if not md or len(md) < 200:
                continue
                
            sha = sha256(md or html or "")
            if store.source_unchanged(url, sha):
                log.info("unchanged, skip: %s", url)
                continue
                
            archive = store.archive_raw(url, html)
            
            # Store page attributes in execution memory state for batching
            fetched_pages.append({
                "markdown": md,
                "source_url": url,
                "issuer_id": row.get("issuer_id"),
                "issuer_name": row.get("issuer_name"),
                "archive": archive,
                "sha": sha,
                "etag": etag
            })
            
        except Exception as e:
            log.exception("Fetch execution failed for %s: %s", row["url"], e)
        time.sleep(0.5)  # Politeness threshold between domains

    # STAGE 2: Execute batch token extractions (Chunk Size: 15 URLs per Gemini Call)
    BATCH_SIZE = 15
    all_cards: list[dict] = []
    
    log.info("Starting structural batch extractions for %d updated pages...", len(fetched_pages))
    for i in range(0, len(fetched_pages), BATCH_SIZE):
        current_chunk = fetched_pages[i : i + BATCH_SIZE]
        log.info("Processing extraction slice [%d-%d/%d]", i, i + len(current_chunk), len(fetched_pages))
        
        # This invokes our batch implementation inside extract.py
        batch_cards = extract_cards(current_chunk)
        
        # Post-process extracted entities
        for c in batch_cards:
            # Find matching configuration from our runtime page cache
            page_meta = next((p for p in current_chunk if p["source_url"] == c["source_url"]), None)
            if not page_meta:
                continue
                
            c["source_archive_s3"] = page_meta["archive"]
            c["raw_text_sha256"]   = page_meta["sha"]
            c = stamp(ensure_card_id(c))

            existing = store.get_existing(c["card_id"])
            if existing:
                c["first_seen_at"] = existing.get("first_seen_at", c["first_seen_at"])
                for change in diff_card(existing, c):
                    store.record_change(change)
                    log.info("CHANGE %s :: %s :: %s → %s", change["card_id"],
                             change["field"], change["old_value"], change["new_value"])
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

        # Mark source targets completely processed inside persistence layers
        for page_meta in current_chunk:
            store.mark_source(page_meta["source_url"], sha=page_meta["sha"], etag=page_meta["etag"])
            
        time.sleep(2.0)  # Rate-limiting cushion between API calls

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("done. %d unique cards in %.1fs", len(final), time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())