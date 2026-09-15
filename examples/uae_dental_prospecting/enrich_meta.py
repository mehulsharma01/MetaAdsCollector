"""Enrichment stage: find out which clinics are already running Meta ads.

This is the highest-value enrichment in the pipeline and it costs nothing,
because it uses this repository's own Ad Library collector rather than a
paid scraper.

What it produces per clinic:

* ``running``          -- True, False, or None when the page could not be resolved
* ``active_ad_count``  -- how many active ads, capped at ``MAX_ADS_PER_PAGE``
* ``has_video``        -- whether any active ad uses video
* ``page_id`` / ``page_name`` -- so you can open the Ad Library yourself
* ``ad_library_url``   -- a link to paste straight into a first message
* ``sample_headlines`` -- what they are actually saying, for personalisation

Two cautions:

**Name matching is fuzzy.** The Ad Library typeahead matches on page name,
and UAE clinic names are long, bilingual and inconsistent. A miss means
``running=None``, not ``running=False``. Treat None as unknown, never as
evidence of absence -- :mod:`score` already does this.

**Running ads is not evidence the ads work.** All this tells you is that
budget exists and someone is spending it. Any claim about how well those
ads perform would need data neither you nor the Ad Library has.
"""

from __future__ import annotations

import logging
import re
import string
from typing import Any, Iterable

from meta_ads_collector import MetaAdsCollector

logger = logging.getLogger(__name__)

# Enough ads to judge rotation depth without paging a large advertiser.
MAX_ADS_PER_PAGE = 25

AD_LIBRARY_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=AE&view_all_page_id={page_id}"
)

# Words that make a clinic name unsearchable in the typeahead: legal
# suffixes, branch markers, and the Arabic half of a bilingual name.
_STRIP = re.compile(
    r"\b(l\.?l\.?c|llc|fz\s?llc|fzc|dmcc|br|branch|moh no\.?:?\s*\d+|"
    r"dha|moh|est|establishment)\b",
    re.IGNORECASE,
)
# Clinic names in the UAE are routinely bilingual or trilingual. The
# typeahead matches on the Latin half, so every other script is dropped
# rather than fed to it as mixed input. Printable ASCII plus Latin-1 and
# Latin Extended-A covers accented Latin names without letting Arabic,
# Cyrillic or CJK through.
_LATIN_CHARS = frozenset(string.printable) | frozenset(
    chr(code) for code in range(0x00C0, 0x0250)
)


def latin_only(text: str) -> str:
    """Replace every character outside the Latin scripts with a space."""
    return "".join(ch if ch in _LATIN_CHARS else " " for ch in text)
_PUNCT = re.compile(r"[|(),\-–—]+")


def search_name(title: str) -> str:
    """Turn a Google Business title into something the typeahead can match.

    ``"RAK DENTAL CARE AND IMPLANT CENTRE MOH NO.: 5108"`` becomes
    ``"RAK DENTAL CARE AND IMPLANT CENTRE"``; a bilingual title keeps only
    its Latin half; and anything after a comma (usually the branch) is
    dropped, because pages are rarely registered per branch.
    """
    name = latin_only(title)
    name = name.split(",")[0]
    name = _STRIP.sub(" ", name)
    name = _PUNCT.sub(" ", name)
    return " ".join(name.split()).strip()


def _best_page(pages: list[Any], clinic_name: str) -> Any | None:
    """Pick the likeliest page match, or None when nothing is close enough.

    Requires a real token overlap rather than trusting the typeahead's
    first result, which will happily return "Dental Care" for any query
    containing the word dental.
    """
    if not pages:
        return None

    wanted = {w for w in re.findall(r"[a-z0-9]+", clinic_name.lower()) if len(w) > 2}
    if not wanted:
        return None

    best = None
    best_overlap = 0.0
    for page in pages:
        got = {w for w in re.findall(r"[a-z0-9]+", (page.page_name or "").lower()) if len(w) > 2}
        if not got:
            continue
        overlap = len(wanted & got) / len(wanted)
        if overlap > best_overlap:
            best_overlap = overlap
            best = page

    # Below half the distinctive words in common, the match is a coin flip.
    return best if best_overlap >= 0.5 else None


def check_clinic(
    collector: MetaAdsCollector,
    clinic: dict[str, Any],
    country: str = "AE",
) -> dict[str, Any]:
    """Look up one clinic's Meta advertising status.

    Args:
        collector: A live :class:`MetaAdsCollector`.
        clinic: A deduplicated clinic dict.
        country: Ad Library country filter.

    Returns:
        A dict suitable for assignment to ``clinic["_meta_ads"]``. On any
        failure it returns ``{"running": None}`` with an ``error`` note,
        so one bad lookup never breaks the run.
    """
    title = clinic.get("title") or ""
    query = search_name(title)
    if not query:
        return {"running": None, "error": "no searchable name"}

    try:
        pages = collector.search_pages(query=query, country=country)
    except Exception as exc:
        logger.warning("Page search failed for %r: %s", query, exc)
        return {"running": None, "error": f"page search failed: {exc}"}

    page = _best_page(pages, query)
    if page is None:
        return {"running": None, "error": "no confident page match", "query": query}

    try:
        ads = list(
            collector.collect_by_page_id(page.page_id, country=country, max_results=MAX_ADS_PER_PAGE)
        )
    except Exception as exc:
        logger.warning("Ad collection failed for page %s: %s", page.page_id, exc)
        return {
            "running": None,
            "error": f"ad collection failed: {exc}",
            "page_id": page.page_id,
            "page_name": page.page_name,
        }

    headlines: list[str] = []
    has_video = False
    for ad in ads:
        for creative in getattr(ad, "creatives", []) or []:
            if creative.video_url or creative.video_hd_url or creative.video_sd_url:
                has_video = True
            text = creative.title or creative.body
            if text and len(headlines) < 3:
                headlines.append(" ".join(str(text).split())[:120])

    return {
        "running": bool(ads),
        "active_ad_count": len(ads),
        "has_video": has_video if ads else None,
        "page_id": page.page_id,
        "page_name": page.page_name,
        "ad_library_url": AD_LIBRARY_URL.format(page_id=page.page_id),
        "sample_headlines": headlines,
        "query": query,
    }


def enrich(
    clinics: Iterable[dict[str, Any]],
    country: str = "AE",
    delay: float = 2.0,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Attach Meta advertising status to each clinic, in place.

    Only run this over clinics you intend to contact. It is one or two
    network round trips per clinic, so enriching 1,400 clinics wastes
    hours on rows you will never message. Score first, slice the top few
    hundred, enrich those, then re-score.

    Args:
        clinics: Deduplicated clinics to enrich.
        country: Ad Library country filter.
        delay: Seconds between clinics.
        on_progress: Optional ``callable(done, total, clinic)``.

    Returns:
        The same clinic dicts, each with a ``_meta_ads`` key added.
    """
    clinic_list = list(clinics)
    with MetaAdsCollector(rate_limit_delay=delay) as collector:
        for position, clinic in enumerate(clinic_list, start=1):
            clinic["_meta_ads"] = check_clinic(collector, clinic, country=country)
            if on_progress:
                on_progress(position, len(clinic_list), clinic)
    return clinic_list
