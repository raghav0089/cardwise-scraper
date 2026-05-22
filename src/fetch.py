"""Fetch HTML / markdown for a URL.

Strategy:
  1. Try Firecrawl for all URLs (handles JS-rendered SPAs and Cloudflare-
     protected sites like HDFC, ICICI, IDFC, Yes Bank).
  2. Fall back to free Jina AI Reader API if Firecrawl fails or hits payment walls.
  3. Fall back to plain requests + BS4 only for domains NOT in
     FIRECRAWL_ONLY_DOMAINS.

FetchResult unpacks as a 4-tuple (text, html, status_code, etag) so all
existing callers work unchanged:

    md, html, status, etag = fetch(url)        # still works
    result = fetch(url); result.ok             # also works
"""
from __future__ import annotations
import os, logging, hashlib, urllib3
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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 45  # raised from 30 — Yes Bank times out at 30 s

# Suppress insecure platform warnings from verify=False logic
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Domains that actively block plain requests (WAF / Cloudflare / JS-only).
# For these, we skip standard requests.get entirely to prevent 403 blocks.
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
    "bandhanbank.com",       
    "southindianbank.com",   
    "kvb.co.in"
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


# ── Jina AI Free Fallback ─────────────────────────────────────────────────────

def _try_jina(url: str) -> Optional[FetchResult]:
    """Free alternative to process pages with heavy client JS or proxy blocks."""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        r = requests.get(jina_url, headers={"User-Agent": UA}, timeout=TIMEOUT, verify=False)
        if r.status_code == 200 and r.text:
            return FetchResult(text=r.text, html=r.text, status_code=200)
        return None
    except Exception as e:
        log.warning("Jina fallback failed for %s: %s", url, e)
        return None


# ── requests fallback ─────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _try_requests(url: str) -> FetchResult:
    # Set headers with browser-grade properties to circumvent SSLv3 alerts & handshakes
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
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

    prefer="auto"       — Firecrawl first, then Jina AI; plain requests fallback
                          only for domains NOT in FIRECRAWL_ONLY_DOMAINS.
    prefer="firecrawl"  — Firecrawl/Jina only, no plain requests fallback.
    prefer="requests"   — plain requests only (skips Firecrawl and Jina entirely).
    """
    fc_only = prefer == "firecrawl" or (
        prefer == "auto" and _root_domain(url) in FIRECRAWL_ONLY_DOMAINS
    )

    # --- Pass 1: Firecrawl ---
    if prefer != "requests":
        result = _try_firecrawl(url)
        if result and result.ok:
            return result
        
        # --- Pass 2: Jina AI Reader Fallback (Free & handles JS) ---
        log.info("Attempting free Jina reader fallback engine for %s", url)
        jina_result = _try_jina(url)
        if jina_result and jina_result.ok:
            return jina_result

        if fc_only:
            log.warning("firecrawl/jina domain fetch failed %s: %s",
                        url, result.error if result else "no API engine available")
            return jina_result or result or FetchResult(error="All processing engines failed")

    # --- Pass 3: Standard Requests Fallback (unprotected sites only) ---
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