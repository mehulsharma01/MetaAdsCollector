"""Merge overlapping discovery results into one clinic per real-world site.

The discovery grid overlaps circles on purpose, so the same clinic arrives
many times. Four matching signals are applied in order of reliability:

1. ``place_id`` -- Google's own identifier. Exact, never wrong.
2. ``cid`` -- the Maps customer ID. Also exact.
3. ``domain + phone`` -- catches the same clinic listed twice by Google
   under slightly different names.
4. normalised ``name + borough`` -- last resort for listings with no
   website and no phone.

The one case that must NOT be merged is a genuine multi-branch group.
"Dr. Joy Dental Clinic, Mirdif" and "Dr. Joy Dental Clinic, BurJuman" share
a domain and a toll-free number but are two separate clinics at two
addresses. They are kept as separate rows and linked by ``group_key``, which
:mod:`score` uses to count locations. Rule 3 therefore requires the
coordinates to be within ``SAME_SITE_KM`` of each other.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

# Two listings sharing a domain and phone are the same physical clinic only
# if they are also within this distance. Beyond it they are branches.
SAME_SITE_KM = 0.4

_NOISE = re.compile(
    r"\b(l\.?l\.?c|llc|fz\s?llc|fzc|dmcc|branch|br|the|clinic|dental|dentistry"
    r"|centre|center|medical|polyclinic|hospital|est|establishment)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Reduce a clinic name to a comparable core.

    Strips legal suffixes, branch markers and the generic dental vocabulary
    that nearly every clinic shares, so that "Pearl Dental Clinic L.L.C"
    and "Pearl Dental Centre (Branch)" collapse to the same key.
    """
    lowered = name.lower()
    lowered = _NOISE.sub(" ", lowered)
    return _NON_ALNUM.sub("", lowered)


def normalize_phone(phone: str | None) -> str:
    """Reduce a phone number to digits, dropping the UAE country code.

    ``+971 4 334 5955``, ``0097143345955`` and ``043345955`` all normalise
    to ``43345955``.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    for prefix in ("00971", "971"):
        if digits.startswith(prefix):
            digits = digits[len(prefix) :]
            break
    return digits.lstrip("0")


def normalize_domain(url_or_domain: str | None) -> str:
    """Reduce a website to a bare registrable domain.

    Drops scheme, ``www.``, ``m.`` and any path. Social profiles are
    returned as-is so that a clinic whose only "website" is a Facebook page
    does not merge with every other such clinic.
    """
    if not url_or_domain:
        return ""
    value = url_or_domain.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/")[0]
    value = re.sub(r"^(www|m)\.", "", value)
    return value


# Domains that are not a clinic's own website and must never be used as a
# merge key -- thousands of clinics "have" a facebook.com website.
SOCIAL_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "wa.me",
    "api.whatsapp.com",
    "business.site",
    "sites.google.com",
    "linktr.ee",
}


def is_own_domain(domain: str) -> bool:
    """True when a domain looks like the clinic's own site, not a social profile."""
    if not domain or "." not in domain:
        return False
    return domain not in SOCIAL_DOMAINS


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _richness(item: dict[str, Any]) -> int:
    """Score how complete a listing is, to pick the best of several duplicates.

    Google returns a fuller record for some queries than others; when two
    copies of the same clinic disagree, keep the one carrying more fields.
    """
    score = 0
    for field in ("phone", "url", "description", "main_image", "work_time"):
        if item.get(field):
            score += 1
    score += min(len(item.get("services") or []), 20)
    score += min(len(item.get("category_ids") or []), 10)
    if (item.get("rating") or {}).get("votes_count"):
        score += 2
    return score


def _merge(primary: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Fill gaps in ``primary`` from ``other`` without overwriting real values."""
    merged = dict(primary)
    for key, value in other.items():
        if value in (None, "", [], {}):
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    # Union the category lists -- different queries surface different ones,
    # and the union is what the cosmetic scoring reads.
    cats = list(dict.fromkeys((primary.get("category_ids") or []) + (other.get("category_ids") or [])))
    if cats:
        merged["category_ids"] = cats
    # Keep the query labels that found this clinic, for debugging coverage.
    sources = list(
        dict.fromkeys((primary.get("_sources") or []) + (other.get("_sources") or []))
    )
    if sources:
        merged["_sources"] = sources
    return merged


def deduplicate(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse raw discovery results into unique clinics.

    Args:
        items: Raw business listing dicts from DataForSEO, in any order and
            with any amount of duplication.

    Returns:
        One dict per physical clinic. Each carries an added ``group_key``
        naming the brand it belongs to, so multi-branch groups can be
        counted without being merged.
    """
    by_key: dict[str, dict[str, Any]] = {}
    # Secondary indexes for the softer matching rules.
    by_domain_phone: dict[tuple[str, str], list[str]] = {}
    by_name_area: dict[tuple[str, str], list[str]] = {}

    for item in items:
        place_id = (item.get("place_id") or "").strip()
        cid = (item.get("cid") or "").strip()
        domain = normalize_domain(item.get("domain") or item.get("url"))
        phone = normalize_phone(item.get("phone"))
        name_key = normalize_name(item.get("title") or "")
        borough = ((item.get("address_info") or {}).get("borough") or "").lower().strip()
        lat = item.get("latitude")
        lng = item.get("longitude")

        # Rule 1 and 2: exact identifiers.
        key = f"pid:{place_id}" if place_id else (f"cid:{cid}" if cid else "")

        # Rule 3: same own-domain and same phone, at the same coordinates.
        if not key and is_own_domain(domain) and phone:
            for candidate in by_domain_phone.get((domain, phone), []):
                existing = by_key[candidate]
                if lat is None or existing.get("latitude") is None:
                    key = candidate
                    break
                if haversine_km(lat, lng, existing["latitude"], existing["longitude"]) <= SAME_SITE_KM:
                    key = candidate
                    break

        # Rule 4: same normalised name in the same borough.
        if not key and name_key and borough:
            for candidate in by_name_area.get((name_key, borough), []):
                existing = by_key[candidate]
                if lat is None or existing.get("latitude") is None:
                    key = candidate
                    break
                if haversine_km(lat, lng, existing["latitude"], existing["longitude"]) <= SAME_SITE_KM:
                    key = candidate
                    break

        if not key:
            key = f"name:{name_key}:{borough}:{lat}:{lng}"

        if key in by_key:
            # Keep whichever copy is richer, then backfill from the other.
            existing = by_key[key]
            if _richness(item) > _richness(existing):
                by_key[key] = _merge(item, existing)
            else:
                by_key[key] = _merge(existing, item)
        else:
            by_key[key] = dict(item)

        if is_own_domain(domain) and phone:
            by_domain_phone.setdefault((domain, phone), []).append(key)
        if name_key and borough:
            by_name_area.setdefault((name_key, borough), []).append(key)

    clinics = list(by_key.values())

    # Brand grouping: prefer the domain, fall back to the normalised name.
    # This is what turns three "Dr. Joy" rows into "a 3-location group"
    # rather than three independent single-site prospects.
    for clinic in clinics:
        domain = normalize_domain(clinic.get("domain") or clinic.get("url"))
        if is_own_domain(domain):
            clinic["group_key"] = domain
        else:
            clinic["group_key"] = normalize_name(clinic.get("title") or "") or "unknown"

    counts: dict[str, int] = {}
    for clinic in clinics:
        counts[clinic["group_key"]] = counts.get(clinic["group_key"], 0) + 1
    for clinic in clinics:
        clinic["location_count"] = counts[clinic["group_key"]]

    return clinics
