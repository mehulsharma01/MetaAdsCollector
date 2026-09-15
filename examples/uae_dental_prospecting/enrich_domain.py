"""Enrichment stage: read a clinic's search footprint from its domain.

One request per domain returns both halves of the picture:

* ``organic.count`` -- how many keywords the site ranks for. This is the
  SEO-agency detector. A UAE dental site ranking for 300+ keywords did not
  get there by accident; somebody is being paid.
* ``paid.count`` -- how many keywords it buys. This is the Google Ads
  detector, and the gap between the two numbers is the sales argument.

The most interesting prospect is a clinic with a large organic footprint
and near-zero paid presence. It proves the clinic already invests in
acquisition and already believes search intent converts, while leaving the
entire paid channel unclaimed.

Interpretation caveats, because these are estimates and not the clinic's
own numbers:

* ``etv`` is DataForSEO's modelled traffic value, not measured sessions.
* ``paid.count`` reflects ads visible to DataForSEO's crawler at crawl
  time. A clinic running only Performance Max, or only on Meta, can show
  zero here while still spending. Absence of paid keywords is weak
  evidence, not proof.

Never quote these figures to a clinic as facts about their business. Use
them to decide who to contact and what to ask about.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

import requests

from discover import _auth_header, DiscoveryError

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.dataforseo.com/v3/dataforseo_labs/google/domain_rank_overview/live"

# Thresholds used to turn raw counts into the CRM's text columns. These are
# judgement calls tuned to UAE single-clinic sites, not universal truths.
SEO_STRONG = 150
SEO_SOME = 30
PAID_ACTIVE = 5


def _post(payload: list[dict[str, Any]], timeout: int = 60, retries: int = 3) -> dict[str, Any]:
    headers = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.post(ENDPOINT, headers=headers, data=json.dumps(payload), timeout=timeout)
            if response.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(2 ** (attempt + 1))
    raise DiscoveryError(f"Domain lookup failed after {retries} attempts: {last_error}")


def classify_seo(organic_count: int) -> str:
    """Turn an organic keyword count into a readable SEO-presence label."""
    if organic_count >= SEO_STRONG:
        return f"strong ({organic_count} keywords) - likely has an SEO agency"
    if organic_count >= SEO_SOME:
        return f"moderate ({organic_count} keywords)"
    if organic_count > 0:
        return f"minimal ({organic_count} keywords)"
    return "none detected"


def classify_paid(paid_count: int) -> str:
    """Turn a paid keyword count into a readable Google Ads label."""
    if paid_count >= PAID_ACTIVE:
        return f"yes ({paid_count} paid keywords)"
    if paid_count > 0:
        return f"minimal ({paid_count} paid keywords)"
    return "none detected"


def lookup(domain: str, location_name: str = "United Arab Emirates", language_code: str = "en") -> dict[str, Any]:
    """Fetch organic and paid search metrics for one domain.

    Args:
        domain: Bare registrable domain, e.g. ``"drjoydentalclinic.com"``.
        location_name: Country name, not a city.
        language_code: Two-letter language code.

    Returns:
        A dict with ``organic_count``, ``paid_count``, ``organic_etv``,
        readable ``seo_presence`` and ``google_ads`` labels, and an
        ``opportunity_gap`` flag that is True for the strong-SEO,
        no-paid profile described in the module docstring. Returns a dict
        with ``error`` set when the lookup fails.
    """
    payload = [{"target": domain, "location_name": location_name, "language_code": language_code}]
    try:
        body = _post(payload)
    except DiscoveryError as exc:
        return {"error": str(exc)}

    if body.get("status_code") != 20000:
        return {"error": f"{body.get('status_code')}: {body.get('status_message')}"}

    tasks = body.get("tasks") or []
    results = (tasks[0].get("result") if tasks else None) or []
    items = (results[0].get("items") if results else None) or []
    if not items:
        return {
            "organic_count": 0,
            "paid_count": 0,
            "organic_etv": 0.0,
            "seo_presence": "none detected",
            "google_ads": "none detected",
            "opportunity_gap": False,
        }

    metrics = items[0].get("metrics") or {}
    organic = metrics.get("organic") or {}
    paid = metrics.get("paid") or {}

    organic_count = int(organic.get("count") or 0)
    paid_count = int(paid.get("count") or 0)

    return {
        "organic_count": organic_count,
        "paid_count": paid_count,
        "organic_etv": float(organic.get("etv") or 0.0),
        "paid_etv": float(paid.get("etv") or 0.0),
        "seo_presence": classify_seo(organic_count),
        "google_ads": classify_paid(paid_count),
        # The profile worth opening a conversation with: they invest in
        # being found, but nobody is buying the intent.
        "opportunity_gap": organic_count >= SEO_SOME and paid_count < PAID_ACTIVE,
    }


def enrich(
    clinics: Iterable[dict[str, Any]],
    location_name: str = "United Arab Emirates",
    delay: float = 0.5,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Attach search-footprint metrics to clinics that have their own domain.

    Clinics without a real website are skipped rather than looked up, both
    to save credits and because a facebook.com URL tells you nothing about
    the clinic's own search presence.

    Domains are cached across clinics, so a multi-branch group costs one
    lookup rather than one per branch.
    """
    from dedupe import is_own_domain, normalize_domain

    clinic_list = list(clinics)
    cache: dict[str, dict[str, Any]] = {}

    for position, clinic in enumerate(clinic_list, start=1):
        domain = normalize_domain(clinic.get("domain") or clinic.get("url"))
        if not is_own_domain(domain):
            clinic["_domain_metrics"] = {"skipped": "no own domain"}
        else:
            if domain not in cache:
                cache[domain] = lookup(domain, location_name=location_name)
                time.sleep(delay)
            clinic["_domain_metrics"] = cache[domain]
        if on_progress:
            on_progress(position, len(clinic_list), clinic)

    return clinic_list
