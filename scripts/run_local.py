"""Resumable, local, full-coverage scrape runner.

Writes NOTHING to DynamoDB and never calls a paid LLM. Produces a complete local
dataset (out/cards_full.json) that we can validate card-by-card before any DDB
load. Designed to be driven in batches across many runs — progress is persisted
after every URL, so an interrupted/killed run just resumes.

    python -m scripts.run_local gather          # gather all card URLs once
    python -m scripts.run_local run [BATCH]      # process next BATCH urls (default 60)
    python -m scripts.run_local status           # show progress + per-issuer counts

Env:
    ENRICH_WITH_LLM=1   also run the free local Ollama depth pass (slower)
"""
from __future__ import annotations
import os
# Disable DDB/S3 before importing store-touching modules (store no-ops without a region).
os.environ["AWS_REGION"] = ""
os.environ.setdefault("ALLOW_PAID_LLM", "0")

import sys, json, time
from collections import Counter
from pathlib import Path

from src.scrape_issuers import collect_detail_urls
from src.main import fetch_page, _is_valid_card_name
from src.extract import extract_cards
from src.normalize import ensure_card_id, stamp, dedupe

OUT = Path("out"); OUT.mkdir(exist_ok=True)
URLS  = OUT / "urls.json"
DONE  = OUT / "done.json"
CARDS = OUT / "cards_full.json"


def _load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def gather() -> None:
    t0 = time.time()
    urls = collect_detail_urls()
    URLS.write_text(json.dumps(urls, indent=2))
    print(f"gathered {len(urls)} card URLs in {time.time()-t0:.0f}s -> {URLS}")


def run(batch: int) -> None:
    urls = _load(URLS, None)
    if urls is None:
        print("no urls.json — run `gather` first"); return
    done = set(_load(DONE, []))
    cards = _load(CARDS, [])
    todo = [r for r in urls if r["url"] not in done]
    print(f"{len(done)}/{len(urls)} done; {len(todo)} remaining; processing {min(batch,len(todo))}")

    def flush():
        DONE.write_text(json.dumps(sorted(done)))
        CARDS.write_text(json.dumps(dedupe(cards), indent=2, default=str))

    n = 0
    for r in todo[:batch]:
        try:
            page = fetch_page(r)
            if page:
                for c in (extract_cards([page]) or []):
                    if not _is_valid_card_name((c.get("card_name") or "")):
                        continue
                    c = stamp(ensure_card_id(c))
                    cards.append(c)
        except Exception as e:
            print(f"  ERR {r['url']}: {e}")
        done.add(r["url"]); n += 1
        if n % 10 == 0:
            flush(); print(f"  …{n}/{min(batch,len(todo))}  (cards={len(dedupe(cards))})", flush=True)
    flush()
    print(f"batch done: +{n} urls, {len(dedupe(cards))} unique cards, {len(done)}/{len(urls)} total")


def status() -> None:
    urls = _load(URLS, [])
    done = set(_load(DONE, []))
    cards = dedupe(_load(CARDS, []))
    print(f"urls={len(urls)}  processed={len(done)}  remaining={len(urls)-len(done)}  cards={len(cards)}")
    by = Counter(c.get("issuer_id") for c in cards)
    fee = sum(1 for c in cards if c.get("fees"))
    print(f"issuers with cards={len(by)}  cards-with-fees={fee}/{len(cards)}")
    for iid, n in by.most_common():
        print(f"   {n:4d}  {iid}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "gather":
        gather()
    elif cmd == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    elif cmd == "status":
        status()
    else:
        print(__doc__)
