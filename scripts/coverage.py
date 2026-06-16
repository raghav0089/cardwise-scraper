"""Discovery coverage diagnostic.

For every issuer in config/issuers.yaml, run ONLY the URL-discovery phase
(sitemap match + list-page link harvest) and report how many card detail URLs
we can find — plus any fetch errors. No extraction, no DDB, no LLM, no paid APIs.

This tells us, per issuer, whether the bottleneck is *finding* cards (0 / too few
URLs → config problem) or something downstream. Run:

    python3 -m scripts.coverage           # all issuers
    python3 -m scripts.coverage hdfc sbi  # subset

Writes out/coverage.json and prints a sorted table.
"""
from __future__ import annotations
import sys, re, json, logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from pathlib import Path

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

import src.fetch as _fetchmod
_fetchmod.TIMEOUT = 8   # diagnostic: fail fast, don't wait 30s on dead URLs
from src.scrape_issuers import CFG, _fetch_sitemap_urls, _is_scrapeable, _same_site
from src.fetch import fetch, harvest_links

OUT = Path("out"); OUT.mkdir(exist_ok=True)
PER_ISSUER_BUDGET = 60   # seconds; a single slow issuer can't block the whole scan


def probe(issuer: dict) -> dict:
    pat = re.compile(issuer.get("detail_link_pattern") or r".*card", re.I)
    found: set[str] = set()
    errors: list[str] = []
    sitemap_n = 0

    # Explicit single-card detail pages count directly (no fetch needed).
    found.update(issuer.get("detail_urls") or [])

    sm = issuer.get("sitemap_url", "")
    if sm:
        try:
            urls = _fetch_sitemap_urls(sm, pat)
            sitemap_n = len(urls)
            found.update(urls)
        except Exception as e:
            errors.append(f"sitemap {sm}: {e}")

    for lu in (issuer.get("list_urls") or []):
        try:
            r = fetch(lu, prefer="requests")   # diagnostic: skip slow Jina, direct HTTP
            if not r.ok:
                errors.append(f"list {lu}: {r.error}")
                continue
            html = r.html or r.text or ""
            harvested = [u for u in harvest_links(html, lu)
                         if _same_site(u, lu) and pat.search(u) and _is_scrapeable(u)]
            found.update(harvested)
        except Exception as e:
            errors.append(f"list {lu}: {e}")

    return {
        "id": issuer["id"], "name": issuer["name"],
        "urls": len(found), "from_sitemap": sitemap_n,
        "has_sitemap": bool(sm), "n_list": len(issuer.get("list_urls") or []),
        "errors": errors[:4],
        "sample": sorted(found)[:5],
    }


def main(argv: list[str]) -> int:
    issuers = CFG["issuers"]
    if argv:
        want = set(argv)
        issuers = [i for i in issuers if i["id"] in want]
    # Merge with any existing results so batched/resumed runs accumulate.
    prev = {}
    cov = OUT / "coverage.json"
    if cov.exists():
        try:
            prev = {x["id"]: x for x in json.loads(cov.read_text())}
        except Exception:
            prev = {}

    def flush():
        merged = {**prev, **{r["id"]: r for r in results}}
        cov.write_text(json.dumps(list(merged.values()), indent=2))

    results = []
    pool = ThreadPoolExecutor(max_workers=1)
    for idx, issuer in enumerate(issuers, 1):
        fut = pool.submit(probe, issuer)
        try:
            res = fut.result(timeout=PER_ISSUER_BUDGET)
        except FTimeout:
            res = {"id": issuer["id"], "name": issuer["name"], "urls": 0,
                   "from_sitemap": 0, "has_sitemap": bool(issuer.get("sitemap_url")),
                   "n_list": len(issuer.get("list_urls") or []),
                   "errors": [f"TIMEOUT >{PER_ISSUER_BUDGET}s"], "sample": []}
            pool = ThreadPoolExecutor(max_workers=1)   # abandon the stuck worker
        except Exception as e:
            res = {"id": issuer["id"], "name": issuer["name"], "urls": 0,
                   "from_sitemap": 0, "has_sitemap": bool(issuer.get("sitemap_url")),
                   "n_list": len(issuer.get("list_urls") or []),
                   "errors": [f"probe crashed: {e}"], "sample": []}
        results.append(res)
        flag = "ZERO" if res["urls"] == 0 else f"{res['urls']:4d}"
        note = ""
        if res["urls"] == 0 and res["errors"]:
            note = " | " + res["errors"][0][:90]
        print(f"[{idx:3d}/{len(issuers)}] {flag}  {res['id']:26s}{note}", flush=True)
        flush()   # persist incrementally so a stall/kill never loses progress
    zero = [r["id"] for r in results if r["urls"] == 0]
    low  = [r["id"] for r in results if 0 < r["urls"] <= 2]
    print(f"\n=== {len(results)} issuers | ZERO: {len(zero)} | 1-2 urls: {len(low)} ===")
    print("ZERO:", ", ".join(zero))
    print("LOW :", ", ".join(low))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
