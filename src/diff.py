"""Compare new card record vs the one currently in DDB and emit a change
event for devaluations, fee hikes, new perks, status flips."""
from __future__ import annotations
import datetime as dt
from typing import Any

WATCH_PATHS = [
    ("fees", "joining_fee_inr"),
    ("fees", "annual_fee_inr"),
    ("fees", "fee_waiver_spend_inr"),
    ("fees", "fx_markup_pct"),
    ("rewards", "base_rate_pct"),
    ("rewards", "point_value_inr"),
    ("lounge_access", "domestic_visits_year"),
    ("lounge_access", "international_visits_year"),
    ("status",),
]

def _get(d: dict, path: tuple) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict): return None
        cur = cur.get(k)
    return cur

def diff_card(old: dict | None, new: dict) -> list[dict]:
    if not old: return []
    out = []
    for p in WATCH_PATHS:
        ov, nv = _get(old, p), _get(new, p)
        if ov is None and nv is None: continue
        if ov == nv: continue
        change_type = "devaluation" if _is_devaluation(p, ov, nv) else "change"
        out.append({
            "change_id": f"{new['card_id']}#{'.'.join(p)}#{dt.datetime.utcnow().isoformat()}",
            "card_id": new["card_id"],
            "field": ".".join(p),
            "old_value": ov,
            "new_value": nv,
            "change_type": change_type,
            "detected_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source_url": new.get("source_url"),
        })
    return out

def _is_devaluation(path: tuple, ov, nv) -> bool:
    if path == ("status",): return nv in ("discontinued", "invite_only")
    if not isinstance(ov, (int, float)) or not isinstance(nv, (int, float)): return False
    # higher fee/markup OR lower rewards/lounge ⇒ devaluation
    if path[0] == "fees" and nv > ov: return True
    if path[0] in ("rewards", "lounge_access") and nv < ov: return True
    return False
