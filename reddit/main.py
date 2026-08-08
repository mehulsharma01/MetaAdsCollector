"""Apify Actor: Reddit keyword/brand search scraper (intelligence edition).

Searches Reddit for a keyword or brand and collects matching posts with
engagement data, media, a lightweight sentiment tag, and (optionally) the
top comments on each post -- the signals that matter for competitive and
market intelligence.

Uses Reddit's public JSON API (append ``.json`` to any listing URL), so no
API key or login is required. Pages through results with the ``after``
cursor Reddit returns.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Any, Optional

from apify import Actor
from curl_cffi import requests as cffi_requests

REDDIT_BASE = "https://www.reddit.com"

# Tiny built-in sentiment lexicon. Deliberately dependency-free: no model
# download, no heavy import -- good enough to flag clearly positive vs
# negative posts/comments for triage. Not a substitute for a real NLP model.
_POSITIVE = {
    "good", "great", "love", "loved", "amazing", "awesome", "best", "excellent",
    "perfect", "recommend", "recommended", "happy", "worth", "worked", "works",
    "helpful", "fantastic", "wonderful", "nice", "impressed", "quality", "easy",
    "reliable", "favorite", "favourite", "win", "winning", "success", "beautiful",
    "cheap", "affordable", "fast", "smooth", "solid", "glad", "thanks", "thank",
}
_NEGATIVE = {
    "bad", "worst", "hate", "hated", "terrible", "awful", "horrible", "scam",
    "waste", "wasted", "broken", "broke", "useless", "disappointed", "disappointing",
    "poor", "expensive", "overpriced", "slow", "buggy", "problem", "problems",
    "issue", "issues", "fail", "failed", "failure", "avoid", "regret", "refund",
    "annoying", "difficult", "hard", "sucks", "sucked", "warning", "fake", "fraud",
}
_WORD_RE = re.compile(r"[a-z']+")


def _iso(utc_seconds: Optional[float]) -> Optional[str]:
    """Convert a Reddit UTC epoch to an ISO-8601 string."""
    if not utc_seconds:
        return None
    return datetime.fromtimestamp(utc_seconds, tz=timezone.utc).isoformat()


def _sentiment(text: str) -> tuple[str, float]:
    """Return (label, score) for text using the built-in lexicon.

    Score is (pos - neg) / (pos + neg) in [-1, 1]; label thresholds at
    +/-0.2. Empty/neutral text returns ("neutral", 0.0).
    """
    if not text:
        return "neutral", 0.0
    words = _WORD_RE.findall(text.lower())
    pos = sum(1 for w in words if w in _POSITIVE)
    neg = sum(1 for w in words if w in _NEGATIVE)
    total = pos + neg
    if total == 0:
        return "neutral", 0.0
    score = round((pos - neg) / total, 3)
    if score >= 0.2:
        return "positive", score
    if score <= -0.2:
        return "negative", score
    return "neutral", score


def _classify_media(d: dict[str, Any]) -> tuple[str, list[str]]:
    """Determine a post's media type and collect its media URLs."""
    # Gallery: multiple images under media_metadata.
    if d.get("is_gallery") and isinstance(d.get("media_metadata"), dict):
        urls: list[str] = []
        for item in d["media_metadata"].values():
            source = (item or {}).get("s") or {}
            u = source.get("u") or source.get("gif")
            if u:
                urls.append(html.unescape(u))
        if urls:
            return "gallery", urls

    # Native Reddit video.
    if d.get("is_video"):
        rv = ((d.get("media") or {}).get("reddit_video") or {})
        url = rv.get("fallback_url")
        return "video", [url] if url else []

    url = d.get("url") or ""
    hint = d.get("post_hint") or ""
    if hint == "image" or re.search(r"\.(jpg|jpeg|png|gif|webp)$", url, re.I):
        return "image", [url] if url else []
    if hint in ("hosted:video", "rich:video"):
        return "video", [url] if url else []
    if d.get("is_self"):
        return "text", []
    if url:
        return "link", [url]
    return "text", []


def _parse_post(child: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Reddit listing child into a clean, enriched post record."""
    d = child.get("data", {})
    permalink = d.get("permalink")
    thumb = d.get("thumbnail") or ""
    post_type, media_urls = _classify_media(d)
    title = d.get("title") or ""
    body = d.get("selftext") or ""
    label, score_val = _sentiment(f"{title} {body}")
    return {
        "id": d.get("id"),
        "title": title,
        "body": body or None,
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
        "post_type": post_type,
        "media_urls": media_urls,
        "is_self": d.get("is_self"),
        "over_18": d.get("over_18"),
        "link_flair_text": d.get("link_flair_text"),
        "thumbnail": thumb if thumb.startswith("http") else None,
        "total_awards_received": d.get("total_awards_received"),
        "sentiment": label,
        "sentiment_score": score_val,
        "top_comments": [],
    }


def _parse_comments(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Extract the top comments from a Reddit comments-endpoint response."""
    comments: list[dict[str, Any]] = []
    try:
        listing = payload[1]["data"]["children"]
    except (IndexError, KeyError, TypeError):
        return comments
    for child in listing:
        if child.get("kind") != "t1":
            continue  # skip "more" placeholders
        c = child.get("data", {})
        body = c.get("body")
        if not body:
            continue
        label, score_val = _sentiment(body)
        comments.append({
            "author": c.get("author"),
            "body": body,
            "score": c.get("score"),
            "created_utc": _iso(c.get("created_utc")),
            "sentiment": label,
            "sentiment_score": score_val,
        })
        if len(comments) >= limit:
            break
    return comments


def _fetch_json(session, url, params, proxies, log) -> Optional[Any]:
    """GET a Reddit JSON endpoint with retries. Returns parsed JSON or None."""
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, proxies=proxies, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning("Rate limited (429) on %s, waiting %ss", url, wait)
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Request failed (attempt %d/3) on %s: %s", attempt + 1, url, exc)
            import time
            time.sleep(2 * (attempt + 1))
    return None


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
        include_comments = bool(actor_input.get("includeComments"))
        comments_limit = int(actor_input.get("commentsLimit") or 5)

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
                "Searching Reddit: query=%r sort=%s time=%s comments=%s url=%s",
                query, sort, time_filter, include_comments, base_url,
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

                data = _fetch_json(session, base_url, params, proxies, Actor.log)
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
                    post = _parse_post(child)

                    if include_comments and post.get("id"):
                        c_url = f"{REDDIT_BASE}/comments/{post['id']}.json"
                        c_params = {"limit": comments_limit, "sort": "top", "raw_json": 1}
                        c_data = _fetch_json(session, c_url, c_params, proxies, Actor.log)
                        if c_data:
                            post["top_comments"] = _parse_comments(c_data, comments_limit)
                        if rate_limit_delay:
                            await asyncio.sleep(rate_limit_delay)

                    await Actor.push_data(post)
                    collected += 1

                after = listing.get("after")
                if not after:
                    break
                if rate_limit_delay:
                    await asyncio.sleep(rate_limit_delay)

        Actor.log.info("Done. Collected %d posts.", collected)


if __name__ == "__main__":
    asyncio.run(main())
