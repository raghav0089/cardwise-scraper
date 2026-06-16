"""Base class for per-bank extractor overrides.

Most issuers are handled by the generic rule-based + (optional local LLM)
pipeline in extract.py. A few banks render pages in ways the generic pipeline
mis-handles — SPA shells, unusual fee tables, co-brand cross-sell pollution,
or product names that only appear in odd places. For those, subclass
BankExtractor and register it in __init__.py.

A registered extractor OWNS extraction for its issuer(s): extract_cards()
hands the page batch straight to it and uses whatever it returns. The default
implementation just runs the shared rule-based parser, so a subclass only has
to override the parts that are actually different for that bank.
"""
from __future__ import annotations
import logging

from ..parse import parse_cards

log = logging.getLogger(__name__)


class BankExtractor:
    """Override hook for a single issuer (or a family of related issuers).

    Subclasses set `issuer_ids` and override `skip_url` and/or `extract`.
    """

    #: issuer_id values (from config/issuers.yaml) this extractor handles
    issuer_ids: tuple[str, ...] = ()

    def skip_url(self, url: str) -> bool:
        """Return True to drop a URL before it is ever fetched.

        Use for bank-specific junk paths that the global filters miss.
        """
        return False

    def extract(self, pages: list[dict]) -> list[dict]:
        """Return card dicts for this batch of fetched pages.

        Default: run the shared rule-based parser on each page. Subclasses
        override to add bank-specific pre/post-processing while still reusing
        the generic parser via `super().extract(pages)`.
        """
        out: list[dict] = []
        for p in pages:
            out.extend(parse_cards(p))
        return out
