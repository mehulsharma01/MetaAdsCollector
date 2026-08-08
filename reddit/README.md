# Reddit Intel Collector

An Apify Actor that searches Reddit by keyword or brand and collects
matching posts with their engagement data. Built for competitive and
market intelligence — find where a topic is discussed, how much traction
it gets, and what people are saying.

Uses Reddit's public JSON API (no API key, no login).

## Input

| Field | Type | Description |
|-------|------|-------------|
| `query` | string | Keyword or brand to search for (required). |
| `subreddits` | array | Optional subreddits to restrict the search to. Empty = all of Reddit. |
| `sort` | string | `relevance`, `hot`, `top`, `new`, or `comments`. |
| `time` | string | `all`, `year`, `month`, `week`, `day`, `hour`. |
| `maxResults` | integer | Max posts to collect (0 = no limit). Applies per subreddit when restricting. |
| `rateLimitDelay` | integer | Seconds to wait between requests. |
| `proxyConfiguration` | object | Apify proxy settings. |

## Output

Each dataset item is a post:

`id`, `title`, `body`, `subreddit`, `subreddit_subscribers`, `author`,
`score`, `upvote_ratio`, `num_comments`, `created_utc`, `permalink`,
`url`, `domain`, `is_self`, `over_18`, `link_flair_text`, `thumbnail`,
`total_awards_received`.

## Deploy

Pushes to Apify automatically via GitHub Actions on every push to `main`
once an `APIFY_TOKEN` repository secret is set (Settings → Secrets and
variables → Actions).
