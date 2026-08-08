"""Apify Actor: Reddit keyword/brand search scraper.

Searches Reddit for a keyword or brand and collects matching posts with
their engagement data (score, comments, subreddit, author, dates, links).

Reddit exposes a public JSON API -- appending ``.json`` to any listing URL
returns structured data -- so no reverse-engineering is needed. We page
through results with the ``after`` cursor Reddit returns.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from apify import Actor
from curl_cffi import requests as cffi_requests

REDDIT_BASE = "https://www.reddit.com"


def _iso(utc_seconds: Optional[float]) -> Optional[str]:
    """Convert a Reddit UTC epoch to an ISO-8601 string."""
    if not utc_seconds:
        return None
    return datetime.fromtimestamp(utc_seconds, tz=timezone.utc).isoformat()


def _parse_post(child: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Reddit listing child into a clean post record."""
    d = child.get("data", {})
    permalink = d.get("permalink")
    thumb = d.get("thumbnail") or ""
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "body": d.get("selftext") or None,
        "subreddit": d.get("subreddit"),
        "subreddit_subscribers": d.get("subreddit_subscribers"),
        "author": d.get("author"),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
        "created_utc": _iso(d.get("created_utc")),
        "permalink": f"{REDDIT_BASE}{permalink}" if permalink else None,
        "url": d.get("url"),
        "domain": d.get("domain"),
        "is_self": d.get("is_self"),
        "over_18": d.get("over_18"),
        "link_flair_text": d.get("link_flair_text"),
        "thumbnail": thumb if thumb.startswith("http") else None,
        "total_awards_received": d.get("total_awards_received"),
    }


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        query = (actor_input.get("query") or "").strip()
        if not query:
            raise ValueError("You must provide a 'query' (keyword or brand to search).")

        sort = actor_input.get("sort") or "relevance"
        time_filter = actor_input.get("time") or "all"
        subreddits = actor_input.get("subreddits") or []
        rate_limit_delay = float(actor_input.get("rateLimitDelay", 1) or 0)

        # 0 (or missing) means "no limit".
        max_results_raw = actor_input.get("maxResults", 100)
        max_results = int(max_results_raw) if max_results_raw else None

        # Resolve an Apify proxy URL when proxy configuration was supplied.
        proxies = None
        proxy_configuration = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration")
        )
        if proxy_configuration:
            proxy_url = await proxy_configuration.new_url()
            proxies = {"http": proxy_url, "https": proxy_url}
            Actor.log.info("Using proxy for outgoing requests.")

        session = cffi_requests.Session(impersonate="chrome")

        # Search all of Reddit, or restrict to each named subreddit.
        targets: list[tuple[str, bool]] = []
        if subreddits:
            for sr in subreddits:
                name = (sr or "").strip().removeprefix("r/").strip("/")
                if name:
                    targets.append((f"{REDDIT_BASE}/r/{name}/search.json", True))
        else:
            targets.append((f"{REDDIT_BASE}/search.json", False))

        collected = 0

        for base_url, restrict in targets:
            after: Optional[str] = None
            Actor.log.info(
                "Searching Reddit: query=%r sort=%s time=%s url=%s",
                query, sort, time_filter, base_url,
            )

            while True:
                if max_results and collected >= max_results:
                    break

                params: dict[str, Any] = {
                    "q": query,
                    "sort": sort,
                    "t": time_filter,
                    "limit": 100,
                    "raw_json": 1,
                }
                if restrict:
                    params["restrict_sr"] = 1
                if after:
                    params["after"] = after

                data = None
                for attempt in range(3):
                    try:
                        resp = session.get(
                            base_url, params=params, proxies=proxies, timeout=30
                        )
                        if resp.status_code == 429:
                            wait = 5 * (attempt + 1)
                            Actor.log.warning("Rate limited (429), waiting %ss", wait)
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                        break
                    except Exception as exc:  # noqa: BLE001
                        Actor.log.warning(
                            "Request failed (attempt %d/3): %s", attempt + 1, exc
                        )
                        await asyncio.sleep(2 * (attempt + 1))

                if not data:
                    Actor.log.error("Giving up on %s after retries.", base_url)
                    break

                listing = data.get("data", {})
                children = listing.get("children", [])
                if not children:
                    break

                for child in children:
                    if max_results and collected >= max_results:
                        break
                    await Actor.push_data(_parse_post(child))
                    collected += 1

                after = listing.get("after")
                if not after:
                    break
                if rate_limit_delay:
                    await asyncio.sleep(rate_limit_delay)

        Actor.log.info("Done. Collected %d posts.", collected)


if __name__ == "__main__":
    asyncio.run(main())
