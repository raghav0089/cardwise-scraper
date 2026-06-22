"""Migrate out/cards_full.json from the old extraction format to the new ATOMIC
schema so every card validates. Deterministic (no LLM): renames fields, lowercases
enums to tokens, keeps reliable atoms (fee/lounge/insurance/eligibility numbers,
currency, base rate) + highlights, and DROPS the messy free-text fields
(perks[].value, partner_offers[].benefit, milestone reward strings, accelerated
notes) — those are preserved verbatim in highlights[] and re-derived atomically by
the LLM normalization pass.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from jsonschema import Draft7Validator

SRC = Path("out/cards_full.json")
SCHEMA = json.loads(Path("schema/card.schema.json").read_text())


def slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


_NET = {"visa": "visa", "mastercard": "mastercard", "rupay": "rupay", "amex": "amex",
        "american_express": "amex", "diners": "diners", "diners_club": "diners"}
_SEG = {"super-premium": "super_premium", "invite-only": "invite_only"}
_CAT = {"credit", "debit", "prepaid", "forex", "corporate", "business"}
_PERIODS = {"monthly", "quarterly", "half_yearly", "annual", "anniversary"}
_PERK = {"golf": "golf", "golf_simulator": "golf", "movie": "movie", "dining": "dining",
         "concierge": "concierge", "spa": "spa", "hotel": "hotel", "subscription": "subscription",
         "railway_lounge": "railway_lounge", "roadside_assist": "roadside_assist",
         "annual_voucher": "voucher", "forex_perk": "forex_perk", "contactless": "contactless",
         "virtual_card": "virtual_card", "emi_conversion": "emi_conversion"}
_CURR = {"reward_point": "reward_points", "reward_points": "reward_points", "point": "reward_points",
         "points": "reward_points", "cashback": "cashback", "cash_back": "cashback",
         "cashpoints": "cashpoints", "neucoins": "neucoins", "neucoin": "neucoins",
         "edge_miles": "edge_miles", "indusmiles": "indusmiles", "kotak_points": "kotak_points",
         "membership_rewards": "membership_rewards", "miles": "miles",
         "skywards_miles": "skywards_miles", "inr": "cashback"}


def _num(v):
    return v if isinstance(v, (int, float)) else None


def migrate(c: dict) -> dict:
    out: dict = {}
    for k in ("card_id", "issuer_id", "issuer_name", "card_name", "source_url",
              "apply_url", "image_url", "raw_text_sha256", "last_scraped_at", "first_seen_at"):
        if c.get(k) not in (None, ""):
            out[k] = c[k]
    out["category"] = c.get("category") if c.get("category") in _CAT else "credit"
    out["status"] = c.get("status", "active")
    if c.get("network"):
        n = _NET.get(slug(c["network"]))
        if n:
            out["network"] = n
    if c.get("card_material") and slug(c["card_material"]) in ("metal", "plastic", "virtual"):
        out["card_material"] = slug(c["card_material"])
    if c.get("segment"):
        s = _SEG.get(c["segment"], c["segment"])
        if s in ("entry", "mid", "premium", "super_premium", "invite_only", "student", "secured", "nri"):
            out["segment"] = s

    # fees
    f = c.get("fees") or {}
    nf = {}
    for ok, nk in (("joining_fee_inr", "joining_inr"), ("annual_fee_inr", "annual_inr"),
                   ("renewal_fee_inr", "renewal_inr"), ("fee_waiver_spend_inr", "waiver_spend_inr"),
                   ("addon_card_fee_inr", "addon_inr"), ("fx_markup_pct", "fx_markup_pct"),
                   ("cash_advance_fee_pct", "cash_advance_pct"),
                   ("finance_charge_pct_mo", "finance_charge_pct_mo")):
        if _num(f.get(ok)) is not None:
            nf[nk] = f[ok]
    if isinstance(f.get("gst_extra"), bool):
        nf["gst_extra"] = f["gst_extra"]
    fsw = f.get("fuel_surcharge_waiver")
    if isinstance(fsw, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", fsw)
        if m:
            nf["fuel_surcharge_waiver_pct"] = float(m.group(1))
    elif _num(fsw) is not None:
        nf["fuel_surcharge_waiver_pct"] = fsw
    if nf:
        out["fees"] = nf

    # rewards (keep reliable atoms; drop messy accelerated text → re-derived by LLM)
    r = c.get("rewards") or {}
    nr = {}
    if r.get("currency"):
        cur = _CURR.get(slug(r["currency"]))
        if cur:
            nr["currency"] = cur
    if _num(r.get("point_value_inr")) is not None:
        nr["unit_value_inr"] = r["point_value_inr"]
    if _num(r.get("base_rate_pct")) is not None:
        nr["base_rate_pct"] = r["base_rate_pct"]
    if isinstance(r.get("expiry_months"), int):
        nr["expiry_months"] = r["expiry_months"]
    if nr:
        out["rewards"] = nr

    # lounge
    l = c.get("lounge_access") or {}
    nl = {}
    if _num(l.get("domestic_visits_year")) is not None:
        nl["domestic_year"] = int(l["domestic_visits_year"])
    if _num(l.get("international_visits_year")) is not None:
        nl["international_year"] = int(l["international_visits_year"])
    if isinstance(l.get("guest_allowed"), bool):
        nl["guest_allowed"] = l["guest_allowed"]
    if _num(l.get("spend_unlock_inr")) is not None:
        nl["spend_unlock_inr"] = l["spend_unlock_inr"]
    if l.get("unlimited"):
        nl["unlimited_domestic"] = True
    if l.get("program"):
        ps = slug(l["program"])
        if "priority" in ps:
            nl["program"] = "priority_pass"
        elif "dreamfolks" in ps:
            nl["program"] = "dreamfolks"
        elif "lounge" in ps and "key" in ps:
            nl["program"] = "loungekey"
    if nl:
        out["lounge_access"] = nl

    # insurance
    ins = c.get("insurance") or {}
    ni = {}
    for ok, nk in (("air_accident_inr", "air_accident_inr"),
                   ("lost_card_inr", "lost_card_liability_inr"),
                   ("purchase_protection_inr", "purchase_protection_inr"),
                   ("travel_inr", "travel_inr")):
        if _num(ins.get(ok)) is not None:
            ni[nk] = ins[ok]
    if ni:
        out["insurance"] = ni

    # eligibility
    el = c.get("eligibility") or {}
    ne = {}
    for k in ("min_age", "max_age", "min_income_inr_year", "min_income_inr_month",
              "min_credit_score", "salaried", "self_employed"):
        if el.get(k) is not None:
            ne[k] = el[k]
    if ne:
        out["eligibility"] = ne

    # milestones (keep spend + period; reward string dropped → re-derived by LLM)
    ms = []
    for m in (c.get("milestones") or []):
        if isinstance(m, dict) and _num(m.get("spend_inr")) is not None:
            e = {"spend_inr": m["spend_inr"]}
            if m.get("period") in _PERIODS:
                e["period"] = m["period"]
            ms.append(e)
    if ms:
        out["milestones"] = ms

    # perks → {kind} only
    pk, seen = [], set()
    for p in (c.get("perks") or []):
        if isinstance(p, dict):
            k = _PERK.get(p.get("kind"))
            if k and k not in seen:
                seen.add(k)
                pk.append({"kind": k})
    if pk:
        out["perks"] = pk

    # partner_offers → {partner} token only
    po, seenp = [], set()
    for o in (c.get("partner_offers") or []):
        if isinstance(o, dict) and o.get("partner"):
            pt = slug(o["partner"])
            if pt and pt not in seenp:
                seenp.add(pt)
                po.append({"partner": pt})
    if po:
        out["partner_offers"] = po

    if c.get("highlights"):
        out["highlights"] = c["highlights"]
    return out


def main() -> None:
    cards = json.loads(SRC.read_text())
    v = Draft7Validator(SCHEMA)
    migrated = [migrate(c) for c in cards]
    bad = [(m.get("card_id"), [e.message for e in v.iter_errors(m)][:2]) for m in migrated if list(v.iter_errors(m))]
    SRC.write_text(json.dumps(migrated, indent=2, default=str))
    print(f"migrated {len(migrated)} cards; {len(migrated)-len(bad)} valid, {len(bad)} still failing")
    for cid, errs in bad[:10]:
        print("  ✗", cid, errs)


if __name__ == "__main__":
    main()
