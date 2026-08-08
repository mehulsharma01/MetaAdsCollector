"""Apify Actor entry point for Meta Ads Collector.

This file is what the Apify platform runs (declared via the Dockerfile
`CMD` and `.actor/actor.json`). It reads the Actor input, drives
:class:`meta_ads_collector.MetaAdsCollector`, and pushes each collected ad
to the default dataset.
"""

from __future__ import annotations

import logging

from apify import Actor

from meta_ads_collector import MetaAdsCollector

# Surface the collector package's INFO logs on the Apify platform. Its
# loggers otherwise stay at the root's default WARNING level, hiding the
# search progress lines that make runs debuggable.
_pkg_logger = logging.getLogger("meta_ads_collector")
_pkg_logger.setLevel(logging.INFO)
if not _pkg_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _pkg_logger.addHandler(_handler)
    _pkg_logger.propagate = False

# Sort value used by the collector for server-default relevancy ordering.
SORT_IMPRESSIONS = "SORT_BY_TOTAL_IMPRESSIONS"


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        query = (actor_input.get("query") or "").strip()
        page_urls = actor_input.get("pageUrls") or []
        country = (actor_input.get("country") or "US").strip() or "US"
        ad_type = actor_input.get("adType") or "ALL"
        status = actor_input.get("status") or "ACTIVE"
        search_type = actor_input.get("searchType") or "KEYWORD_UNORDERED"
        sort_by_input = actor_input.get("sortBy") or "SORT_BY_TOTAL_IMPRESSIONS"
        page_size = int(actor_input.get("pageSize") or 10)
        rate_limit_delay = float(actor_input.get("rateLimitDelay", 2) or 0)

        # 0 (or missing) means "no limit".
        max_results_raw = actor_input.get("maxResults", 50)
        max_results = int(max_results_raw) if max_results_raw else None

        # "RELEVANCY" -> None (server default); anything else -> impressions sort.
        sort_by = None if sort_by_input == "RELEVANCY" else SORT_IMPRESSIONS

        if not query and not page_urls:
            raise ValueError(
                "You must provide either a 'query' or at least one entry in 'pageUrls'."
            )

        # Resolve an Apify proxy URL when proxy configuration was supplied.
        proxy_url = None
        proxy_configuration = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration")
        )
        if proxy_configuration:
            proxy_url = await proxy_configuration.new_url()
            # Log only the scheme (never the credentials) so runs confirm
            # the proxy URL is passed through intact by the fixed parser.
            scheme = proxy_url.split("://", 1)[0] if proxy_url else "?"
            Actor.log.info("Using proxy for outgoing requests (scheme=%s).", scheme)

        collected = 0

        with MetaAdsCollector(
            proxy=proxy_url,
            rate_limit_delay=rate_limit_delay,
        ) as collector:
            common_kwargs = {
                "country": country,
                "ad_type": ad_type,
                "status": status,
                "sort_by": sort_by,
                "max_results": max_results,
                "page_size": page_size,
            }

            if page_urls:
                for url in page_urls:
                    Actor.log.info("Collecting ads from page URL: %s", url)
                    for ad in collector.collect_by_page_url(url, **common_kwargs):
                        await Actor.push_data(ad.to_dict())
                        collected += 1
            else:
                Actor.log.info(
                    "Searching ads: query=%r country=%s ad_type=%s status=%s",
                    query,
                    country,
                    ad_type,
                    status,
                )
                for ad in collector.search(
                    query=query,
                    search_type=search_type,
                    **common_kwargs,
                ):
                    await Actor.push_data(ad.to_dict())
                    collected += 1

        Actor.log.info("Done. Collected %d ads.", collected)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
