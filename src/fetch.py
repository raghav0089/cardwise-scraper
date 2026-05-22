"""Fetch HTML / markdown for a URL.

Strategy:
  1. Try free Jina AI Reader API first for ALL domains (handles JS-rendering,
     bypasses strict bank firewalls, corrects geo-routing/redirect issues).
  2. Fall back to plain requests + BS4 text extraction only if Jina is down
     and the domain is NOT in standard proxy protection lists.
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
TIMEOUT = 45

# Suppress insecure platform connection pool warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# High-security domains that must never drop down to basic requests (will guarantee a block)
STRICT_PROXY_DOMAINS: frozenset[str] = frozenset({
    "hdfcbank.com", "icicibank.com", "idfcfirstbank.com", "yesbank.in",
    "axisbank.com", "kotak.com", "sbi.co.in", "sbicard.com", "indusind.com",
    "bandhanbank.com", "southindianbank.com", "kvb.co.in", "rblbank.com",
    "canarabank.com", "cred.club", "jupiter.money", "fi.money", "super.money",
    "amazon.in", "goniyo.com", "technofino.in", "cardexpert.in", "live-from-a-lounge.com"
})


@dataclass
class FetchResult:
    text: str = ""
    html: str = ""
    status_code: int = 0
    etag: Optional[str] = None
    error: Optional[str] = None

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


# ── Jina AI Primary Engine ───────────────────────────────────────────────────

def _try_jina(url: str) -> Optional[FetchResult]:
    """Free, headless engine used as primary wrapper to clean pages into Markdown."""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        r = requests.get(jina_url, headers={"User-Agent": UA}, timeout=TIMEOUT, verify=False)
        if r.status_code == 200 and r.text:
            return FetchResult(text=r.text, html=r.text, status_code=200)
        return FetchResult(error=f"Jina returned status code {r.status_code}", status_code=r.status_code)
    except Exception as e:
        log.warning("Jina proxy engine processing failed for %s: %s", url, e)
        return FetchResult(error=str(e))


# ── Requests Fallback ─────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=2, max=10),
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


# ── Public API ────────────────────────────────────────────────────────────────

def fetch(url: str, *, prefer: str = "auto") -> FetchResult:
    """Fetch *url* using Jina AI as the primary worker engine."""
    root = _root_domain(url)

    # --- Strategy: Run Jina First ---
    if prefer != "requests":
        result = _try_jina(url)
        if result and result.ok:
            return result
        
        # If Jina failed but this domain absolutely requires standard routing bypass, return the error
        if root in STRICT_PROXY_DOMAINS:
            log.warning("Primary Jina engine and proxy context failed for strict domain %s: %s", url, result.error if result else "No body")
            return result or FetchResult(error="Proxy pipeline delivery failure")

    # --- Strategy: Run Local Requests Fallback for unprotected domains ---
    try:
        log.info("Running standard backup request context for %s", url)
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


# ── Utilities ─────────────────────────────────────────────────────────────────

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