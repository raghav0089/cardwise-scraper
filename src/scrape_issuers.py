"""Issuer job — for every configured issuer, hit its list pages, harvest
detail URLs that look like product pages, and return them for extraction.
"""
from __future__ import annotations
import os, re, logging, yaml
from pathlib import Path
from urllib.parse import urlparse
from .fetch import fetch, harvest_links

log = logging.getLogger(__name__)
CFG = yaml.safe_load(Path("config/issuers.yaml").read_text())
MAX_URLS_PER_ISSUER = int(os.getenv("MAX_URLS_PER_ISSUER", "50"))


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc.split(":")[0].lstrip("www.") == \
           urlparse(b).netloc.split(":")[0].lstrip("www.")


def collect_detail_urls(issuer_ids: set[str] | None = None) -> list[dict]:
    """Fetch each issuer's listing pages, harvest detail links, return all URLs.

    Each row has: url, issuer_id, issuer_name, issuer_type.
    Listing pages also carry prefetched_html so the main loop skips re-fetching.
    Pass issuer_ids to restrict to a subset (used by smoke-test mode).
    """
    issuers = CFG["issuers"]
    if issuer_ids:
        issuers = [i for i in issuers if i["id"] in issuer_ids]
    log.info("URL-gather phase: %d issuers%s",
             len(issuers),
             f" (filtered: {sorted(issuer_ids)})" if issuer_ids else "")

    out: list[dict] = []
    for issuer_idx, issuer in enumerate(issuers, 1):
        pat   = re.compile(issuer.get("detail_link_pattern") or r".*card", re.I)
        cap   = issuer.get("max_urls", MAX_URLS_PER_ISSUER)
        added: set[str] = set()

        log.info("  [%d/%d] %s — fetching %d list page(s)",
                 issuer_idx, len(issuers), issuer["name"], len(issuer["list_urls"]))

        for list_url in issuer["list_urls"]:
            result = fetch(list_url)
            html   = result.html if result and result.ok else ""

            # Always include the listing page itself (with pre-fetched content)
            if list_url not in added:
                added.add(list_url)
                out.append({
                    "url":             list_url,
                    "issuer_id":       issuer["id"],
                    "issuer_name":     issuer["name"],
                    "issuer_type":     issuer["type"],
                    "is_listing":      True,
                    "prefetched_html": html,   # avoids re-fetch in main loop
                })

            if not html:
                log.warning("    no content from %s — skipping link harvest", list_url)
                continue

            links = harvest_links(html, list_url)
            n_before = len(added)
            for u in links:
                if len(added) >= cap: break
                if not _same_site(u, list_url): continue
                if not pat.search(u): continue
                if u in added: continue
                added.add(u)
                out.append({
                    "url":        u,
                    "issuer_id":  issuer["id"],
                    "issuer_name":issuer["name"],
                    "issuer_type":issuer["type"],
                })
            log.info("    harvested %d detail links from %s", len(added) - n_before, list_url)

    # global dedupe across issuers
    seen, uniq = set(), []
    for r in out:
        if r["url"] in seen: continue
        seen.add(r["url"]); uniq.append(r)
    log.info("URL-gather done: %d total URLs to scrape", len(uniq))
    return uniq
