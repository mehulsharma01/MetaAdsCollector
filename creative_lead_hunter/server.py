"""
Creative Lead Hunter — find beauty DTC brands running weak (static-only) ad creative.

Single-file FastAPI app. It proxies the Winning Hunter public ad-library API
(the browser cannot: no CORS headers), collapses per-ad rows into per-brand
records, scores them as prospects for AI product creative, and keeps outreach
state in SQLite so it survives restarts.

Run:
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8000

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

API_URL = "https://app.winninghunter.com/api/public/adlibrary"

# The upstream pagination parameter. Its real name is not documented and
# `scroll` comes back null, so this is the one place to change it.
#
# Leave AUTO_DETECT_PAGINATION on and the first multi-page run tries each
# candidate in order and keeps whichever one actually changes the result set.
# Whatever it settles on is logged, so you can pin it here afterwards and
# stop paying for the probe.
PAGE_PARAM = "page"
PAGE_PARAM_CANDIDATES = ["page", "offset", "scroll"]
AUTO_DETECT_PAGINATION = True

REQUEST_INTERVAL_SECONDS = 1.2  # ~1 request / 1.2s, outbound
REQUEST_TIMEOUT_SECONDS = 25.0
PAGE_SIZE_HINT = 20  # what the API returns per call; used to compute offsets

DEFAULT_KEYWORDS = [
    "skincare", "serum", "moisturizer", "lip oil", "face cream", "cleanser",
    "sunscreen", "eye cream", "hair oil", "hair serum", "shampoo",
    "body butter", "lip balm", "perfume", "nail",
]

STATUSES = ["New", "Shortlist", "Samples made", "DM sent", "Replied", "Client", "Rejected"]

DEFAULT_DM_TEMPLATE = (
    "Hey {brand} — saw your ads in the Meta library. You're running almost all "
    "stills right now.\n\n"
    "I make AI product video for small beauty brands: same product, same angle, "
    "turned into scroll-stopping motion. No shoot, no studio, 48h turnaround.\n\n"
    "Want me to make one from your existing product images so you can see it "
    "before deciding anything?"
)

HERE = Path(__file__).parent
DB_PATH = HERE / "leads.db"
CACHE_PATH = HERE / "leads.json"
INDEX_PATH = HERE / "index.html"

# Mock mode lets the whole pipeline run without touching the network, which is
# also how the test fixture exercises dedupe/scoring/filters.
MOCK_MODE = os.environ.get("HUNTER_MOCK") == "1"
MOCK_PATH = HERE / "mock_api.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger("hunter")

# Static formats are the whole point: a brand running only stills is a brand
# that needs video. "dco" is Meta's dynamic creative — still image-based unless
# a video asset is attached, and we check the assets separately anyway.
STATIC_FORMATS = {"image", "carousel", "dco"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_int(value: Any) -> int | None:
    """Coerce to int, tolerating '12,345', '1.2k', None, floats and junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(float(value)) else None
    text = str(value).strip().lower().replace(",", "").replace(" ", "")
    if not text:
        return None
    mult = 1
    if text.endswith("k"):
        mult, text = 1_000, text[:-1]
    elif text.endswith("m"):
        mult, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def is_http_url(value: Any) -> bool:
    """
    The API mixes real URLs with bare content hashes in the same fields.
    Anything that is not http(s) cannot be rendered, so it never leaves here.
    """
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def human_number(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands (
                page_id          TEXT PRIMARY KEY,
                page_name        TEXT,
                url_store        TEXT,
                product_url      TEXT,
                page_profile_uri TEXT,
                url_all          TEXT,
                ig_followers     INTEGER,
                page_like_count  INTEGER,
                monthly_visits   INTEGER,
                ad_count         INTEGER,
                static_count     INTEGER,
                static_pct       REAL,
                days_running     INTEGER,
                last_seen        TEXT,
                country_code     TEXT,
                adscore          TEXT,
                adscores_json    TEXT,
                total_adspend    TEXT,
                technologies_json TEXT,
                is_shopify       INTEGER,
                creatives_json   TEXT,
                has_video        INTEGER,
                ad_copy          TEXT,
                caption          TEXT,
                keywords_json    TEXT,
                lead_score       REAL,
                reason           TEXT,
                first_seen_at    TEXT,
                updated_at       TEXT
            );

            -- Outreach lives in its own table so re-running a hunt can never
            -- clobber a status or a note.
            CREATE TABLE IF NOT EXISTS outreach (
                page_id    TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'New',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_brands_score ON brands(lead_score DESC);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('dm_template', ?)",
            (DEFAULT_DM_TEMPLATE,),
        )


def get_setting(key: str, default: str = "") -> str:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------
# Upstream fetching
# --------------------------------------------------------------------------

class RateLimiter:
    """One outbound request per `interval` seconds, across all keywords."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


limiter = RateLimiter(REQUEST_INTERVAL_SECONDS)


def page_param_value(param: str, page_index: int, scroll_token: Any) -> Any:
    """
    Translate a zero-based page index into whatever the candidate param wants.

    `page` is treated as 1-based, `offset` as a row count, `scroll` as an
    opaque token echoed from the previous response.
    """
    if param == "offset":
        return page_index * PAGE_SIZE_HINT
    if param == "scroll":
        return scroll_token
    return page_index + 1


def _load_mock() -> dict:
    # The fixture is generated rather than committed, so build it on first use.
    if not MOCK_PATH.exists():
        try:
            import make_mock
            make_mock.write()
            log.info("generated %s", MOCK_PATH.name)
        except Exception as exc:
            log.error("could not generate the mock fixture: %s", exc)
            return {"data": [], "total": 0}
    try:
        return json.loads(MOCK_PATH.read_text())
    except Exception as exc:  # pragma: no cover - only hit with a broken fixture
        log.error("mock fixture unreadable: %s", exc)
        return {"data": [], "total": 0}


async def fetch_page(
    client: httpx.AsyncClient,
    keyword: str,
    page_index: int,
    param: str,
    scroll_token: Any,
) -> dict | None:
    """
    One upstream call. Returns the decoded body, or None on any failure —
    a single bad request must never take down the run.
    """
    params: dict[str, Any] = {"keyword": keyword}
    if page_index > 0:
        value = page_param_value(param, page_index, scroll_token)
        if value is None:
            # `scroll` with nothing to send: there is no next page to ask for.
            return None
        params[param] = value

    if MOCK_MODE:
        await asyncio.sleep(0.01)
        payload = _load_mock()
        rows = payload.get("data", [])
        start = page_index * PAGE_SIZE_HINT
        return {
            "data": rows[start:start + PAGE_SIZE_HINT],
            "total": payload.get("total", len(rows)),
            "scroll": None,
        }

    await limiter.wait()
    try:
        resp = await client.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("request failed  keyword=%r page=%d: %s", keyword, page_index, exc)
        return None

    if resp.status_code != 200:
        log.warning("HTTP %s  keyword=%r page=%d", resp.status_code, keyword, page_index)
        return None

    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        log.warning(
            "non-JSON response  keyword=%r page=%d  first 120 chars: %r",
            keyword, page_index, resp.text[:120],
        )
        return None

    if not isinstance(body, dict):
        log.warning("unexpected JSON shape (%s) keyword=%r", type(body).__name__, keyword)
        return None
    return body


def ids_in(rows: Iterable[dict]) -> set[str]:
    return {str(r.get("page_id")) for r in rows if r.get("page_id")}


# --------------------------------------------------------------------------
# Creative extraction
# --------------------------------------------------------------------------

def creatives_from_ad(ad: dict) -> list[dict]:
    """
    Pull every renderable asset out of one ad row.

    Non-http values are dropped: those fields sometimes hold bare content
    hashes, and a hash in an <img src> is a broken-image icon.
    """
    out: list[dict] = []
    fmt = str(ad.get("display_format") or "").lower()

    video = ad.get("video")
    poster = ad.get("poster")
    image = ad.get("image")

    if is_http_url(video):
        out.append({
            "type": "video",
            "src": video,
            "poster": poster if is_http_url(poster) else (image if is_http_url(image) else None),
        })

    if is_http_url(image):
        out.append({"type": "image", "src": image, "poster": None})
    elif fmt == "video" and is_http_url(poster) and not out:
        out.append({"type": "image", "src": poster, "poster": None})

    for card in ad.get("cards") or []:
        if not isinstance(card, dict):
            continue
        c_img, c_poster = card.get("image"), card.get("poster")
        if is_http_url(c_img):
            out.append({"type": "image", "src": c_img, "poster": None})
        elif is_http_url(c_poster):
            out.append({"type": "image", "src": c_poster, "poster": None})

    # Same asset can arrive on several ads; keep first occurrence only.
    seen, unique = set(), []
    for c in out:
        if c["src"] in seen:
            continue
        seen.add(c["src"])
        unique.append(c)
    return unique


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _band(value: float | None, rise_from: float, full_from: float,
          full_to: float, fall_to: float) -> float:
    """
    Trapezoid membership in 0..1. Flat 1.0 across the sweet spot, ramping in
    and out either side, so a brand just outside the band is not thrown away.
    """
    if value is None:
        return 0.0
    v = float(value)
    if v <= rise_from or v >= fall_to:
        return 0.0
    if full_from <= v <= full_to:
        return 1.0
    if v < full_from:
        return (v - rise_from) / max(full_from - rise_from, 1e-9)
    return (fall_to - v) / max(fall_to - full_to, 1e-9)


def score_brand(b: dict) -> tuple[float, str]:
    """
    Score a brand as a prospect for AI product creative, 0..100.

    The thesis, in order of weight:
      1. Running only stills          -> they need video, which is the offer
      2. 1k-30k IG followers          -> can pay, too small to have an agency
      3. 200-20k monthly visits       -> a real store, still owner-operated
      4. Testing/Scaling, not Winning -> spending, not converting: creative is
                                         the bottleneck
    Big traffic or a big following means an agency already has them, so both
    are penalised hard rather than merely scored low.
    """
    static_pct = float(b.get("static_pct") or 0.0)
    ig = b.get("ig_followers")
    visits = b.get("monthly_visits")
    scores = [str(s or "").lower() for s in (b.get("adscores") or [])]

    # 1. static share — 40 pts
    static_points = 40.0 * (static_pct / 100.0)

    # 2. follower band — 25 pts
    ig_points = 25.0 * _band(ig, 300, 1_000, 30_000, 100_000)

    # 3. traffic band — 20 pts
    visit_points = 20.0 * _band(visits, 50, 200, 20_000, 60_000)

    # 4. still figuring it out — 15 pts
    hungry = sum(1 for s in scores if s in ("testing", "scaling"))
    ad_points = 15.0 * (hungry / len(scores)) if scores else 0.0

    # The fit signals only count to the extent there is a creative gap to sell
    # into. A perfectly sized brand already running 90% video does not need
    # what I make, and without this it scored higher than brands that do.
    gap = 0.35 + 0.65 * (static_pct / 100.0)
    score = static_points + gap * (ig_points + visit_points + ad_points)

    # Penalties. Graduated rather than a cliff, but steep.
    penalties = []
    if visits and visits > 50_000:
        pen = min(30.0, 10.0 + 20.0 * min((visits - 50_000) / 150_000, 1.0))
        score -= pen
        penalties.append("big traffic")
    if ig and ig > 100_000:
        pen = min(30.0, 10.0 + 20.0 * min((ig - 100_000) / 400_000, 1.0))
        score -= pen
        penalties.append("big following")

    score = max(0.0, min(100.0, score))

    # --- the one-line reason -------------------------------------------------
    bits = [f"{static_pct:.0f}% static ads"]
    if ig is not None:
        bits.append(f"{human_number(ig)} IG")
    if visits is not None:
        bits.append(f"{human_number(visits)} visits/mo")
    head = ", ".join(bits)

    if penalties:
        tail = " and ".join(penalties) + " — probably already has an agency"
    elif hungry and scores and hungry == len(scores):
        tail = "testing and losing" if static_pct >= 80 else "testing, mixed creative"
    elif hungry:
        tail = "still testing creative"
    elif scores and all(s == "winning" for s in scores):
        tail = "already winning — lower priority"
    else:
        tail = "no ad-score signal"

    return round(score, 1), f"{head} — {tail}"


# --------------------------------------------------------------------------
# Dedupe + aggregate
# --------------------------------------------------------------------------

def aggregate(rows: list[dict], keyword_of: dict[str, set[str]]) -> list[dict]:
    """
    Collapse per-ad rows into one record per brand, keyed on page_id.

    The API returns one row per AD, so a brand with 40 live ads arrives 40
    times. Everything downstream assumes one row per brand.

    product_title / product_image / product_price / product_handle are
    deliberately never read: every record returns the same stale value.
    """
    brands: dict[str, dict] = {}

    for ad in rows:
        page_id = ad.get("page_id")
        if not page_id:
            continue
        page_id = str(page_id)

        b = brands.get(page_id)
        if b is None:
            b = brands[page_id] = {
                "page_id": page_id,
                "page_name": ad.get("pageName") or "(unnamed)",
                "url_store": ad.get("urlStore") or "",
                "product_url": ad.get("product_url") or "",
                "page_profile_uri": ad.get("page_profile_uri") or "",
                "url_all": ad.get("urlAll") or "",
                "ig_followers": as_int(ad.get("igFollowers")),
                "page_like_count": as_int(ad.get("page_like_count")),
                "monthly_visits": None,
                "ad_count": 0,
                "static_count": 0,
                "days_running": None,
                "last_seen": ad.get("lastSeen") or "",
                "country_code": ad.get("country_code") or "",
                "adscores": [],
                "total_adspend": ad.get("total_adspend") or "",
                "technologies": [],
                "creatives": [],
                "ad_copy": "",
                "caption": "",
                "keywords": sorted(keyword_of.get(page_id, set())),
            }

        traffic = ad.get("store_traffic")
        if isinstance(traffic, dict):
            visits = as_int(traffic.get("monthly_visits"))
            if visits is not None:
                b["monthly_visits"] = max(b["monthly_visits"] or 0, visits)

        fmt = str(ad.get("display_format") or "").lower()
        b["ad_count"] += 1
        if fmt in STATIC_FORMATS:
            b["static_count"] += 1

        if ad.get("adscore"):
            b["adscores"].append(str(ad["adscore"]))

        days = as_int(ad.get("daysrunning"))
        if days is not None:
            b["days_running"] = max(b["days_running"] or 0, days)

        for tech in ad.get("technologies") or []:
            if tech and str(tech) not in b["technologies"]:
                b["technologies"].append(str(tech))

        # Keep the longest copy seen — the fullest version of the pitch.
        for field, key in (("copy", "ad_copy"), ("caption", "caption")):
            text = ad.get(field)
            if isinstance(text, str) and len(text) > len(b[key]):
                b[key] = text

        for c in creatives_from_ad(ad):
            if len(b["creatives"]) >= 12:
                break
            if all(c["src"] != existing["src"] for existing in b["creatives"]):
                b["creatives"].append(c)

    out = []
    for b in brands.values():
        b["static_pct"] = round(100.0 * b["static_count"] / b["ad_count"], 1) if b["ad_count"] else 0.0
        b["has_video"] = any(c["type"] == "video" for c in b["creatives"])
        b["is_shopify"] = "SH" in b["technologies"]
        counts: dict[str, int] = {}
        for s in b["adscores"]:
            counts[s] = counts.get(s, 0) + 1
        b["adscore"] = max(counts, key=counts.get) if counts else ""
        b["lead_score"], b["reason"] = score_brand(b)
        out.append(b)

    out.sort(key=lambda x: x["lead_score"], reverse=True)
    return out


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def upsert_brands(brands: list[dict]) -> None:
    stamp = now_iso()
    with closing(db()) as conn, conn:
        for b in brands:
            conn.execute(
                """
                INSERT INTO brands (
                    page_id, page_name, url_store, product_url, page_profile_uri, url_all,
                    ig_followers, page_like_count, monthly_visits, ad_count, static_count,
                    static_pct, days_running, last_seen, country_code, adscore, adscores_json,
                    total_adspend, technologies_json, is_shopify, creatives_json, has_video,
                    ad_copy, caption, keywords_json, lead_score, reason, first_seen_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(page_id) DO UPDATE SET
                    page_name = excluded.page_name,
                    url_store = excluded.url_store,
                    product_url = excluded.product_url,
                    page_profile_uri = excluded.page_profile_uri,
                    url_all = excluded.url_all,
                    ig_followers = excluded.ig_followers,
                    page_like_count = excluded.page_like_count,
                    monthly_visits = excluded.monthly_visits,
                    ad_count = excluded.ad_count,
                    static_count = excluded.static_count,
                    static_pct = excluded.static_pct,
                    days_running = excluded.days_running,
                    last_seen = excluded.last_seen,
                    country_code = excluded.country_code,
                    adscore = excluded.adscore,
                    adscores_json = excluded.adscores_json,
                    total_adspend = excluded.total_adspend,
                    technologies_json = excluded.technologies_json,
                    is_shopify = excluded.is_shopify,
                    creatives_json = excluded.creatives_json,
                    has_video = excluded.has_video,
                    ad_copy = excluded.ad_copy,
                    caption = excluded.caption,
                    keywords_json = excluded.keywords_json,
                    lead_score = excluded.lead_score,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    b["page_id"], b["page_name"], b["url_store"], b["product_url"],
                    b["page_profile_uri"], b["url_all"], b["ig_followers"], b["page_like_count"],
                    b["monthly_visits"], b["ad_count"], b["static_count"], b["static_pct"],
                    b["days_running"], b["last_seen"], b["country_code"], b["adscore"],
                    json.dumps(b["adscores"]), b["total_adspend"], json.dumps(b["technologies"]),
                    int(b["is_shopify"]), json.dumps(b["creatives"]), int(b["has_video"]),
                    b["ad_copy"], b["caption"], json.dumps(b["keywords"]),
                    b["lead_score"], b["reason"], stamp, stamp,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO outreach(page_id, status, notes, updated_at) "
                "VALUES(?, 'New', '', ?)",
                (b["page_id"], stamp),
            )


def row_to_brand(row: sqlite3.Row) -> dict:
    return {
        "page_id": row["page_id"],
        "page_name": row["page_name"],
        "url_store": row["url_store"],
        "product_url": row["product_url"],
        "page_profile_uri": row["page_profile_uri"],
        "url_all": row["url_all"],
        "ig_followers": row["ig_followers"],
        "page_like_count": row["page_like_count"],
        "monthly_visits": row["monthly_visits"],
        "ad_count": row["ad_count"],
        "static_count": row["static_count"],
        "static_pct": row["static_pct"],
        "days_running": row["days_running"],
        "last_seen": row["last_seen"],
        "country_code": row["country_code"],
        "adscore": row["adscore"],
        "adscores": json.loads(row["adscores_json"] or "[]"),
        "total_adspend": row["total_adspend"],
        "technologies": json.loads(row["technologies_json"] or "[]"),
        "is_shopify": bool(row["is_shopify"]),
        "creatives": json.loads(row["creatives_json"] or "[]"),
        "has_video": bool(row["has_video"]),
        "ad_copy": row["ad_copy"],
        "caption": row["caption"],
        "keywords": json.loads(row["keywords_json"] or "[]"),
        "lead_score": row["lead_score"],
        "reason": row["reason"],
        "status": row["status"] or "New",
        "notes": row["notes"] or "",
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------
# The hunt
# --------------------------------------------------------------------------

HUNT: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": "",
    "raw_ads": 0,
    "brands": 0,
    "dropped_no_creative": 0,
    "page_param": PAGE_PARAM,
    "pagination_ok": None,
    "started_at": None,
    "finished_at": None,
    "errors": [],
}


async def run_hunt(keywords: list[str], pages: int) -> None:
    HUNT.update(
        running=True, done=0, total=len(keywords) * max(pages, 1), current="",
        raw_ads=0, brands=0, dropped_no_creative=0, errors=[],
        started_at=now_iso(), finished_at=None,
        page_param=PAGE_PARAM, pagination_ok=None,
    )

    all_rows: list[dict] = []
    keyword_of: dict[str, set[str]] = {}
    active_param = PAGE_PARAM
    param_settled = not (AUTO_DETECT_PAGINATION and pages > 1)
    candidates = list(PAGE_PARAM_CANDIDATES)
    if PAGE_PARAM in candidates:  # try the configured one first
        candidates.remove(PAGE_PARAM)
    candidates.insert(0, PAGE_PARAM)

    async with httpx.AsyncClient(
        headers={"Accept": "application/json", "User-Agent": "creative-lead-hunter/1.0"},
        follow_redirects=True,
    ) as client:
        for keyword in keywords:
            scroll_token = None
            previous_ids: set[str] | None = None

            for page_index in range(max(pages, 1)):
                HUNT["current"] = f"{keyword} · page {page_index + 1}"
                body = await fetch_page(client, keyword, page_index, active_param, scroll_token)

                if body is None:
                    HUNT["errors"].append(f"{keyword} p{page_index + 1}: request failed")
                    HUNT["done"] += 1
                    continue

                rows = body.get("data") or []
                if not isinstance(rows, list):
                    HUNT["errors"].append(f"{keyword} p{page_index + 1}: 'data' was not a list")
                    HUNT["done"] += 1
                    continue

                scroll_token = body.get("scroll")
                current_ids = ids_in(rows)

                # --- pagination probe -------------------------------------
                # If page 2 comes back identical to page 1, the parameter name
                # is wrong. Try the next candidate before giving up on it.
                if page_index == 1 and previous_ids is not None:
                    if current_ids and current_ids == previous_ids and not param_settled:
                        remaining = [c for c in candidates if c != active_param]
                        for candidate in remaining:
                            log.warning(
                                "pagination: %r returned an identical page_id set — trying %r",
                                active_param, candidate,
                            )
                            retry = await fetch_page(client, keyword, page_index, candidate, scroll_token)
                            retry_ids = ids_in(retry.get("data") or []) if retry else set()
                            if retry_ids and retry_ids != previous_ids:
                                log.info("pagination: %r works — set PAGE_PARAM = %r", candidate, candidate)
                                active_param = candidate
                                HUNT["page_param"] = candidate
                                HUNT["pagination_ok"] = True
                                rows = retry["data"]
                                current_ids = retry_ids
                                scroll_token = retry.get("scroll")
                                break
                        else:
                            log.warning(
                                "PAGINATION NOT WORKING: every candidate %s returned the same "
                                "page_id set. Only page 1 is real — extra pages are duplicates. "
                                "Set pages=1, or find the right param and put it in PAGE_PARAM.",
                                PAGE_PARAM_CANDIDATES,
                            )
                            HUNT["pagination_ok"] = False
                            HUNT["errors"].append(
                                "Pagination has no effect — only page 1 is unique. "
                                "See PAGE_PARAM in server.py."
                            )
                        param_settled = True
                    elif current_ids and current_ids != previous_ids:
                        HUNT["pagination_ok"] = True
                        param_settled = True

                previous_ids = current_ids

                for ad in rows:
                    if isinstance(ad, dict) and ad.get("page_id"):
                        keyword_of.setdefault(str(ad["page_id"]), set()).add(keyword)
                all_rows.extend(r for r in rows if isinstance(r, dict))

                HUNT["raw_ads"] = len(all_rows)
                HUNT["done"] += 1

                if not rows:
                    break  # ran off the end of this keyword

    brands = aggregate(all_rows, keyword_of)

    # A brand with no renderable creative cannot be judged, which is the whole
    # point of the tool, so it never reaches the grid.
    usable = [b for b in brands if b["creatives"]]
    HUNT["dropped_no_creative"] = len(brands) - len(usable)

    upsert_brands(usable)
    CACHE_PATH.write_text(json.dumps({
        "fetched_at": now_iso(),
        "raw_ads": len(all_rows),
        "brands": len(usable),
        "dropped_no_creative": HUNT["dropped_no_creative"],
        "keywords": keywords,
        "data": usable,
    }, indent=2))

    HUNT.update(brands=len(usable), running=False, finished_at=now_iso(), current="")
    log.info(
        "hunt done: %d raw ads -> %d brands (%d dropped, no creative)",
        len(all_rows), len(usable), HUNT["dropped_no_creative"],
    )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(title="Creative Lead Hunter")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Cold DB but a cache on disk: reload it so the UI works fully offline.
    with closing(db()) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM brands").fetchone()["n"]
    if count == 0 and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text()).get("data", [])
            if cached:
                upsert_brands(cached)
                log.info("restored %d brands from %s", len(cached), CACHE_PATH.name)
        except Exception as exc:
            log.warning("could not restore cache: %s", exc)
    if MOCK_MODE:
        log.warning("HUNTER_MOCK=1 — serving the local fixture, not the live API")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.post("/api/hunt")
async def api_hunt(payload: dict = Body(default={})) -> dict:
    if HUNT["running"]:
        raise HTTPException(409, "A hunt is already running")
    keywords = [k.strip() for k in (payload.get("keywords") or DEFAULT_KEYWORDS) if str(k).strip()]
    if not keywords:
        raise HTTPException(400, "No keywords given")
    pages = max(1, min(int(payload.get("pages") or 1), 20))
    asyncio.create_task(run_hunt(keywords, pages))
    return {"started": True, "keywords": keywords, "pages": pages}


@app.get("/api/hunt/status")
def api_hunt_status() -> dict:
    return HUNT


def filtered(
    status: str | None, min_ig: int | None, max_ig: int | None,
    min_visits: int | None, max_visits: int | None,
    fmt: str | None, adscore: str | None, sort: str | None,
) -> list[dict]:
    sql = """
        SELECT b.*, o.status, o.notes
        FROM brands b LEFT JOIN outreach o ON o.page_id = b.page_id
        WHERE 1 = 1
    """
    args: list[Any] = []

    if status and status != "all":
        sql += " AND COALESCE(o.status, 'New') = ?"
        args.append(status)
    if min_ig is not None:
        sql += " AND COALESCE(b.ig_followers, 0) >= ?"
        args.append(min_ig)
    if max_ig is not None:
        sql += " AND COALESCE(b.ig_followers, 0) <= ?"
        args.append(max_ig)
    if min_visits is not None:
        sql += " AND COALESCE(b.monthly_visits, 0) >= ?"
        args.append(min_visits)
    if max_visits is not None:
        sql += " AND COALESCE(b.monthly_visits, 0) <= ?"
        args.append(max_visits)
    if fmt == "static":
        sql += " AND b.has_video = 0"
    elif fmt == "video":
        sql += " AND b.has_video = 1"

    if adscore and adscore != "all":
        wanted = [a.strip() for a in adscore.split(",") if a.strip()]
        if wanted:
            sql += " AND b.adscore IN (%s)" % ",".join("?" * len(wanted))
            args.extend(wanted)

    order = {
        "score": "b.lead_score DESC",
        "static": "b.static_pct DESC, b.lead_score DESC",
        "followers": "COALESCE(b.ig_followers, 0) DESC",
        "visits": "COALESCE(b.monthly_visits, 0) DESC",
        "ads": "b.ad_count DESC",
    }.get(sort or "score", "b.lead_score DESC")
    # Ties are common (a lot of brands land on 100), and without a stable
    # tiebreak the grid reshuffles between reloads -- which breaks j/k
    # navigation and makes you re-judge cards you already judged.
    sql += f" ORDER BY {order}, b.page_id"

    with closing(db()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [row_to_brand(r) for r in rows]


@app.get("/api/leads")
def api_leads(
    status: str | None = None,
    min_ig: int | None = None,
    max_ig: int | None = None,
    min_visits: int | None = None,
    max_visits: int | None = None,
    format: str | None = None,
    adscore: str | None = None,
    sort: str | None = "score",
) -> dict:
    brands = filtered(status, min_ig, max_ig, min_visits, max_visits, format, adscore, sort)
    with closing(db()) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM brands").fetchone()["n"]
    cache_meta = {}
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            cache_meta = {
                "fetched_at": cached.get("fetched_at"),
                "raw_ads": cached.get("raw_ads"),
                "dropped_no_creative": cached.get("dropped_no_creative"),
            }
        except Exception:
            pass
    return {
        "brands": brands,
        "shown": len(brands),
        "total_brands": total,
        "raw_ads": cache_meta.get("raw_ads"),
        "dropped_no_creative": cache_meta.get("dropped_no_creative"),
        "fetched_at": cache_meta.get("fetched_at"),
        "statuses": STATUSES,
    }


@app.patch("/api/leads/{page_id}")
def api_update_lead(page_id: str, payload: dict = Body(...)) -> dict:
    with closing(db()) as conn:
        exists = conn.execute("SELECT 1 FROM brands WHERE page_id = ?", (page_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Unknown page_id")

    status = payload.get("status")
    notes = payload.get("notes")
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")

    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO outreach(page_id, status, notes, updated_at) "
            "VALUES(?, 'New', '', ?)",
            (page_id, now_iso()),
        )
        if status is not None:
            conn.execute(
                "UPDATE outreach SET status = ?, updated_at = ? WHERE page_id = ?",
                (status, now_iso(), page_id),
            )
        if notes is not None:
            conn.execute(
                "UPDATE outreach SET notes = ?, updated_at = ? WHERE page_id = ?",
                (notes, now_iso(), page_id),
            )
        row = conn.execute(
            "SELECT status, notes FROM outreach WHERE page_id = ?", (page_id,)
        ).fetchone()
    return {"page_id": page_id, "status": row["status"], "notes": row["notes"]}


@app.get("/api/export")
def api_export(
    status: str | None = None,
    min_ig: int | None = None,
    max_ig: int | None = None,
    min_visits: int | None = None,
    max_visits: int | None = None,
    format: str | None = None,
    adscore: str | None = None,
    sort: str | None = "score",
) -> StreamingResponse:
    brands = filtered(status, min_ig, max_ig, min_visits, max_visits, format, adscore, sort)
    buf = io.StringIO()
    cols = [
        "page_name", "lead_score", "reason", "static_pct", "ad_count", "has_video",
        "ig_followers", "monthly_visits", "days_running", "adscore", "country_code",
        "is_shopify", "url_store", "url_all", "page_profile_uri", "status", "notes", "page_id",
    ]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for b in brands:
        writer.writerow({c: b.get(c, "") for c in cols})
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="leads-{stamp}.csv"'},
    )


@app.get("/api/settings")
def api_get_settings() -> dict:
    return {
        "dm_template": get_setting("dm_template", DEFAULT_DM_TEMPLATE),
        "default_keywords": DEFAULT_KEYWORDS,
        "statuses": STATUSES,
        "mock_mode": MOCK_MODE,
    }


@app.put("/api/settings")
def api_put_settings(payload: dict = Body(...)) -> dict:
    if "dm_template" in payload:
        set_setting("dm_template", str(payload["dm_template"]))
    return {"dm_template": get_setting("dm_template", DEFAULT_DM_TEMPLATE)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
