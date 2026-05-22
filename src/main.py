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
    log.info("Gathered %d candidate target URLs", len(urls))

    # Phase 1: Fetch and determine state hashes
    targets: list[dict] = []
    for row in urls:
        url = row["url"]
        try:
            md, html, code, etag = fetch(url)
            if code != 200 or not md:
                continue
            
            sha = sha256(md)
            if store.source_unchanged(url, sha):
                log.info("Unchanged checkpoint skip: %s", url)
                continue

            store.archive_raw(url, html)
            targets.append({
                "url": url, "markdown": md, "sha": sha, "etag": etag,
                "issuer_id": row.get("issuer_id"), "issuer_name": row.get("issuer_name")
            })
        except Exception as e:
            log.warning("Fetch preparation step failed for %s: %s", url, e)

    log.info("Pending extraction stack queue size: %d", len(targets))
    all_cards: list[dict] = []
    chunk_size = 15

    # Phase 2: Chunked batch extraction and transactional updates
    for i in range(0, len(targets), chunk_size):
        current_chunk = targets[i:i + chunk_size]
        log.info("Processing extraction slice [%d-%d/%d]", i, i + len(current_chunk), len(targets))

        chunk_payloads = []
        for page_meta in current_chunk:
            chunk_payloads.append({
                "markdown": page_meta["markdown"],
                "source_url": page_meta["url"],
                "issuer_id": page_meta["issuer_id"],
                "issuer_name": page_meta["issuer_name"]
            })

        batch_cards = extract_cards(chunk_payloads)

        # Check if the execution task explicitly errored out (returned None)
        if batch_cards is None:
            log.error("Batch slice extraction processing encountered a validation crash. Skipping source checkpoint updates for automatic retry tomorrow.")
            continue

        # If it succeeded (even if it returned empty []), process the results and commit states safely
        for c in batch_cards:
            c = stamp(ensure_card_id(c))
            url = c["source_url"]

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
                    "source_url": url,
                })
            store.upsert_card(c)
            all_cards.append(c)

        # Extraction completed successfully for this entire slice block, mark them as done
        for page_meta in current_chunk:
            store.mark_source(page_meta["url"], sha=page_meta["sha"], etag=page_meta["etag"])

        time.sleep(2.0)  # Safe rate-limiting cooldown padding

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("Pipeline lifecycle sequence completed. Processed %d entries in %.1fs", len(final), time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())