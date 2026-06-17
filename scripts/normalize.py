"""Normalize captured cards' raw `highlights` into the atomic schema using the
local LLM (free Ollama). Resumable, fault-tolerant. Fills category_rewards,
milestones (with reward_units), welcome, partner value, base earn structure —
keeps the deterministic atoms (fees, lounge, insurance) already present.

    python -m scripts.normalize            # all cards not yet normalized
    python -m scripts.normalize hdfc__...  # specific card_ids
Env: OLLAMA_MODEL (default llama3.2:latest), OLLAMA_TIMEOUT (90)
"""
from __future__ import annotations
import os
os.environ["DISABLE_STORE"] = "1"; os.environ["ALLOW_PAID_LLM"] = "0"
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")   # qwen extracts category rates well
TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "90"))

import sys, json, re
from pathlib import Path
from jsonschema import Draft7Validator

CARDS = Path("out/cards_full.json")
SCHEMA = json.loads(Path("schema/card.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
CATS = set(SCHEMA["properties"]["rewards"]["properties"]["category_rewards"]["propertyNames"]["enum"])
PERIODS = {"daily", "monthly", "quarterly", "half_yearly", "annual", "anniversary", "statement", "per_transaction"}

SYSTEM = """You convert an Indian credit card's benefit bullet points into STRICT JSON.
Rules: use ONLY the bullets; never invent. Numbers are plain integers (₹2 lakh -> 200000, 10,000 -> 10000). Use null / [] when absent. Output ONLY the JSON.

{"currency": "<one token: cashback|reward_points|cashpoints|neucoins|edge_miles|miles|indusmiles|kotak_points|6e_rewards or null>",
 "unit_value_inr": <rupee value of 1 reward unit, or null>,
 "base": {"units": <reward units per block or null>, "per_inr": <spend block in rupees or null>, "rate_pct": <effective % back or null>},
 "category_rewards": {"<category>": {"rate_pct": <%>, "units": <or null>, "per_inr": <or null>, "cap_inr": <or null>, "cap_period": "<monthly|quarterly|annual|statement or null>"}},
 "milestones": [{"spend_inr": <n>, "reward_units": <or null>, "value_inr": <or null>, "period": "<annual|quarterly|monthly or null>"}],
 "welcome": {"reward_units": <or null>, "value_inr": <or null>, "min_spend_inr": <or null>},
 "partner_offers": [{"partner": "<lowercase token like swiggy>", "value_pct": <or null>, "cap_inr": <or null>}]}

category MUST be one of: dining swiggy zomato food_delivery fuel grocery online_shopping amazon flipkart myntra flights hotels travel international utilities bill_payments upi movies entertainment insurance rent education wallet_load departmental_store smartbuy_portal gaming"""


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _ask(card: dict):
    hl = card.get("highlights") or []
    if not hl:
        return None
    from ollama import Client
    user = f"CARD: {card.get('card_name')}\nBULLETS:\n" + "\n".join(f"- {h}" for h in hl[:40])
    resp = Client(timeout=TIMEOUT).chat(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        format="json", options={"temperature": 0, "num_predict": 900},
    )
    try:
        return json.loads(resp.message.content)
    except Exception:
        return None


def apply(card: dict, d: dict) -> None:
    """Merge LLM output into card atomic fields (fill only; keep existing atoms)."""
    r = card.setdefault("rewards", {})
    if d.get("currency") and not r.get("currency"):
        r["currency"] = d["currency"]
    if _num(d.get("unit_value_inr")) is not None and r.get("unit_value_inr") is None:
        r["unit_value_inr"] = d["unit_value_inr"]
    b = d.get("base") or {}
    for k, nk in (("units", "base_units"), ("per_inr", "base_per_inr"), ("rate_pct", "base_rate_pct")):
        if _num(b.get(k)) is not None and r.get(nk) is None:
            r[nk] = b[k]
    # category rewards
    cr = {}
    for cat, v in (d.get("category_rewards") or {}).items():
        cat = re.sub(r"[^a-z_]", "", str(cat).lower())
        if cat not in CATS or not isinstance(v, dict):
            continue
        ent = {kk: v[kk] for kk in ("rate_pct", "units", "per_inr", "cap_inr") if _num(v.get(kk)) is not None}
        # require a real rate signal — skip categories the model named but left empty
        if "rate_pct" not in ent and "units" not in ent:
            continue
        if v.get("cap_period") in PERIODS:
            ent["cap_period"] = v["cap_period"]
        cr[cat] = ent
    if cr:
        r["category_rewards"] = cr
    # milestones
    ms = []
    for m in (d.get("milestones") or []):
        if isinstance(m, dict) and _num(m.get("spend_inr")):
            e = {"spend_inr": m["spend_inr"]}
            for k in ("reward_units", "value_inr"):
                if _num(m.get(k)) is not None:
                    e[k] = m[k]
            if m.get("period") in PERIODS:
                e["period"] = m["period"]
            ms.append(e)
    if ms:
        card["milestones"] = ms
    # welcome
    w = d.get("welcome") or {}
    we = {k: w[k] for k in ("reward_units", "value_inr", "min_spend_inr") if _num(w.get(k)) is not None}
    if we:
        card["welcome_benefit"] = we
    # partner offers (enrich existing tokens with value)
    po = []
    for o in (d.get("partner_offers") or []):
        if isinstance(o, dict) and o.get("partner"):
            e = {"partner": re.sub(r"[^a-z0-9_]", "", str(o["partner"]).lower())}
            for k in ("value_pct", "cap_inr"):
                if _num(o.get(k)) is not None:
                    e[k] = o[k]
            if e["partner"]:
                po.append(e)
    if po:
        card["partner_offers"] = po


def main(argv):
    cards = json.loads(CARDS.read_text())
    want = set(argv) or None
    todo = [c for c in cards if (not want or c.get("card_id") in want)
            and c.get("highlights") and not c.get("_normalized")]
    print(f"normalizing {len(todo)} cards (model={MODEL})", flush=True)
    for i, c in enumerate(todo, 1):
        try:
            d = _ask(c)
            if d:
                apply(c, d)
            c["_normalized"] = True
        except Exception as e:
            print(f"  ! {c.get('card_id')}: {e}", flush=True)
        if i % 10 == 0:
            CARDS.write_text(json.dumps(cards, indent=2, default=str))
            print(f"  …{i}/{len(todo)}", flush=True)
    CARDS.write_text(json.dumps(cards, indent=2, default=str))
    bad = sum(1 for c in cards if list(VALIDATOR.iter_errors({k: v for k, v in c.items() if not k.startswith('_')})))
    print(f"done. {len(cards)} cards, {bad} schema-invalid")


if __name__ == "__main__":
    main(sys.argv[1:])
