"""Fetch HTML / markdown for a URL.

Strategy:
  1. Jina AI Reader (r.jina.ai) — free, handles JS + most WAFs → clean markdown.
  2. Plain requests + BS4 fallback for non-strict domains.

Rate limiting: enforces a minimum gap between Jina calls to avoid 429s.
"""
from __future__ import annotations
import logging, hashlib, time, threading
from dataclasses import dataclass
from typing import Optional, Iterator
from urllib.parse import urlparse, urljoin

import re, requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 30

# A free Jina API key (https://jina.ai/reader) raises the rate limit from ~20 RPM
# (keyless) to ~200+ RPM and authenticates requests. Set JINA_API_KEY to use it.
import os as _os
JINA_API_KEY = _os.getenv("JINA_API_KEY", "").strip()

# Minimum seconds between Jina requests. Keyless free tier ~20 RPM → 3.5s; with an
# API key the limit is far higher, so we can go much faster. Override via JINA_MIN_INTERVAL.
JINA_MIN_INTERVAL = float(_os.getenv(
    "JINA_MIN_INTERVAL", "0.4" if JINA_API_KEY else "3.5"))

# Hosts where Jina can't render meaningful content (SPA with broken SSR router)
# but direct requests returns SSR HTML with card data embedded.
JINA_BYPASS_HOSTS: frozenset[str] = frozenset({
    "www.axis.bank.in",
    "axis.bank.in",
    # PSU / SFB / payments banks whose heavy *.bank.in sites Jina can't render but
    # plain requests returns full SSR HTML with the card links/content.
    "www.unionbankofindia.bank.in", "www.indianbank.in",
    "bankofmaharashtra.bank.in", "centralbank.bank.in", "www.cityunionbank.com",
    "www.dhan.bank.in", "www.dbs.bank.in", "equitas.bank.in", "www.jana.bank.in",
    "www.ujjivansfb.bank.in", "www.nainitalbank.bank.in",
    "www.airtelpayments.bank.in", "www.fino.bank.in",
})

STRICT_PROXY_DOMAINS: frozenset[str] = frozenset({
    # Major private / PSU banks (legacy domains)
    "hdfcbank.com", "icicibank.com", "idfcfirstbank.com", "yesbank.in",
    "axisbank.com", "sbi.co.in", "sbicard.com",
    "bandhanbank.com", "southindianbank.com",
    "federalbank.co.in", "idbibank.in", "unionbankofindia.co.in",
    "bobcard.co.in",
    # Fintech / neobanks
    "cred.club", "jupiter.money", "fi.money", "super.money",
    "amazon.in", "goniyo.com", "tataneu.com", "bajajfinserv.in",
    # Indian banks registered under *.bank.in — covers Axis, HDFC, Kotak,
    # IndusInd, Standard Chartered India, Yes, PNB, Canara, RBL, AU, Bandhan,
    # BOI, BOM, Central Bank, IOB, UCO, PSB, Karnataka Bank, KVB, DCB, Dhan,
    # Saraswat, JKB, CSB, DBS India, Equitas, Suryoday, ESAF, Jana, Utkarsh etc.
    "bank.in",
    # Other ccTLD bank domains not on *.bank.in
    "hsbc.co.in", "americanexpress.com", "sc.com",
    "cityunionbank.com",       # CUB still on .com
    "paisabazaar.com",         # StepUp card
    "stashfin.com",
})

# ── Jina rate-limit state ─────────────────────────────────────────────────────
_jina_lock      = threading.Lock()
_jina_last_call = 0.0


def _jina_wait() -> None:
    """Block until we're allowed to make the next Jina call."""
    global _jina_last_call
    with _jina_lock:
        now     = time.monotonic()
        gap     = now - _jina_last_call
        if gap < JINA_MIN_INTERVAL:
            time.sleep(JINA_MIN_INTERVAL - gap)
        _jina_last_call = time.monotonic()


@dataclass
class FetchResult:
    text: str = ""
    html: str = ""
    status_code: int = 0
    etag: Optional[str] = None
    error: Optional[str] = None

    def __iter__(self) -> Iterator:
        yield self.text; yield self.html; yield self.status_code; yield self.etag
    def __len__(self) -> int: return 4
    def __getitem__(self, i: int): return (self.text, self.html, self.status_code, self.etag)[i]

    @property
    def ok(self) -> bool:
        return bool(self.text or self.html) and self.error is None


# ── Jina ──────────────────────────────────────────────────────────────────────

def _try_jina(url: str) -> Optional[FetchResult]:
    _jina_wait()
    jina_url = f"https://r.jina.ai/{url}"
    t0 = time.monotonic()
    try:
        headers = {
            "User-Agent": UA,
            "Accept": "text/markdown,text/plain,*/*",
            # Strip nav/header/footer so card content isn't buried under 8K+ of nav menus.
            # Critical for HDFC bank.in and other SPA banks that render huge nav dropdowns.
            "X-Remove-Selector": "nav, header, footer, .mega-menu, .nav-menu, .navigation",
            # Append a compact links section to the markdown — improves link harvest
            # on sites whose card links are spread across JS-rendered components.
            "X-With-Links-Summary": "true",
        }
        if JINA_API_KEY:
            headers["Authorization"] = f"Bearer {JINA_API_KEY}"
        r = requests.get(jina_url, headers=headers, timeout=TIMEOUT)
        elapsed = time.monotonic() - t0
        if r.status_code == 429:
            log.warning("jina rate-limited (429) for %s — will back off", url)
            time.sleep(10)   # back off before next call
            return FetchResult(error="jina 429", status_code=429)
        if r.status_code == 200 and r.text:
            # Detect SPA 404 shells — Jina renders JavaScript but gets "Page Not Found"
            # (e.g. axis.bank.in which has SSR content but a broken SPA router)
            first300 = r.text[:300].lower()
            if ("page not found" in first300 or
                    "warning: target url returned error 404" in first300 or
                    "error 404" in first300):
                log.warning("jina returned 404-page content for %s", url)
                return FetchResult(error="jina_spa_404", status_code=404)
            log.debug("jina ok %.1fs %s", elapsed, url)
            return FetchResult(text=r.text, html=r.text, status_code=200)
        return FetchResult(error=f"jina status {r.status_code}", status_code=r.status_code)
    except requests.Timeout:
        log.warning("jina timeout (>%ds) %s", TIMEOUT, url)
        return FetchResult(error=f"jina timeout after {TIMEOUT}s")
    except Exception as e:
        log.warning("jina error %s: %s", url, e)
        return FetchResult(error=str(e))


# ── Requests fallback ─────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=2, max=8),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _try_requests(url: str) -> FetchResult:
    log.info("requests fallback → %s", url)
    r = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
        timeout=TIMEOUT,
        allow_redirects=True,
    )
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
    log.info("fetch → %s", url)
    t0    = time.monotonic()
    host  = urlparse(url).hostname or ""
    root  = _root_domain(url)
    strict = root in STRICT_PROXY_DOMAINS

    # SPA sites where Jina can't render content — use direct requests only.
    if host in JINA_BYPASS_HOSTS:
        try:
            result = _try_requests(url)
            log.info("fetch ok (requests/spa-bypass, %.1fs) %s", time.monotonic() - t0, url)
            return result
        except Exception as e:
            log.warning("fetch error (requests/spa-bypass, %.1fs) %s: %s", time.monotonic() - t0, url, e)
            return FetchResult(error=str(e))

    if prefer != "requests":
        result = _try_jina(url)
        if result and result.ok:
            log.info("fetch ok (jina, %.1fs) %s", time.monotonic() - t0, url)
            return result
        # Allow direct-requests fallback even on strict domains when Jina returned a
        # "Page Not Found" SPA shell — the SSR content is in the raw HTML.
        jina_spa_404 = result and result.error == "jina_spa_404"
        if strict and not jina_spa_404:
            log.warning("fetch failed strict domain (%.1fs) %s: %s",
                        time.monotonic() - t0, url,
                        result.error if result else "no result")
            return result or FetchResult(error="jina failed, no fallback for strict domain")

    try:
        result = _try_requests(url)
        log.info("fetch ok (requests, %.1fs) %s", time.monotonic() - t0, url)
        return result
    except requests.Timeout:
        log.warning("fetch timeout (%.1fs) %s", time.monotonic() - t0, url)
        return FetchResult(error=f"timeout after {TIMEOUT}s", status_code=408)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        log.warning("fetch HTTP %s (%.1fs) %s", code, time.monotonic() - t0, url)
        return FetchResult(error=str(e), status_code=code)
    except requests.RequestException as e:
        log.warning("fetch error (%.1fs) %s: %s", time.monotonic() - t0, url, e)
        return FetchResult(error=str(e))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _root_domain(url: str) -> str:
    host  = urlparse(url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


# Query params that are tracking-only and should be stripped from harvested URLs.
_TRACKING_PARAMS = frozenset({
    "icid", "mc_id", "channelsource", "lgcode", "cmpid",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "source", "isonline", "campaign",
})


def _clean_url(url: str) -> str:
    """Strip known tracking query parameters, keep functional ones."""
    from urllib.parse import parse_qs, urlencode, urlunparse
    p = urlparse(url)
    if not p.query:
        return url
    qs = {k: v for k, v in parse_qs(p.query, keep_blank_values=False).items()
          if k.lower() not in _TRACKING_PARAMS}
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


def absolutize(base: str, links: list[str]) -> list[str]:
    out, seen = [], set()
    for href in links:
        if not href: continue
        u = urljoin(base, href.split("#")[0])
        if not urlparse(u).scheme.startswith("http"): continue
        u = _clean_url(u)
        if u in seen: continue
        seen.add(u); out.append(u)
    return out


def harvest_links(html: str, base_url: str) -> list[str]:
    soup  = BeautifulSoup(html or "", "lxml")
    hrefs = [a.get("href") for a in soup.find_all("a", href=True)]
    # Jina returns markdown — extract both absolute and relative markdown links.
    # Relative links like [Card Name](/path/to/card) were previously missed entirely.
    abs_md  = re.findall(r'\]\((https?://[^\s)]+)\)', html or "")
    rel_md  = re.findall(r'\]\((/[^\s)#][^\s)]*)\)',  html or "")
    return absolutize(base_url, hrefs + abs_md + rel_md)