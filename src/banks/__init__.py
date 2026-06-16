"""Per-bank extractor registry.

Architecture decision (see project notes): we deliberately do NOT write a
separate full scraper per bank. ~90 issuers are handled fine by the shared
generic pipeline (URL gather → fetch → rule-based parse → optional local LLM).
Only the banks the generic pipeline handles poorly get a small focused
override here — so a site redesign touches one file, not ninety.

Usage:
    from .banks import get_extractor
    ext = get_extractor(issuer_id)      # None → use the generic pipeline
    if ext:
        cards = ext.extract(pages)

To add special handling for a bank, create a module in this package that
subclasses BankExtractor (see base.py) and call register() on an instance
below. Keep get_extractor() returning None for everyone else.
"""
from __future__ import annotations
import logging

from .base import BankExtractor

log = logging.getLogger(__name__)

# issuer_id -> extractor instance
_REGISTRY: dict[str, BankExtractor] = {}


def register(extractor: BankExtractor) -> BankExtractor:
    """Register an extractor for every issuer_id it declares."""
    for iid in extractor.issuer_ids:
        if iid in _REGISTRY:
            log.warning("bank extractor for %r already registered — overriding", iid)
        _REGISTRY[iid] = extractor
    return extractor


def get_extractor(issuer_id: str | None) -> BankExtractor | None:
    """Return the registered extractor for an issuer, or None for generic handling."""
    if not issuer_id:
        return None
    return _REGISTRY.get(issuer_id)


# ── Registrations ──────────────────────────────────────────────────────────────
# Add per-bank overrides here as they are built, e.g.:
#   from .hdfc import HdfcExtractor
#   register(HdfcExtractor())
# Empty registry = every issuer uses the generic pipeline (safe default).
