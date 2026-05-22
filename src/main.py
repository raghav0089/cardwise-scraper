"""Orchestrator — invoked by GitHub Actions once a day.

Stages:
  1. issuer-scrape — every configured issuer's listing & detail pages
  2. discovery     — finance blogs/news for newly-launched or devalued cards
  3. for each URL: fetch → archive HTML to S3 → LLM extract → normalize
  4. diff vs DDB and upsert; write change rows to cards_changes
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


def process(url_row: dict) -> list[dict]:
    url = url_row["url"]
    try:
        md, html, status, etag = fetch(url)
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e); return []

    sha = sha256(md or html or "")
    if store.source_unchanged(url, sha):
        log.info("unchanged, skip: %s", url); return []

    archive = store.archive_raw(url, html)
    cards = extract_cards(md, source_url=url,
                          issuer_id=url_row.get("issuer_id"),
                          issuer_name=url_row.get("issuer_name"))
    out = []
    for c in cards:
        c["source_archive_s3"] = archive
        c["raw_text_sha256"]   = sha
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
                "source_url": url,
            })
        store.upsert_card(c)
        out.append(c)

    store.mark_source(url, sha=sha, etag=etag)
    return out


def main() -> int:
    started = time.time()
    urls = gather_urls()
    log.info("processing %d urls", len(urls))
    all_cards: list[dict] = []
    for i, row in enumerate(urls, 1):
        log.info("[%d/%d] %s", i, len(urls), row["url"])
        try:
            all_cards.extend(process(row))
        except Exception as e:
            log.exception("processing error %s: %s", row["url"], e)
        time.sleep(0.5)   # be polite

    final = dedupe(all_cards)
    (OUT / "cards.json").write_text(json.dumps(final, indent=2, default=str))
    log.info("done. %d unique cards in %.1fs", len(final), time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
