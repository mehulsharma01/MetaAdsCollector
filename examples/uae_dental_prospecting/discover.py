"""Discovery stage: pull raw dental business listings from DataForSEO.

This module is the reason the list goes from ~50 clinics to well over a
thousand. It does not scrape Google Maps. It queries DataForSEO's Business
Listings database directly, which is a stored index of Google Business
profiles rather than a live SERP, so:

* results are not capped at what fits on a results page,
* the same request can be paged through with ``offset``,
* one request returns the full profile -- categories, services, prices,
  ratings, hours, website, phone -- with no per-clinic follow-up call.

That last point is what keeps the cost down. Scraping the equivalent detail
from Maps would be one actor run per clinic.

Credentials come from the environment::

    export DATAFORSEO_LOGIN="you@example.com"
    export DATAFORSEO_PASSWORD="..."
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Iterator

import requests

from grid import Query

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.dataforseo.com/v3/business_data/business_listings/search/live"

# DataForSEO caps a single response at 1000 listings. Anything denser than
# that in one circle needs paging.
MAX_LIMIT = 1000

# Stop paging a single circle past this many listings. A circle returning
# more than this is drawn too wide -- split it rather than paging forever.
PAGE_CEILING = 3000


class DiscoveryError(RuntimeError):
    """Raised when the API rejects a request or returns an error status."""


def _auth_header() -> str:
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise DiscoveryError(
            "Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in the environment."
        )
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return f"Basic {token}"


def _post(payload: list[dict[str, Any]], timeout: int, retries: int = 3) -> dict[str, Any]:
    """POST one task array, retrying on transport errors and 429s."""
    headers = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            response = requests.post(
                ENDPOINT, headers=headers, data=json.dumps(payload), timeout=timeout
            )
            if response.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("Rate limited, waiting %ss", wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:  # network, timeout, 5xx
            last_error = exc
            wait = 2 ** (attempt + 1)
            logger.warning("Request failed (%s), retrying in %ss", exc, wait)
            time.sleep(wait)

    raise DiscoveryError(f"Request failed after {retries} attempts: {last_error}")


def _extract(body: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Pull ``(items, total_count)`` out of a DataForSEO response envelope."""
    if body.get("status_code") != 20000:
        raise DiscoveryError(f"API error {body.get('status_code')}: {body.get('status_message')}")

    tasks = body.get("tasks") or []
    if not tasks:
        return [], 0

    task = tasks[0]
    if task.get("status_code") != 20000:
        raise DiscoveryError(f"Task error {task.get('status_code')}: {task.get('status_message')}")

    results = task.get("result") or []
    if not results:
        return [], 0

    result = results[0]
    return (result.get("items") or []), (result.get("total_count") or 0)


def count_only(query: Query, timeout: int = 60) -> int:
    """Return how many listings a query would yield, without pulling them.

    Costs one request. Use this to sanity-check the grid before committing
    to a full run, and to find circles drawn too wide.
    """
    payload = [
        {
            "categories": query.categories,
            "location_coordinate": query.location_coordinate,
            "limit": 1,
        }
    ]
    _, total = _extract(_post(payload, timeout))
    return total


def run_query(
    query: Query,
    page_size: int = MAX_LIMIT,
    timeout: int = 120,
    delay: float = 1.0,
) -> Iterator[dict[str, Any]]:
    """Execute one grid query, paging until the circle is exhausted.

    Args:
        query: The circle and category set to search.
        page_size: Listings per request, capped at 1000 by the API.
        timeout: Per-request timeout in seconds.
        delay: Seconds to sleep between pages, to stay friendly.

    Yields:
        Raw business listing dicts, each tagged with a ``_sources`` entry
        naming the query that found it so coverage gaps are traceable.
    """
    page_size = min(page_size, MAX_LIMIT)
    offset = 0
    total = None

    while True:
        payload = [
            {
                "categories": query.categories,
                "location_coordinate": query.location_coordinate,
                "limit": page_size,
                "offset": offset,
            }
        ]
        items, reported_total = _extract(_post(payload, timeout))
        if total is None:
            total = reported_total
            logger.info("[%03d] %s -> %d listings", query.index, query.label, total)
            if total > PAGE_CEILING:
                logger.warning(
                    "[%03d] %s returns %d listings; the circle is too wide. "
                    "Split it into smaller radii for better coverage.",
                    query.index,
                    query.label,
                    total,
                )

        if not items:
            return

        for item in items:
            item["_sources"] = [query.label]
            yield item

        offset += len(items)
        if offset >= min(total, PAGE_CEILING) or len(items) < page_size:
            return
        time.sleep(delay)


def discover(
    queries: list[Query],
    page_size: int = MAX_LIMIT,
    delay: float = 1.0,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Run the whole grid and return every raw listing found.

    Failures on individual queries are logged and skipped rather than
    aborting the run -- losing one circle is better than losing the
    other 111.

    Args:
        queries: Grid queries, typically from :func:`grid.build_queries`.
        page_size: Listings per request.
        delay: Seconds between requests.
        on_progress: Optional ``callable(done, total, query)`` for a
            progress display.

    Returns:
        Raw listings with duplicates still present. Pass to
        :func:`dedupe.deduplicate` next.
    """
    collected: list[dict[str, Any]] = []
    for position, query in enumerate(queries, start=1):
        try:
            collected.extend(run_query(query, page_size=page_size, delay=delay))
        except DiscoveryError as exc:
            logger.error("[%03d] %s failed: %s", query.index, query.label, exc)
        if on_progress:
            on_progress(position, len(queries), query)
        time.sleep(delay)
    return collected
