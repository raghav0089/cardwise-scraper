"""Fetch HTML / markdown for a URL.

Strategy:
  1. Try Firecrawl for all URLs (handles JS-rendered SPAs and Cloudflare-
     protected sites like HDFC, ICICI, IDFC, Yes Bank).
  2. Fall back to plain requests + BS4 only for domains NOT in
     FIRECRAWL_ONLY_DOMAINS.

FetchResult unpacks as a 4-tuple (text, html, status_code, etag) so all
existing callers work unchanged:

    md, html, status, etag = fetch(url)        # still works
    result = fetch(url); result.ok             # also works
"""
from __future__ import annotations
import os, logging, hashlib
from dataclasses import dataclass
from typing import Optional, Iterator
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; PlentiCardBot/1.0; +https://plenti.app/bot)"
TIMEOUT = 45  # raised from 30 — Yes Bank times out at 30 s

# Domains that actively block plain requests (WAF / Cloudflare / JS-only).
# For these we ONLY try Firecrawl and skip the requests.get fallback entirely,
# because the fallback just burns retries on a guaranteed 403/429.
FIRECRAWL_ONLY_DOMAINS: frozenset[str] = frozenset({
    "hdfcbank.com",
    "icicibank.com",
    "idfcfirstbank.com",
    "yesbank.in",
    "axisbank.com",
    "kotak.com",
    "sbi.co.in",
    "sbicard.com",
    "indusind.com",
    "bandhanbank.com",       # Added to handle proxy blockades
    "southindianbank.com",   # Added to prevent parsing failures
})


@dataclass
class FetchResult:
    """Return value of fetch().

    Unpacks as a 4-tuple so legacy callers are unaffected:
        md, html, status, etag = fetch(url)
    """
    text: str = ""
    html: str = ""
    status_code: int = 0
    etag: Optional[str] = None
    error: Optional[str] = None

    # ── tuple protocol ────────────────────────────────────────────────────
    def __iter__(self) -> Iterator:
        yield self.text
        yield self.html
        yield self.status_code
        yield self.etag

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int):
        return (self.text, self.html, self.status_code, self.etag)[index]

    @property
    def ok(self) -> bool:
        return bool(self.text or self.html) and self.error is None


# ── Firecrawl ─────────────────────────────────────────────────────────────────

_firecrawl = None

def _fc():
    global _firecrawl
    if _firecrawl is None and os.getenv("FIRECRAWL_API_KEY"):
        from firecrawl import FirecrawlApp
        _firecrawl = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])
    return _firecrawl


def _try_firecrawl(url: str) -> Optional[FetchResult]:
    fc = _fc()
    if fc is None:
        return None
    try:
        res = fc.scrape_url(url, formats=["markdown", "html"], only_main_content=True)
        md   = getattr(res, "markdown", None) or (res.get("markdown")  if isinstance(res, dict) else None) or ""
        html = getattr(res, "html",     None) or (res.get("html")      if isinstance(res, dict) else None) or ""
        if md or html:
            return FetchResult(text=md or _to_text(html), html=html or "", status_code=200)
        return FetchResult(error=f"firecrawl returned empty for {url}")
    except Exception as e:
        log.warning("firecrawl failed for %s: %s", url, e)
        return FetchResult(error=str(e))


# ── requests fallback ─────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _try_requests(url: str) -> FetchResult:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    return FetchResult(
        text=_to_text(html),
        html=html,
        status_code=r.status_code,
        etag=r.headers.get("ETag"),
    )


# ── public API ────────────────────────────────────────────────────────────────

def fetch(url: str, *, prefer: str = "auto") -> FetchResult:
    """Fetch *url* and return a FetchResult (also iterable as a 4-tuple).

    prefer="auto"       — Firecrawl first; plain requests fallback only for
                          domains NOT in FIRECRAWL_ONLY_DOMAINS.
    prefer="firecrawl"  — Firecrawl only, no fallback.
    prefer="requests"   — plain requests only (skips Firecrawl entirely).
    """
    fc_only = prefer == "firecrawl" or (
        prefer == "auto" and _root_domain(url) in FIRECRAWL_ONLY_DOMAINS
    )

    # --- Firecrawl pass ---
    if prefer != "requests":
        result = _try_firecrawl(url)
        if result and result.ok:
            return result
        if fc_only:
            log.warning("firecrawl-only domain fetch failed %s: %s",
                        url, result.error if result else "no FIRECRAWL_API_KEY")
            return result or FetchResult(error="no FIRECRAWL_API_KEY configured")

    # --- requests fallback (non-protected domains only) ---
    try:
        return _try_requests(url)
    except requests.Timeout:
        log.warning("timeout fetching %s", url)
        return FetchResult(error=f"timeout after {TIMEOUT}s", status_code=408)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        log.warning("HTTP %s fetching %s: %s", code, url, e)
        return FetchResult(error=str(e), status_code=code)
    except requests.RequestException as e:
        log.warning("request failed %s: %s", url, e)
        return FetchResult(error=str(e))


# ── utilities ─────────────────────────────────────────────────────────────────

def _root_domain(url: str) -> str:
    """'sub.hdfcbank.com' → 'hdfcbank.com'"""
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return "\n".join(
        line.strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def absolutize(base: str, links: list[str]) -> list[str]:
    out, seen = [], set()
    for href in links:
        if not href:
            continue
        u = urljoin(base, href.split("#")[0])
        if not urlparse(u).scheme.startswith("http"):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def harvest_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    hrefs = [a.get("href") for a in soup.find_all("a", href=True)]
    return absolutize(base_url, hrefs)