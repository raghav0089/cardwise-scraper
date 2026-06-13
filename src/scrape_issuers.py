"""Issuer job — for every configured issuer, hit its list pages, harvest
detail URLs that look like product pages, and return them for extraction.

If an issuer has a `sitemap_url`, its card URLs are extracted directly from
the sitemap (no JS-rendered listing pages needed). list_urls are still fetched
for their pre-rendered content so the LLM can do a listing-page extraction pass.
"""
from __future__ import annotations
import os, re, logging, yaml
from pathlib import Path
from urllib.parse import urlparse
import requests as _requests

from .fetch import fetch, harvest_links, UA

log = logging.getLogger(__name__)
CFG = yaml.safe_load(Path("config/issuers.yaml").read_text())
MAX_URLS_PER_ISSUER = int(os.getenv("MAX_URLS_PER_ISSUER", "150"))


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc.split(":")[0].lstrip("www.") == \
           urlparse(b).netloc.split(":")[0].lstrip("www.")


def _fetch_sitemap_urls(sitemap_url: str, pattern: re.Pattern) -> list[str]:
    """Download a sitemap.xml and return all <loc> URLs matching the pattern."""
    try:
        r = _requests.get(sitemap_url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("sitemap fetch failed %s: %s", sitemap_url, e)
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    matched = [u.strip() for u in locs if pattern.search(u.strip())]
    log.info("sitemap %s → %d/%d URLs match pattern", sitemap_url, len(matched), len(locs))
    return matched


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

        n_list = len(issuer.get("list_urls") or [])
        sitemap = issuer.get("sitemap_url", "")
        log.info("  [%d/%d] %s — %s",
                 issuer_idx, len(issuers), issuer["name"],
                 f"sitemap + {n_list} list page(s)" if sitemap else f"{n_list} list page(s)")

        # ── Sitemap-based URL discovery ───────────────────────────────────────
        if sitemap:
            for u in _fetch_sitemap_urls(sitemap, pat):
                if u not in added:
                    added.add(u)
                    out.append({
                        "url":        u,
                        "issuer_id":  issuer["id"],
                        "issuer_name":issuer["name"],
                        "issuer_type":issuer["type"],
                    })
            log.info("    sitemap contributed %d card URLs", len(added))

        # ── List-page HTML harvest ────────────────────────────────────────────
        for list_url in (issuer.get("list_urls") or []):
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
                    "prefetched_html": html,
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
