# Trend Radar (Google Trends)

Apify Actor that turns seed keywords into a "trend radar" using Google
Trends (via pytrends): trend direction, interest-over-time sparkline, and
the fastest-rising related queries. Free alternative to paid keyword APIs
for the trends section of a market-intelligence report.

## Input
- `keywords` (array, required) — seed terms.
- `geo` — 2-letter country code (e.g. `AE`, `US`). Empty = worldwide.
- `timeframe` — `now 7-d`, `today 1-m`, `today 3-m`, `today 12-m`, `today 5-y`.
- `includeRising` / `includeTop` — related-query lists.
- `proxyConfiguration` — residential recommended (Trends rate-limits).

## Output (per keyword)
`keyword`, `geo`, `timeframe`, `direction` (rising/flat/falling),
`change_pct`, `avg_interest`, `latest_interest`, `rising_queries`,
`top_queries`, `interest_over_time`.
