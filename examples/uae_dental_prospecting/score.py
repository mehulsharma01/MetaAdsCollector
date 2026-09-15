"""Score a clinic 0-100 on how likely it is to buy paid patient acquisition.

The score answers one question: *if I send this clinic a message tomorrow,
what are the odds it turns into a paying retainer?* It is not a measure of
how good the clinic is.

Six components, weighted by how much each actually predicts a sale:

    high_ticket        25   Does it sell treatments worth advertising?
    demand_proof       20   Do patients already choose it?
    capability         15   Can it afford a retainer?
    opportunity        20   Is there a gap I can visibly fix?
    reachability       10   Can I get to the decision maker?
    manageable_size    10   Is the sale short enough to close alone?

The weighting is deliberate. ``high_ticket`` and ``opportunity`` together
are 45 points because a clinic doing AED 300 cleanings has no room for a
retainer no matter how well reviewed it is, and a clinic with no visible
gap gives you nothing to open a conversation with.

Every number below is a prior, not a measurement. They are starting weights
to be corrected once real reply rates come in -- see ``recalibrate`` at the
bottom of this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dedupe import is_own_domain, normalize_domain, normalize_phone

# --- Component 1: high-ticket treatment signal (max 25) ------------------

CATEGORY_POINTS = {
    "cosmetic_dentist": 8,
    "dental_implants_periodontist": 7,
    "dental_implants_provider": 7,
    "prosthodontist": 6,
    "orthodontist": 5,
    "periodontist": 4,
    "oral_surgeon": 4,
    "teeth_whitening_service": 2,
}

# Treatment names worth advertising, matched against the clinic's own
# service list and description. Weighted by typical ticket size in the UAE:
# a veneer case is a five-figure decision, a cleaning is not.
TREATMENT_PATTERNS = {
    "veneers": (re.compile(r"\bveneers?\b", re.I), 5),
    "hollywood_smile": (re.compile(r"hollywood smile", re.I), 5),
    "smile_makeover": (re.compile(r"smile (makeover|design|designing)", re.I), 4),
    "implants": (re.compile(r"\b(dental )?implants?\b", re.I), 4),
    "full_mouth": (re.compile(r"full[- ]mouth|full arch|all[- ]on[- ]\d", re.I), 5),
    "invisalign": (re.compile(r"invisalign|clear aligners?", re.I), 3),
    "zirconia": (re.compile(r"zirconia|zirconium", re.I), 2),
    "orthodontics": (re.compile(r"braces|orthodontic", re.I), 2),
}

HIGH_TICKET_MAX = 25


def _text_blob(clinic: dict[str, Any]) -> str:
    """Everything the clinic says about itself, as one searchable string."""
    parts = [clinic.get("title") or "", clinic.get("description") or ""]
    for service in clinic.get("services") or []:
        parts.append(service.get("title") or "")
        parts.append(service.get("snippet") or "")
    for topic in (clinic.get("place_topics") or {}):
        parts.append(topic)
    return " ".join(parts)


def score_high_ticket(clinic: dict[str, Any]) -> tuple[int, list[str]]:
    """Points for selling treatments that justify an ad budget."""
    points = 0
    found: list[str] = []

    for category in clinic.get("category_ids") or []:
        if category in CATEGORY_POINTS:
            points += CATEGORY_POINTS[category]
            found.append(category)

    blob = _text_blob(clinic)
    for name, (pattern, weight) in TREATMENT_PATTERNS.items():
        if pattern.search(blob):
            points += weight
            found.append(name)

    return min(points, HIGH_TICKET_MAX), found


# --- Component 2: demand proof (max 20) ----------------------------------

def score_demand(clinic: dict[str, Any]) -> tuple[int, str]:
    """Points for review volume, discounted when the rating is poor.

    Review count is the single best free proxy for patient throughput.
    A low rating halves the score: a clinic with 800 reviews at 3.2 stars
    has a reputation problem that ads will amplify, not fix, and it is a
    bad first client.
    """
    rating = clinic.get("rating") or {}
    votes = rating.get("votes_count") or 0
    value = rating.get("value") or 0.0

    if votes >= 1000:
        points = 20
    elif votes >= 400:
        points = 17
    elif votes >= 150:
        points = 12
    elif votes >= 50:
        points = 6
    elif votes >= 10:
        points = 3
    else:
        points = 0

    note = f"{votes} reviews at {value}"
    if value and value < 4.0:
        points = int(points * 0.5)
        note += " (halved: rating below 4.0)"

    return points, note


# --- Component 3: financial capability (max 15) --------------------------

def score_capability(clinic: dict[str, Any]) -> tuple[int, list[str]]:
    """Points for visible signs the clinic spends money on itself."""
    points = 0
    signals: list[str] = []

    domain = normalize_domain(clinic.get("domain") or clinic.get("url"))
    if is_own_domain(domain):
        points += 5
        signals.append("own website")
    elif domain:
        signals.append("social-only web presence")

    priced = [s for s in (clinic.get("services") or []) if s.get("price")]
    if priced:
        points += 4
        signals.append(f"{len(priced)} published prices")

    photos = clinic.get("total_photos") or 0
    if photos >= 40:
        points += 3
        signals.append(f"{photos} photos")
    elif photos >= 15:
        points += 2
        signals.append(f"{photos} photos")

    if clinic.get("is_claimed"):
        points += 3
        signals.append("claimed listing")

    return min(points, 15), signals


# --- Component 4: opportunity (max 20) -----------------------------------
#
# This is the component that decides what you say in the first message, so
# it is worth understanding rather than just reading the number.
#
# A clinic already running Meta ads scores highest. That is counter-
# intuitive -- it means a competitor is in the account -- but a clinic
# running ads has proven three things you would otherwise have to prove:
# it has budget, it has internal approval to spend it, and it believes paid
# acquisition works. Displacing an incumbent is a far shorter sale than
# creating a category.

def score_opportunity(clinic: dict[str, Any]) -> tuple[int, list[str]]:
    """Points for a visible, nameable gap in the clinic's acquisition."""
    points = 0
    signals: list[str] = []

    meta = clinic.get("_meta_ads") or {}
    running = meta.get("running")
    ad_count = meta.get("active_ad_count") or 0
    has_video = meta.get("has_video")

    domain = normalize_domain(clinic.get("domain") or clinic.get("url"))
    strong_web = is_own_domain(domain)

    if running:
        points += 12
        signals.append(f"running {ad_count} Meta ads")
        # Thin creative rotation is the most credible thing to lead with,
        # because you can show it to them in 30 seconds from a public link.
        if ad_count <= 3:
            points += 5
            signals.append("thin creative rotation")
        if has_video is False:
            points += 3
            signals.append("no video creative")
    elif running is False:
        if strong_web:
            points += 8
            signals.append("no Meta ads, but strong web presence")
        else:
            points += 3
            signals.append("no Meta ads, weak web presence")
    else:
        signals.append("Meta ad status unknown")

    # No bookable path from the listing is a conversion gap you can fix
    # without touching their ad account at all.
    if not clinic.get("phone"):
        points += 2
        signals.append("no phone on listing")

    return min(points, 20), signals


# --- Component 5: reachability (max 10) ----------------------------------

# UAE mobile prefixes, which are the numbers that reach a person on
# WhatsApp. A landline (04, 02, 06, 07, 09) reaches a receptionist.
MOBILE_PREFIXES = ("50", "52", "54", "55", "56", "58")

DOCTOR_NAME = re.compile(r"\b(dr\.?|doctor|د\.)\s", re.I)


def score_reachability(clinic: dict[str, Any]) -> tuple[int, list[str]]:
    """Points for being able to reach a decision maker directly."""
    points = 0
    signals: list[str] = []

    phone = normalize_phone(clinic.get("phone"))
    if phone:
        points += 3
        signals.append("phone listed")
        if phone[:2] in MOBILE_PREFIXES:
            points += 4
            signals.append("mobile number (WhatsApp reachable)")

    # A clinic named after its dentist is almost always owner-operated,
    # which means the person who answers can also say yes.
    if DOCTOR_NAME.search(clinic.get("title") or ""):
        points += 3
        signals.append("owner-operator (doctor-named clinic)")

    return min(points, 10), signals


# --- Component 6: manageable size (max 10) -------------------------------

def score_size(clinic: dict[str, Any]) -> tuple[int, str]:
    """Points for being small enough to sell to without a procurement process.

    A single-site clinic can be closed by one person in one conversation.
    A 12-branch hospital group cannot, and chasing one will eat the whole
    deadline.
    """
    count = clinic.get("location_count") or 1
    if count == 1:
        return 10, "single location"
    if count <= 4:
        return 7, f"{count} locations"
    if count <= 9:
        return 3, f"{count} locations"
    return 0, f"{count} locations (group -- long sales cycle)"


# --- Disqualifiers -------------------------------------------------------

EXCLUDE_CATEGORIES = {
    "hospital",
    "general_hospital",
    "government_hospital",
    "military_hospital",
    "university_hospital",
    "dental_school",
    "dental_laboratory",
    "dental_supply_store",
}


def disqualify(clinic: dict[str, Any]) -> str | None:
    """Return a reason to drop this clinic entirely, or ``None`` to keep it."""
    work = ((clinic.get("work_time") or {}).get("work_hours") or {})
    if work.get("current_status") == "closed_forever":
        return "permanently closed"

    categories = set(clinic.get("category_ids") or [])
    if categories & EXCLUDE_CATEGORIES:
        return "hospital, school or supplier, not a private clinic"

    if not (clinic.get("title") or "").strip():
        return "no name"

    return None


@dataclass
class ScoreCard:
    """The full scoring breakdown for one clinic."""

    total: int = 0
    tier: str = "C"
    high_ticket: int = 0
    demand: int = 0
    capability: int = 0
    opportunity: int = 0
    reachability: int = 0
    size: int = 0
    disqualified: str | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        """A one-line human summary, for the CRM's notes column."""
        return "; ".join(self.signals)


def tier_for(total: int) -> str:
    """Map a score to an outreach tier.

    The thresholds are set so that tier A comes out at roughly the size of
    a week's outreach rather than at a round number.
    """
    if total >= 70:
        return "A"
    if total >= 50:
        return "B"
    return "C"


def score_clinic(clinic: dict[str, Any]) -> ScoreCard:
    """Score one clinic and return the full breakdown.

    Args:
        clinic: A deduplicated clinic dict, optionally carrying a
            ``_meta_ads`` key from :mod:`enrich_meta`. Without it the
            opportunity component scores 0 and every clinic looks
            equally cold, so run enrichment before scoring the A list.

    Returns:
        A :class:`ScoreCard`. Disqualified clinics come back with
        ``total=0`` and a populated ``disqualified`` reason.
    """
    reason = disqualify(clinic)
    if reason:
        return ScoreCard(total=0, tier="X", disqualified=reason, signals=[reason])

    card = ScoreCard()
    signals: list[str] = []

    card.high_ticket, found = score_high_ticket(clinic)
    if found:
        signals.append("sells: " + ", ".join(sorted(set(found))[:6]))

    card.demand, demand_note = score_demand(clinic)
    signals.append(demand_note)

    card.capability, cap_signals = score_capability(clinic)
    signals.extend(cap_signals)

    card.opportunity, opp_signals = score_opportunity(clinic)
    signals.extend(opp_signals)

    card.reachability, reach_signals = score_reachability(clinic)
    signals.extend(reach_signals)

    card.size, size_note = score_size(clinic)
    signals.append(size_note)

    card.total = (
        card.high_ticket
        + card.demand
        + card.capability
        + card.opportunity
        + card.reachability
        + card.size
    )

    # A clinic nobody has reviewed cannot be verified as a going concern,
    # whatever else it scores.
    votes = (clinic.get("rating") or {}).get("votes_count") or 0
    if votes < 10:
        card.total = min(card.total, 35)
        signals.append("capped at 35: fewer than 10 reviews")

    card.tier = tier_for(card.total)
    card.signals = signals
    return card


def recalibrate(results: list[tuple[int, bool]]) -> str:
    """Report whether the score is actually predicting replies.

    Feed this the real outcome of your outreach once you have sent 100
    messages: a list of ``(score, got_a_reply)`` pairs. If the A band does
    not reply at a noticeably higher rate than the B band, the weights
    above are wrong for this market and should be changed. Until you have
    that data, every weight in this module is a guess.

    Args:
        results: One ``(score, replied)`` pair per contacted clinic.

    Returns:
        A short text report comparing reply rates by tier.
    """
    if not results:
        return "No outreach results yet -- weights remain unvalidated guesses."

    bands: dict[str, list[bool]] = {"A": [], "B": [], "C": []}
    for total, replied in results:
        bands[tier_for(total)].append(replied)

    lines = ["Reply rate by tier (n = contacted):"]
    for tier in ("A", "B", "C"):
        sample = bands[tier]
        if not sample:
            lines.append(f"  {tier}: no data")
            continue
        rate = sum(sample) / len(sample)
        lines.append(f"  {tier}: {rate:.0%} of {len(sample)}")

    a_rate = (sum(bands["A"]) / len(bands["A"])) if bands["A"] else 0.0
    b_rate = (sum(bands["B"]) / len(bands["B"])) if bands["B"] else 0.0
    if bands["A"] and bands["B"] and a_rate <= b_rate:
        lines.append("")
        lines.append(
            "Tier A is not outperforming tier B. The weights are not capturing "
            "what makes a UAE dental clinic reply. Re-weight before sending more."
        )
    return "\n".join(lines)
