"""
Canonical card schema — every scraper must return this shape.
Any field that can't be determined should be None, not absent.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RewardRule:
    """
    One reward rule. A card can have many.
    Structured so the recommendation engine can calculate actual value.
    """
    channel:            Optional[str] = None   # upi, online, offline, all
    merchant_category:  Optional[str] = None   # dining, travel, fuel, grocery, etc
    network:            Optional[str] = None   # rupay, visa, mastercard (for UPI cashback)
    reward_type:        str = "points"         # points, cashback, miles
    reward_value:       Optional[float] = None # e.g. 4 (for 4 points per ₹150)
    reward_per_spend:   Optional[float] = None # e.g. 150 (points per ₹150 spent)
    reward_percent:     Optional[float] = None # e.g. 2.0 (for 2% cashback)
    reward_currency:    Optional[str] = None   # "RP", "EdgeMiles", "INR", "Accor"
    cap_per_month:      Optional[float] = None # max reward per month
    min_transaction:    Optional[float] = None # minimum txn amount to earn
    valid_from:         Optional[str] = None   # ISO date
    valid_until:        Optional[str] = None   # ISO date — for campaigns
    campaign_type:      Optional[str] = None   # permanent, temporary, seasonal


@dataclass
class LoungeAccess:
    domestic_per_quarter:    Optional[int] = None   # 999 = unlimited
    international_per_year:  Optional[int] = None
    requires_min_spend:      Optional[float] = None
    network:                 Optional[str] = None   # Priority Pass, DreamFolks, etc
    notes:                   Optional[str] = None


@dataclass
class MilestoneBenefit:
    spend_threshold:  float = 0
    benefit:          str = ""
    benefit_type:     Optional[str] = None  # voucher, points, fee_waiver, gift


@dataclass
class Card:
    # ── Identity ─────────────────────────────────────
    card_id:          str = ""         # snake_case: hdfc_regalia_gold
    card_name:        str = ""
    bank:             str = ""
    issuer_type:      str = "bank"     # bank, fintech, nbfc
    network:          str = ""         # Visa, Mastercard, Amex, RuPay
    card_type:        str = "credit"
    co_branded:       bool = False
    co_brand_partner: Optional[str] = None  # e.g. "Swiggy", "Tata Neu"
    variant:          Optional[str] = None  # Gold, Platinum, Signature

    # ── Fees ─────────────────────────────────────────
    joining_fee:          Optional[float] = None
    annual_fee:           Optional[float] = None
    annual_fee_second_yr: Optional[float] = None
    fee_waiver_condition: Optional[str] = None
    fee_waiver_spend:     Optional[float] = None   # spend threshold for waiver

    # ── Rewards ──────────────────────────────────────
    reward_rules:     list = field(default_factory=list)   # list of RewardRule dicts
    reward_program:   Optional[str] = None   # "SmartBuy", "EdgeRewards", etc
    point_value_inr:  Optional[float] = None # 1 point = ₹X

    # ── Lounge ───────────────────────────────────────
    lounge_access:        Optional[dict] = None   # LoungeAccess dict

    # ── Key Benefits (structured) ─────────────────────
    fuel_surcharge_waiver:    bool = False
    fuel_waiver_percent:      Optional[float] = None
    fuel_waiver_cap_monthly:  Optional[float] = None
    movie_benefit:            bool = False
    movie_benefit_details:    Optional[str] = None
    golf_benefit:             bool = False
    golf_rounds_per_year:     Optional[int] = None
    forex_markup_percent:     Optional[float] = None   # 0 = zero forex
    insurance_cover:          bool = False
    insurance_cover_amount:   Optional[float] = None
    concierge:                bool = False
    emi_on_call:              bool = False

    # ── Welcome & Milestone Benefits ─────────────────
    welcome_benefits:     list = field(default_factory=list)   # strings
    milestone_benefits:   list = field(default_factory=list)   # MilestoneBenefit dicts

    # ── Eligibility ───────────────────────────────────
    min_income_annual:    Optional[float] = None
    min_age:              Optional[int] = None
    max_age:              Optional[int] = None
    employment_type:      Optional[str] = None   # salaried, self-employed, both

    # ── Links ─────────────────────────────────────────
    apply_link:           Optional[str] = None
    official_page:        Optional[str] = None
    tnc_url:              Optional[str] = None

    # ── Meta ──────────────────────────────────────────
    active:               bool = True
    source_hash:          Optional[str] = None
    source_url:           Optional[str] = None
    last_verified_at:     Optional[str] = None
    version:              int = 1

    def to_dict(self) -> dict:
        return asdict(self)
