"""Decide whether a clinic can actually afford an AED 2,000/month retainer.

The scoring in :mod:`score` answers "would this clinic be a good client?"
This module answers a narrower and more brutal question: **will this clinic
pay, or will it waste three weeks of conversations and then ask for a
discount?**

They are different questions and they disagree often. A clinic with 900
five-star reviews in Naif advertising "Teeth Cleaning AED 99" scores well
on demand and terribly here, because its entire business model is volume
at the lowest price in the emirate. It cannot fund a retainer and it will
not try.

The evidence hierarchy, strongest first:

1. **Already paying someone for marketing.** Organic keyword count is the
   proxy. A UAE dental site ranking for 200+ keywords did not get there by
   accident; an agency is on a monthly retainer right now. That is proof of
   both budget and willingness, and it is the only signal here that is
   nearly impossible to fake.
2. **Price positioning.** What the clinic charges caps what it can pay.
   Published prices are the clinic telling you its ticket size for free.
3. **Catchment rent.** A clinic on Jumeirah Beach Road pays several times
   the rent of one in Al Muteena. Rent tracks revenue.
4. **Scale.** Two to four branches means real money and still one decision
   maker.
5. **Patient volume.** Review count as a throughput proxy.

Everything here is inference from public data. None of it is the clinic's
P&L. Treat the output as a queue order, not a credit check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- Hard disqualifiers ---------------------------------------------------

# Names that advertise a price are the clearest self-identification a
# discount clinic can give you. Real examples from the collected data:
# "Best Dental Clinic LLC in Dubai - Teeth Cleaning AED99 - Al Karama",
# "Dr Harsha Braces at AED299 - Top Orthodontist Dubai".
PRICE_LED_NAME = re.compile(
    r"aed\s?\d{1,3}(?!\d)"            # AED 99, AED299 -- three digits or fewer
    r"|\bdhs?\s?\d{1,3}(?!\d)"
    r"|\bfree\s+(consultation|checkup|check-up|cleaning|scaling)"
    r"|\bcheap\b|\baffordable\b|\bbudget\b|\blowest\s+price|\bdiscount\b"
    r"|\bbest\s+price|\boffer[s]?\b",
    re.IGNORECASE,
)

# A clinic whose dearest published treatment is under this is not selling
# anything that can carry an ad budget.
MIN_TOP_PRICE_AED = 1500

# Above this many branches you are selling to a committee, not a dentist.
MAX_MANAGEABLE_LOCATIONS = 9


# --- Catchment tiers ------------------------------------------------------
#
# Rent is the cleanest public proxy for revenue per chair. These lists are
# judgement calls about UAE commercial rent, not measurements.

PREMIUM_AREAS = {
    "jumeirah", "umm suqeim", "al wasl", "business bay", "downtown", "difc",
    "dubai marina", "jbr", "marsa dubai", "palm jumeirah", "al safa",
    "al manara", "emirates hills", "jumeirah lake", "jlt", "city walk",
    "al bateen", "al reem", "khalifa city", "saadiyat", "al khalidiyah",
    "corniche", "al raha", "al mushrif", "madinat jumeirah", "al sufouh",
    "mirdif", "arabian ranches", "motor city", "dubai hills",
}

VALUE_AREAS = {
    "naif", "deira", "port saeed", "al muraqqabat", "al rigga", "al muteena",
    "international city", "al qusais", "muwaileh", "mussafah", "al nahda",
    "al quoz", "sonapur", "industrial", "al fahidi", "hor al anz",
    "al baraha", "abu shagara", "al nahyan", "rolla", "al gubaiba",
}


def catchment_tier(area: str, address: str = "") -> int:
    """Classify the clinic's location as 1 (premium), 2 (mid) or 3 (value)."""
    haystack = f"{area} {address}".lower()
    for name in PREMIUM_AREAS:
        if name in haystack:
            return 1
    for name in VALUE_AREAS:
        if name in haystack:
            return 3
    return 2


# --- Price parsing --------------------------------------------------------

_PRICE = re.compile(r"AED\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def top_published_price(published_prices: str) -> float | None:
    """Return the highest published price, or None when none are published.

    Input is the pipeline's ``published_prices`` column, formatted as
    ``"Dental Implants: From AED 4,999.00 | Teeth Whitening: AED 999.00"``.
    """
    if not published_prices:
        return None
    values = []
    for raw in _PRICE.findall(published_prices):
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


# --- Component scores -----------------------------------------------------

def score_marketing_spend(organic_count: int, paid_count: int, organic_etv: float) -> tuple[int, list[str]]:
    """Points for demonstrable, ongoing spend on being found. Max 40.

    This is the heaviest component because it is the only one that proves
    the clinic already writes a monthly cheque to a marketing supplier.
    """
    points = 0
    signals: list[str] = []

    if organic_count >= 200:
        points += 25
        signals.append(f"ranks for {organic_count} keywords - agency on retainer")
    elif organic_count >= 100:
        points += 20
        signals.append(f"ranks for {organic_count} keywords - sustained SEO investment")
    elif organic_count >= 50:
        points += 12
        signals.append(f"ranks for {organic_count} keywords - some SEO work")
    elif organic_count >= 20:
        points += 6
        signals.append(f"ranks for {organic_count} keywords - minimal")
    else:
        signals.append(f"ranks for {organic_count} keywords - no marketing investment visible")

    if paid_count > 0:
        points += 10
        signals.append(f"buying {paid_count} paid keywords - already an advertiser")

    if organic_etv >= 1000:
        points += 5
        signals.append(f"est. {organic_etv:,.0f} monthly organic traffic value")

    return min(points, 40), signals


def score_price_position(top_price: float | None) -> tuple[int, list[str]]:
    """Points for charging enough that an ad budget can pay for itself. Max 25."""
    if top_price is None:
        # Most premium clinics deliberately do not publish prices. Absence is
        # genuinely neutral, so this scores mid rather than zero.
        return 8, ["no published prices (neutral)"]

    if top_price >= 5000:
        return 25, [f"top published price AED {top_price:,.0f} - premium positioning"]
    if top_price >= 3000:
        return 18, [f"top published price AED {top_price:,.0f}"]
    if top_price >= MIN_TOP_PRICE_AED:
        return 10, [f"top published price AED {top_price:,.0f} - mid market"]
    return 0, [f"top published price only AED {top_price:,.0f} - discount positioning"]


def score_catchment(tier: int) -> tuple[int, list[str]]:
    """Points for operating where rent implies revenue. Max 15."""
    if tier == 1:
        return 15, ["premium catchment"]
    if tier == 2:
        return 8, ["mid catchment"]
    return 0, ["value catchment - price-competitive area"]


def score_scale(locations: int) -> tuple[int, list[str]]:
    """Points for being big enough to fund, small enough to close. Max 10."""
    if 2 <= locations <= 4:
        return 10, [f"{locations} branches - real revenue, one decision maker"]
    if locations == 1:
        return 6, ["single site"]
    if locations <= MAX_MANAGEABLE_LOCATIONS:
        return 4, [f"{locations} branches - slower sale"]
    return 0, [f"{locations}+ branches - enterprise, committee sale"]


def score_volume(reviews: int) -> tuple[int, list[str]]:
    """Points for patient throughput. Max 10."""
    if reviews >= 800:
        return 10, [f"{reviews} reviews - high throughput"]
    if reviews >= 400:
        return 7, [f"{reviews} reviews"]
    if reviews >= 150:
        return 4, [f"{reviews} reviews"]
    return 0, [f"only {reviews} reviews"]


# --- Verdict --------------------------------------------------------------

@dataclass
class Affordability:
    """Whether this clinic can fund an AED 2,000/month retainer."""

    score: int = 0
    verdict: str = "UNPROVEN"
    marketing_spend: int = 0
    price_position: int = 0
    catchment: int = 0
    scale: int = 0
    volume: int = 0
    blockers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        return "; ".join(self.blockers + self.evidence)


def verdict_for(score: int) -> str:
    """Map a budget score to an outreach instruction."""
    if score >= 65:
        return "CAN PAY"
    if score >= 45:
        return "PROBABLY"
    if score >= 25:
        return "UNPROVEN"
    return "TOO CHEAP"


def assess(
    clinic_row: dict[str, Any],
    organic_count: int = 0,
    paid_count: int = 0,
    organic_etv: float = 0.0,
) -> Affordability:
    """Judge one clinic's ability to pay.

    Args:
        clinic_row: A row from the pipeline's CRM CSV.
        organic_count: Organic keywords, from bulk traffic estimation.
        paid_count: Paid keywords.
        organic_etv: Estimated monthly organic traffic value.

    Returns:
        An :class:`Affordability`. Any hard blocker forces ``TOO CHEAP``
        regardless of the numeric score, because these are conditions no
        amount of review count compensates for.
    """
    result = Affordability()
    name = clinic_row.get("clinic_name") or ""
    prices = clinic_row.get("published_prices") or ""
    top_price = top_published_price(prices)

    try:
        locations = int(clinic_row.get("number_of_locations") or 1)
    except (TypeError, ValueError):
        locations = 1
    try:
        reviews = int(clinic_row.get("number_of_reviews") or 0)
    except (TypeError, ValueError):
        reviews = 0

    # --- hard blockers ---
    # Two different kinds of "do not pursue", kept apart because they mean
    # opposite things. A discount clinic cannot fund the work; an enterprise
    # group can easily fund it but will not buy it from one person in the
    # time available.
    if PRICE_LED_NAME.search(name):
        result.blockers.append("BLOCKER: advertises price in its own name - discount model")
    if top_price is not None and top_price < MIN_TOP_PRICE_AED:
        result.blockers.append(
            f"BLOCKER: dearest published treatment is AED {top_price:,.0f} - no high-ticket offer"
        )
    if "no website" in (clinic_row.get("website_quality") or ""):
        result.blockers.append("BLOCKER: no website - nowhere to send paid traffic")

    too_big = locations > MAX_MANAGEABLE_LOCATIONS
    if too_big:
        result.blockers.append(f"BLOCKER: {locations} branches - enterprise procurement")

    # --- components ---
    result.marketing_spend, s1 = score_marketing_spend(organic_count, paid_count, organic_etv)
    result.price_position, s2 = score_price_position(top_price)
    result.catchment, s3 = score_catchment(
        catchment_tier(clinic_row.get("area") or "", clinic_row.get("address") or "")
    )
    result.scale, s4 = score_scale(locations)
    result.volume, s5 = score_volume(reviews)
    result.evidence = s1 + s2 + s3 + s4 + s5

    result.score = (
        result.marketing_spend
        + result.price_position
        + result.catchment
        + result.scale
        + result.volume
    )

    if too_big:
        result.verdict = "TOO BIG"
    elif result.blockers:
        result.verdict = "TOO CHEAP"
    else:
        result.verdict = verdict_for(result.score)
    return result
