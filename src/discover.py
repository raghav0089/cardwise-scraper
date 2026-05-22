"""Discovery job — crawl finance blogs/news to find newly-launched cards
or devaluation announcements, return candidate URLs to feed scrape_issuers.
"""
from __future__ import annotations
import re, logging, yaml, tldextract
from pathlib import Path
from .fetch import fetch, harvest_links

log = logging.getLogger(__name__)
CFG = yaml.safe_load(Path("config/discovery_sources.yaml").read_text())


def _looks_relevant(url: str, anchor_or_title: str, keywords: list[str]) -> bool:
    blob = f"{url} {anchor_or_title}".lower()
    return any(k.lower() in blob for k in keywords)


def discover_candidate_urls() -> list[dict]:
    seeds = CFG["sources"]
    kw = CFG["keywords"]
    max_pages = CFG["crawl"]["max_pages_per_source"]

    candidates: dict[str, dict] = {}
    for seed in seeds:
        try:
            md, html, _, _ = fetch(seed)
        except Exception as e:
            log.warning("seed fetch failed %s: %s", seed, e); continue

        links = harvest_links(html, seed)[:max_pages]
        for u in links:
            if _looks_relevant(u, u, kw):
                candidates.setdefault(u, {"url": u, "discovered_via": seed})

        # also scan markdown for inline mentions of newly-launched products
        for m in re.finditer(r"\[([^\]]{4,120})\]\((https?://[^\s)]+)\)", md or ""):
            anchor, link = m.group(1), m.group(2)
            if _looks_relevant(link, anchor, kw):
                candidates.setdefault(link, {"url": link, "discovered_via": seed, "anchor": anchor})

    log.info("discovery: %d candidate URLs", len(candidates))
    return list(candidates.values())
