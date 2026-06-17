"""Issuer-by-issuer, fault-tolerant, resumable full scrape — LOCAL only.

Goes through issuers ONE AT A TIME. For each issuer it discovers every card URL,
fetches each card (plus configured fee/eligibility sub-pages) and extracts every
detail. The local Ollama depth pass is ON by default so each card gets its full
detail set (set ENRICH=0 to skip for a fast first pass).

BULLETPROOF: every card and every issuer is wrapped in try/except — a single
failure is logged and skipped, never aborts the run. Progress is saved after
EVERY card, so an interrupted/killed run resumes exactly where it stopped.

Writes NOTHING to DynamoDB and never calls a paid LLM.

    python -m scripts.run_local gather            # discover all card URLs (once)
    python -m scripts.run_local run               # process all remaining cards
    python -m scripts.run_local run hdfc sbi_card # only these issuers
    python -m scripts.run_local status            # progress + per-issuer counts

Env:
    ENRICH=0            skip the local-LLM depth pass (faster, fewer details)
    OLLAMA_MODEL=...    default llama3.2:latest (faster than qwen2.5:7b)
"""
from __future__ import annotations
import os
# Configure BEFORE importing modules that read these at import time.
os.environ["AWS_REGION"] = ""                      # disable DDB/S3 (store no-ops)
os.environ["ALLOW_PAID_LLM"] = "0"                 # never spend money
os.environ.setdefault("ENRICH_WITH_LLM", "0" if os.getenv("ENRICH") == "0" else "1")
os.environ.setdefault("OLLAMA_MODEL", "llama3.2:latest")

import sys, json, time, traceback
from collections import Counter, defaultdict
from pathlib import Path

import yaml
from src.scrape_issuers import collect_detail_urls
from src.main import fetch_page, _is_valid_card_name
from src.extract import extract_cards
from src.normalize import ensure_card_id, stamp, dedupe

OUT = Path("out"); OUT.mkdir(exist_ok=True)
URLS  = OUT / "urls.json"
DONE  = OUT / "done.json"
CARDS = OUT / "cards_full.json"
FAILS = OUT / "fails.json"

CFG = yaml.safe_load(Path("config/issuers.yaml").read_text())
ISSUER_ORDER = [i["id"] for i in CFG["issuers"]]


def _load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def gather() -> None:
    t0 = time.time()
    rows = collect_detail_urls()
    URLS.write_text(json.dumps(rows, indent=2))
    by = Counter(r["issuer_id"] for r in rows)
    print(f"gathered {len(rows)} card URLs across {len(by)} issuers in {time.time()-t0:.0f}s")


LOCK = OUT / "run.lock"


def _acquire_lock() -> bool:
    """Refuse to start if another runner is already alive (prevents file races)."""
    if LOCK.exists():
        try:
            old = int(LOCK.read_text().strip())
            os.kill(old, 0)          # raises if pid is dead
            print(f"another runner is already active (pid {old}) — refusing to start a second.")
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass                      # stale lock → take over
    LOCK.write_text(str(os.getpid()))
    return True


def run(only: set[str] | None) -> None:
    if not _acquire_lock():
        return
    try:
        _run(only)
    finally:
        try: LOCK.unlink()
        except OSError: pass


def _run(only: set[str] | None) -> None:
    rows = _load(URLS, None)
    if rows is None:
        print("no urls.json — run `gather` first"); return
    done  = set(_load(DONE, []))
    cards = _load(CARDS, [])
    fails = _load(FAILS, [])
    enrich = os.environ.get("ENRICH_WITH_LLM") == "1"

    # group URLs by issuer, in config order
    by_issuer: dict[str, list] = defaultdict(list)
    for r in rows:
        by_issuer[r["issuer_id"]].append(r)
    issuers = [i for i in ISSUER_ORDER if i in by_issuer and (not only or i in only)]
    print(f"=== processing {len(issuers)} issuer(s); enrichment={'ON' if enrich else 'off'} "
          f"({os.environ.get('OLLAMA_MODEL')}) ===", flush=True)

    def flush():
        DONE.write_text(json.dumps(sorted(done)))
        CARDS.write_text(json.dumps(dedupe(cards), indent=2, default=str))
        FAILS.write_text(json.dumps(fails, indent=2))

    for iid in issuers:
        urls = [r for r in by_issuer[iid] if r["url"] not in done]
        if not urls:
            continue
        name = next((i["name"] for i in CFG["issuers"] if i["id"] == iid), iid)
        print(f"\n### {iid} ({name}) — {len(urls)} card URL(s) to process", flush=True)
        got = 0
        for j, r in enumerate(urls, 1):
            try:
                page = fetch_page(r)
                if page:
                    for c in (extract_cards([page]) or []):
                        if not _is_valid_card_name((c.get("card_name") or "")):
                            continue
                        c = stamp(ensure_card_id(c))
                        cards.append(c); got += 1
            except Exception as e:
                fails.append({"url": r["url"], "issuer_id": iid, "error": str(e)})
                print(f"   ! FAIL {r['url']}: {e}", flush=True)
                traceback.print_exc()
            done.add(r["url"])
            if j % 5 == 0:
                flush(); print(f"   …{j}/{len(urls)} ({iid})", flush=True)
        flush()
        print(f"   done {iid}: +{got} cards", flush=True)

    print(f"\n=== ALL DONE: {len(dedupe(cards))} unique cards, {len(done)} urls, {len(fails)} fails ===")


def status() -> None:
    rows = _load(URLS, [])
    done = set(_load(DONE, []))
    cards = dedupe(_load(CARDS, []))
    fails = _load(FAILS, [])
    total_by = Counter(r["issuer_id"] for r in rows)
    done_by  = Counter(r["issuer_id"] for r in rows if r["url"] in done)
    card_by  = Counter(c.get("issuer_id") for c in cards)
    print(f"urls={len(rows)} processed={len(done)} remaining={len(rows)-len(done)} "
          f"cards={len(cards)} fails={len(fails)}")
    fee = sum(1 for c in cards if c.get("fees"))
    print(f"cards-with-fees={fee}/{len(cards)}\n")
    print(f"{'issuer':24s} {'done/total':>10s} {'cards':>6s}")
    for iid in ISSUER_ORDER:
        if total_by.get(iid):
            print(f"{iid:24s} {str(done_by.get(iid,0))+'/'+str(total_by[iid]):>10s} {card_by.get(iid,0):>6d}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "gather":
        gather()
    elif cmd == "run":
        only = set(args[1:]) or None
        run(only)
    elif cmd == "status":
        status()
    else:
        print(__doc__)
