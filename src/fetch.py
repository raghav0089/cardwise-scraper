"""Fetch HTML / markdown for a URL.

Strategy:
  1. Try Firecrawl (handles JS-rendered SPA pages like CRED, Jupiter, OneCard).
  2. Fall back to plain requests + BS4 text extraction.

Returns (markdown_or_text, raw_html, status_code, etag).
"""
from __future__ import annotations
import os, time, logging, hashlib
from typing import Tuple, Optional
import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; PlentiCardBot/1.0; +https://plenti.app/bot)"
TIMEOUT = 30

_firecrawl = None
def _fc():
    global _firecrawl
    if _firecrawl is None and os.getenv("FIRECRAWL_API_KEY"):
        from firecrawl import FirecrawlApp
        _firecrawl = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])
    return _firecrawl


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
def fetch(url: str, *, prefer: str = "auto") -> Tuple[str, str, int, Optional[str]]:
    if prefer in ("auto", "firecrawl"):
        fc = _fc()
        if fc is not None:
            try:
                res = fc.scrape_url(url, formats=["markdown", "html"], only_main_content=True)
                md = (getattr(res, "markdown", None)
                      or (res.get("markdown") if isinstance(res, dict) else None)
                      or "")
                html = (getattr(res, "html", None)
                        or (res.get("html") if isinstance(res, dict) else None)
                        or "")
                if md or html:
                    return md or _to_text(html), html or "", 200, None
            except Exception as e:
                log.warning("firecrawl failed for %s: %s — falling back", url, e)

    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    return _to_text(html), html, r.status_code, r.headers.get("ETag")


def _to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def absolutize(base: str, links: list[str]) -> list[str]:
    from urllib.parse import urljoin, urlparse
    out, seen = [], set()
    for href in links:
        if not href: continue
        u = urljoin(base, href.split("#")[0])
        if not urlparse(u).scheme.startswith("http"): continue
        if u in seen: continue
        seen.add(u); out.append(u)
    return out


def harvest_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    hrefs = [a.get("href") for a in soup.find_all("a", href=True)]
    return absolutize(base_url, hrefs)
