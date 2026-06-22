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

# Quick pre-filter applied before URLs enter the queue — catches image/asset
# URLs that contain "card" in their path and slip past detail_link_pattern.
_STATIC_PATH_RE = re.compile(
    r"/(?:static(?:-resources)?|assets?|img|images?|media|fonts?|icons?|svg|sprint)/",
    re.I,
)
_ASSET_EXT_RE = re.compile(
    r"\.(jpe?g|png|webp|gif|svg|ico|pdf|zip|mp[34]|woff2?|ttf|css|js|xml)(\?.*)?$",
    re.I,
)

def _is_scrapeable(u: str) -> bool:
    """Return False for image, asset, or tracking URLs that should never be queued."""
    if _ASSET_EXT_RE.search(u):
        return False
    if _STATIC_PATH_RE.search(u):
        return False
    return True
CFG = yaml.safe_load(Path("config/issuers.yaml").read_text())
MAX_URLS_PER_ISSUER = int(os.getenv("MAX_URLS_PER_ISSUER", "150"))
# Per-issuer curated fallback URL lists (config/fallback_urls/<id>.txt).
_FALLBACK_DIR = Path("config/fallback_urls")


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

        # ── Forced single cards (neobanks / product name lacks 'card') ─────────
        # issuer `cards:` is a list of {name, url, category?, network?}. Each is
        # emitted as a forced card so the parser creates it with that exact name.
        for cd in (issuer.get("cards") or []):
            u = cd.get("url")
            if not u or u in added:
                continue
            added.add(u)
            out.append({
                "url":             u,
                "issuer_id":       issuer["id"],
                "issuer_name":     issuer["name"],
                "issuer_type":     issuer["type"],
                "is_listing":      False,
                "force_card_name": cd["name"],
                "force_category":  cd.get("category", "credit"),
                "force_network":   cd.get("network"),
            })
        if issuer.get("cards"):
            log.info("    %d forced card(s)", len(issuer["cards"]))

        # ── Explicit single-card detail pages ─────────────────────────────────
        # `detail_urls` are specific card product pages, parsed AS detail pages with
        # NO sibling-link harvesting. Use this for co-brand / single-card issuers so
        # they don't vacuum up the parent bank's entire card sitemap via nav menus.
        for det_url in (issuer.get("detail_urls") or []):
            if det_url in added:
                continue
            added.add(det_url)
            out.append({
                "url":         det_url,
                "issuer_id":   issuer["id"],
                "issuer_name": issuer["name"],
                "issuer_type": issuer["type"],
                "is_listing":  False,
            })
        if issuer.get("detail_urls"):
            log.info("    %d explicit detail URL(s) (no harvest)", len(issuer["detail_urls"]))

        # ── List-page HTML harvest ────────────────────────────────────────────
        for list_url in (issuer.get("list_urls") or []):
            result = fetch(list_url)
            html   = result.html if result and result.ok else ""

            # Always add the listing page with is_listing=True — even if it was already
            # harvested as a detail link, this version takes precedence in the global dedup.
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
                if not _is_scrapeable(u): continue
                if u in added: continue
                added.add(u)
                out.append({
                    "url":        u,
                    "issuer_id":  issuer["id"],
                    "issuer_name":issuer["name"],
                    "issuer_type":issuer["type"],
                })
            log.info("    harvested %d detail links from %s", len(added) - n_before, list_url)

        # ── Curated fallback URLs ──────────────────────────────────────────────
        # config/fallback_urls/<id>.txt is a hand-verifiable list of every known
        # card URL for this issuer. Always merge it so a card is never missed when
        # a sitemap/listing changes or breaks. These are added as DETAIL pages.
        fb = _FALLBACK_DIR / f"{issuer['id']}.txt"
        if fb.exists():
            n_fb = 0
            for line in fb.read_text().splitlines():
                u = line.strip()
                if not u or u.startswith("#") or u in added or not _is_scrapeable(u):
                    continue
                added.add(u)
                out.append({
                    "url":         u,
                    "issuer_id":   issuer["id"],
                    "issuer_name": issuer["name"],
                    "issuer_type": issuer["type"],
                    "is_listing":  False,
                })
                n_fb += 1
            if n_fb:
                log.info("    +%d card URL(s) from fallback file", n_fb)

    # global dedupe — if a URL appears both as a configured list page (is_listing=True)
    # and as a harvested detail link, the list-page entry wins so it gets section-split.
    best: dict[str, dict] = {}
    for r in out:
        url = r["url"]
        if url not in best or r.get("is_listing"):
            best[url] = r
    uniq = list(best.values())
    log.info("URL-gather done: %d total URLs to scrape", len(uniq))
    return uniq
