"""Apify Actor entry point for Meta Ads Collector.

This file is what the Apify platform runs (declared via the Dockerfile
`CMD` and `.actor/actor.json`). It reads the Actor input, drives
:class:`meta_ads_collector.MetaAdsCollector`, and pushes each collected ad
to the default dataset.
"""

from __future__ import annotations

import base64
import logging
from io import BytesIO

from apify import Actor
from curl_cffi import requests as cffi_requests

try:
    from PIL import Image
except ImportError:  # Pillow is installed in the Actor image; guard for local use.
    Image = None

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


def _pick_thumb_url(ad_dict: dict) -> "str | None":
    """Choose the best still image for an ad (image, else video poster)."""
    for creative in ad_dict.get("creatives") or []:
        url = creative.get("image_url") or creative.get("thumbnail_url")
        if url:
            return url
    return None


def _augment_thumbnail(session, ad_dict: dict, max_bytes: int) -> None:
    """Download the ad's thumbnail and attach it as a base64 data URI.

    Only the Actor (running with a residential proxy) can reach Meta's
    image CDN, so we fetch here and embed the bytes -- downstream consumers
    (reports, dashboards) then render the real creative without hitting an
    expiring CDN URL. Failures are non-fatal: the ad is still pushed.
    """
    url = _pick_thumb_url(ad_dict)
    if not url:
        return
    try:
        resp = session.get(url, timeout=25)
        if resp.status_code != 200:
            return
        content = resp.content
        if not content:
            return
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"

        # Downscale so embedded thumbnails stay small (keeps dataset lean and
        # keeps report/data-URI payloads manageable). Falls back to raw bytes.
        if Image is not None:
            try:
                im = Image.open(BytesIO(content)).convert("RGB")
                im.thumbnail((480, 480))
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=72, optimize=True)
                content = buf.getvalue()
                ctype = "image/jpeg"
            except Exception:  # noqa: BLE001
                pass

        if len(content) > max_bytes:
            return
        b64 = base64.b64encode(content).decode("ascii")
        ad_dict["thumbnail_b64"] = f"data:{ctype};base64,{b64}"
    except Exception as exc:  # noqa: BLE001
        Actor.log.warning("Thumbnail download failed: %s", exc)


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
        download_thumbnails = bool(actor_input.get("downloadThumbnails"))
        thumbnail_max_bytes = int(actor_input.get("thumbnailMaxBytes") or 600000)

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

        # Session used only to fetch creative thumbnails (same proxy path).
        thumb_session = None
        if download_thumbnails:
            thumb_session = cffi_requests.Session(impersonate="chrome")
            if proxy_url:
                thumb_session.proxies = {"http": proxy_url, "https": proxy_url}
            Actor.log.info("Thumbnail download enabled (max %d bytes).", thumbnail_max_bytes)

        def _push_dict(ad) -> dict:
            d = ad.to_dict()
            if thumb_session is not None:
                _augment_thumbnail(thumb_session, d, thumbnail_max_bytes)
            return d

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
                        await Actor.push_data(_push_dict(ad))
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
                    await Actor.push_data(_push_dict(ad))
                    collected += 1

        Actor.log.info("Done. Collected %d ads.", collected)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
