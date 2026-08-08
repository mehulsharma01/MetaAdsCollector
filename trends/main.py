"""Apify Actor: Trend Radar (Google Trends via pytrends).

For each seed keyword it returns the trend direction (rising/flat/falling),
the interest-over-time sparkline, and -- most usefully for a "trend radar"
-- the RISING related queries (what people are newly searching around that
topic). Free alternative to paid keyword APIs for the trends section of a
market-intelligence report.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pandas as pd
from apify import Actor
from pytrends.request import TrendReq


def _df_to_list(df: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Convert a pytrends related-queries DataFrame to a list of dicts."""
    out: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", True):
        return out
    for _, row in df.head(limit).iterrows():
        raw = row.get("value")
        try:
            value: Any = int(raw)
        except (TypeError, ValueError):
            value = str(raw)
        out.append({"query": str(row.get("query")), "value": value})
    return out


def _analyse_series(iot: Any, keyword: str) -> dict[str, Any]:
    """Derive direction/change/sparkline from an interest-over-time frame."""
    result: dict[str, Any] = {
        "direction": "unknown",
        "change_pct": None,
        "avg_interest": None,
        "latest_interest": None,
        "interest_over_time": [],
    }
    if iot is None or getattr(iot, "empty", True) or keyword not in iot.columns:
        return result

    vals = [int(v) for v in iot[keyword].tolist()]
    dates = [d.isoformat() for d in iot.index]
    result["interest_over_time"] = [
        {"date": d, "value": v} for d, v in zip(dates, vals)
    ]
    if not vals:
        return result

    result["avg_interest"] = round(sum(vals) / len(vals), 1)
    result["latest_interest"] = vals[-1]

    n = len(vals)
    q = max(1, n // 4)
    first = sum(vals[:q]) / q
    last = sum(vals[-q:]) / q
    if first > 0:
        result["change_pct"] = round((last - first) / first * 100, 1)
    if last > first * 1.15:
        result["direction"] = "rising"
    elif last < first * 0.85:
        result["direction"] = "falling"
    else:
        result["direction"] = "flat"
    return result


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        keywords = actor_input.get("keywords") or []
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        if not keywords:
            raise ValueError("You must provide 'keywords' (one or more seed terms).")

        geo = (actor_input.get("geo") or "").strip().upper()
        timeframe = actor_input.get("timeframe") or "today 3-m"
        include_rising = actor_input.get("includeRising", True)
        include_top = actor_input.get("includeTop", True)
        rate_limit_delay = float(actor_input.get("rateLimitDelay", 2) or 0)

        proxies: Optional[list[str]] = None
        proxy_configuration = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration")
        )
        if proxy_configuration:
            proxy_url = await proxy_configuration.new_url()
            proxies = [proxy_url]
            Actor.log.info("Using proxy for Google Trends requests.")

        pytrends = TrendReq(
            hl="en-US",
            tz=0,
            timeout=(10, 25),
            proxies=proxies or [],
            retries=2,
            backoff_factor=0.5,
        )

        for kw in keywords:
            Actor.log.info("Fetching trend for %r (geo=%s, %s)", kw, geo or "Worldwide", timeframe)
            try:
                pytrends.build_payload([kw], timeframe=timeframe, geo=geo)
                iot = pytrends.interest_over_time()
                related = pytrends.related_queries()
            except Exception as exc:  # noqa: BLE001
                Actor.log.warning("Trends failed for %r: %s", kw, exc)
                await Actor.push_data({"keyword": kw, "geo": geo or "Worldwide", "error": str(exc)})
                if rate_limit_delay:
                    await asyncio.sleep(rate_limit_delay)
                continue

            series = _analyse_series(iot, kw)
            kw_related = (related or {}).get(kw) or {}
            rising = _df_to_list(kw_related.get("rising")) if include_rising else []
            top = _df_to_list(kw_related.get("top")) if include_top else []

            record = {
                "keyword": kw,
                "geo": geo or "Worldwide",
                "timeframe": timeframe,
                "rising_queries": rising,
                "top_queries": top,
                **series,
            }
            await Actor.push_data(record)
            Actor.log.info(
                "Trend %r: %s (change %s%%), rising=%d top=%d",
                kw, series["direction"], series["change_pct"], len(rising), len(top),
            )
            if rate_limit_delay:
                await asyncio.sleep(rate_limit_delay)

        Actor.log.info("Done. Processed %d keywords.", len(keywords))


if __name__ == "__main__":
    asyncio.run(main())
