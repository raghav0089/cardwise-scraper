"""HDFC detail-page benefit extraction spot-check.

Fetches a handful of HDFC credit card detail pages and shows what benefits,
fees, and rewards the extractor pulls out — lets us verify accuracy.
"""
from src.fetch import fetch
from src.banks.hdfc import extractor as hdfc_ext

TEST_URLS = [
    "https://www.hdfc.bank.in/credit-cards/regalia-gold-credit-card",
    "https://www.hdfc.bank.in/credit-cards/millennia-credit-card",
    "https://www.hdfc.bank.in/credit-cards/diners-club-black-credit-card",
    "https://www.hdfc.bank.in/credit-cards/tata-neu-infinity-hdfc-bank-credit-card",
    "https://www.hdfc.bank.in/credit-cards/swiggy-hdfc-bank-credit-card",
]

for url in TEST_URLS:
    result = fetch(url)
    page = {
        "source_url": url,
        "markdown": result.text,
        "html": result.html,
        "is_listing": False,
        "issuer_id": "hdfc",
        "issuer_name": "HDFC Bank",
    }
    cards = hdfc_ext.extract([page])
    if not cards:
        print(f"NO CARD: {url}\n")
        continue

    c = cards[0]
    fees    = c.get("fees", {})
    rewards = c.get("rewards", {})
    lounge  = c.get("lounge_access", {})

    print(f"=== {c['card_name']} ===")
    print(f"  category    : {c.get('category')}")
    print(f"  network     : {c.get('network')}")
    print(f"  segment     : {c.get('segment')}")
    print(f"  joining_fee : ₹{fees.get('joining_fee_inr', '?')}")
    print(f"  annual_fee  : ₹{fees.get('annual_fee_inr', '?')}")
    print(f"  fee_waiver  : ₹{fees.get('fee_waiver_spend_inr', '?')} annual spend")
    print(f"  base_rate   : {rewards.get('base_rate_pct', '?')}%")
    print(f"  point_value : ₹{rewards.get('point_value_inr', '?')}")
    accel = rewards.get("accelerated", [])
    for a in accel[:4]:
        rate = f"{a['rate_pct']}%" if a.get("rate_pct") else a.get("notes", "?")
        print(f"  accel       : {a['category']} → {rate}")
    if lounge:
        if lounge.get("unlimited"):
            print(f"  lounge      : UNLIMITED  prog={lounge.get('program','')}")
        else:
            dom  = lounge.get("domestic_visits_year", "?")
            intl = lounge.get("international_visits_year", "?")
            prog = lounge.get("program", "")
            note = lounge.get("domestic_visits_note", "")
            print(f"  lounge      : domestic={dom}/yr  intl={intl}/yr  prog={prog}  {note}")
    else:
        print(f"  lounge      : none")
    print(f"  welcome     : {str(c.get('welcome_benefit', ''))[:80]}")
    print()
