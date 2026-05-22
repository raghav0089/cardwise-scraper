"""Fetch HTML / markdown for a URL.

Strategy:
  1. Try free Jina AI Reader API first for protected, JavaScript-heavy, or WAF/Cloudflare
     blocked domains (like HDFC, ICICI, Axis, Yes Bank, KVB, South Indian Bank).
  2. Fall back to clean plain requests + BS4 text extraction only for standard,
     unprotected domains not listed in JINA_RECOMMENDED_DOMAINS.

FetchResult unpacks as a 4-tuple (text, html, status_code, etag) so all
existing callers work unchanged:

    md, html, status, etag = fetch(url)        # still works
    result = fetch(url); result.ok             # also works
"""
from __future__ import annotations
import logging, hashlib, urllib3
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
TIMEOUT = 45  # Yes Bank and heavy portals require higher timeout cushions

# Suppress noisy insecure platform warnings printed by verify=False configurations
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# High-security banking domains that block basic requests fingerprints outright.
# For these, we skip standard requests completely and only use Jina's headless wrapper.
JINA_RECOMMENDED_DOMAINS: frozenset[str] = frozenset({
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


# ── Jina AI Free Engine ───────────────────────────────────────────────────────

def _try_jina(url: str) -> Optional[FetchResult]:
    """Free, robust endpoint that handles JavaScript rendering and bypasses WAF/anti-bot systems."""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        r = requests.get(jina_url, headers={"User-Agent": UA}, timeout=TIMEOUT, verify=False)
        if r.status_code == 200 and r.text:
            return FetchResult(text=r.text, html=r.text, status_code=200)
        return FetchResult(error=f"Jina returned status code {r.status_code}", status_code=r.status_code)
    except Exception as e:
        log.warning("Jina engine processing failed for %s: %s", url, e)
        return FetchResult(error=str(e))


# ── requests fallback ─────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _try_requests(url: str) -> FetchResult:
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

    prefer="auto"       — Jina first for protected domains; requests fallback for others.
    prefer="firecrawl"  — (Legacy/Alias) Routes straight to Jina Engine only.
    prefer="requests"   — plain requests only (skips Jina entirely).
    """
    force_jina = prefer == "firecrawl" or (
        prefer == "auto" and _root_domain(url) in JINA_RECOMMENDED_DOMAINS
    )

    # --- Pass 1: Jina Free Engine (Forced or requested) ---
    if prefer != "requests" and force_jina:
        result = _try_jina(url)
        if result and result.ok:
            return result
        log.warning("Jina-recommended engine block failed for %s: %s", url, result.error if result else "No body")
        return result or FetchResult(error="Jina processing failure")

    # --- Pass 2: Standard Requests Fallback (unprotected or auto domains) ---
    try:
        return _try_requests(url)
    except requests.Timeout:
        log.warning("timeout fetching %s", url)
        return FetchResult(error=f"timeout after {TIMEOUT}s", status_code=408)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        
        # If standard requests hit an unexpected 403 Forbidden/406 on an unlisted domain,
        # run an emergency routing pass through Jina before giving up.
        if code in (403, 401, 406) and prefer == "auto":
            log.info("Encountered HTTP %s on standard requests adapter. Routing emergency fallback to Jina for %s", code, url)
            jina_res = _try_jina(url)
            if jina_res and jina_res.ok:
                return jina_res
                
        log.warning("HTTP %s fetching %s: %s", code, url, e)
        return FetchResult(error=str(e), status_code=code)
    except requests.RequestException as e:
        # Emergency catch-all for SSL errors or dropped connections
        if prefer == "auto":
            log.info("Network exception caught on standard requests. Running emergency pass through Jina for %s", url)
            jina_res = _try_jina(url)
            if jina_res and jina_res.ok:
                return jina_res
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