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


def collect_detail_urls() -> list[dict]:
    out: list[dict] = []
    for issuer in CFG["issuers"]:
        pat   = re.compile(issuer.get("detail_link_pattern") or r".*card", re.I)
        cap   = issuer.get("max_urls", MAX_URLS_PER_ISSUER)
        added: set[str] = set()

        for list_url in issuer["list_urls"]:
            # Always include the listing page itself
            if list_url not in added:
                added.add(list_url)
                out.append({
                    "url": list_url,
                    "issuer_id": issuer["id"],
                    "issuer_name": issuer["name"],
                    "issuer_type": issuer["type"],
                    "is_listing": True,
                })

            try:
                _, html, _, _ = fetch(list_url)
            except Exception as e:
                log.warning("list fetch failed %s: %s", list_url, e); continue

            links = harvest_links(html, list_url)
            for u in links:
                if len(added) >= cap: break
                if not _same_site(u, list_url): continue
                if not pat.search(u): continue
                if u in added: continue
                added.add(u)
                out.append({
                    "url": u,
                    "issuer_id": issuer["id"],
                    "issuer_name": issuer["name"],
                    "issuer_type": issuer["type"],
                })

    # global dedupe across issuers
    seen, uniq = set(), []
    for r in out:
        if r["url"] in seen: continue
        seen.add(r["url"]); uniq.append(r)
    log.info("issuer job: %d detail URLs to scrape", len(uniq))
    return uniq
