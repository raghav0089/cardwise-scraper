"""(Re)build per-bank fallback URL files from a gathered URL set.

config/fallback_urls/<issuer_id>.txt holds every known card URL for an issuer,
one per line. Discovery (scrape_issuers) always merges these, so a card is never
missed when a sitemap/listing changes or breaks — the curated URL is fetched
directly (e.g. https://www.hdfc.bank.in/credit-cards/irctc-credit-card).

    python -m scripts.run_local gather      # produces out/urls.json
    python -m scripts.build_fallback        # urls.json -> config/fallback_urls/*.txt
"""
from __future__ import annotations
import json, re
from pathlib import Path
from collections import defaultdict

SRC = Path("out/urls.json")
DST = Path("config/fallback_urls"); DST.mkdir(parents=True, exist_ok=True)

# listing pages and non-card utility paths to exclude
_JUNK = re.compile(
    r'/(credit-cards?|debit-cards?|cards?|creditcard)/?$|'
    r'add-on-card|block-lost|loststolen|compare|/faq|/offers?$|membership-kit|'
    r'netbanking|/services?$|/support|terms-and-cond|-calculator|/category',
    re.I,
)


def main() -> None:
    if not SRC.exists():
        print("no out/urls.json — run `python -m scripts.run_local gather` first"); return
    rows = json.loads(SRC.read_text())
    by = defaultdict(set)
    for r in rows:
        if not _JUNK.search(r["url"]):
            by[r["issuer_id"]].add(r["url"])
    total = 0
    for iid, urls in by.items():
        (DST / f"{iid}.txt").write_text("\n".join(sorted(urls)) + "\n")
        total += len(urls)
    print(f"wrote {len(by)} fallback files, {total} card URLs -> {DST}/")


if __name__ == "__main__":
    main()
