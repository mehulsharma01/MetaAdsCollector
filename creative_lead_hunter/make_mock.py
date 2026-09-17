#!/usr/bin/env python3
"""
Build `mock_api.json` — an offline stand-in for the ad-library API.

The fixture deliberately reproduces every quirk the real API has, because
those are the things worth testing against:

  * one row per AD, so brands repeat many times (dedupe on page_id)
  * product_* fields identical and stale on every single record
  * creative fields that are sometimes empty, sometimes a bare content hash
  * two brands with no renderable creative at all, which must be filtered out
  * one brand already running mostly video, which must NOT score well

Run it directly, or let `HUNTER_MOCK=1` generate it on first use.
"""

import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "mock_api.json"

# Every record returns this, regardless of the actual product. It is never read.
STALE = {
    "product_title": "High Waisted American Flag Shorts",
    "product_image": "https://cdn.example.com/stale-shorts.jpg",
    "product_price": "29.99",
    "product_handle": "high-waisted-american-flag-shorts",
}

#        name,              ig,     visits,  ads, static, adscore,   cc,   creative
BRANDS = [
    ("Lumen Skin",          5700,      533,   9, 1.00, "Testing", "US", "ok"),
    ("Dewy Ritual",        12400,     4100,  14, 1.00, "Scaling", "US", "ok"),
    ("Petal & Poise",       2300,      820,   6, 0.83, "Testing", "GB", "ok"),
    ("Yuzu Glow",          28000,    18500,  22, 0.91, "Scaling", "AU", "ok"),
    ("Molten Lip Co",        890,      140,   4, 1.00, "Testing", "US", "ok"),
    ("Silk Root Hair",      9100,     2600,  11, 0.55, "Testing", "CA", "ok"),
    ("Aurelia Serum",      64000,    41000,  31, 0.74, "Winning", "US", "ok"),
    ("BigBeauty Global",  420000,   380000,  60, 0.95, "Winning", "US", "ok"),
    ("Traffic Monster",    15000,   120000,  26, 1.00, "Scaling", "US", "ok"),
    ("Clay & Cactus",       3400,     1150,   7, 1.00, "Testing", "US", "ok"),
    ("Noir Nail Lab",       1800,      310,   5, 1.00, "Testing", "FR", "ok"),
    ("Veil Sunscreen",      7600,     9200,  13, 0.62, "Scaling", "US", "ok"),
    ("Ghost Brand",         4200,      700,   5, 1.00, "Testing", "US", "none"),
    ("Hash Only Co",        3100,      450,   4, 1.00, "Testing", "US", "hash"),
    ("Video First",         8800,     5400,  10, 0.10, "Scaling", "US", "video"),
]


def build() -> dict:
    random.seed(7)  # stable fixture, so test expectations stay valid
    rows = []

    for i, (name, ig, visits, ads, static_ratio, adscore, cc, creative) in enumerate(BRANDS):
        page_id = f"10{i:04d}5550{i:03d}"
        slug = name.lower().replace(" ", "").replace("&", "")

        for a in range(ads):
            static = a < round(ads * static_ratio)
            fmt = random.choice(["image", "carousel", "dco"]) if static else "video"

            img = poster = video = None
            cards = []

            if creative in ("ok", "video"):
                if fmt == "carousel":
                    cards = [{"image": f"https://cdn.test/{slug}/c{a}_{k}.jpg", "poster": None}
                             for k in range(3)]
                elif fmt == "video":
                    video = f"https://cdn.test/{slug}/v{a}.mp4"
                    poster = f"https://cdn.test/{slug}/v{a}_poster.jpg"
                else:
                    img = f"https://cdn.test/{slug}/i{a}.jpg"
                if a % 5 == 4:  # some ads genuinely carry nothing renderable
                    img = poster = video = None
                    cards = []
            elif creative == "hash":
                img = "a3f9c2e1b7d840ff"  # a bare content hash, not a URL
                poster = ""
            # "none": everything stays None, so the brand must be filtered out

            row = {
                "pageName": name,
                "page_id": page_id,
                "urlStore": f"https://{slug}.myshopify.com",
                "product_url": f"https://{slug}.myshopify.com/products/hero",
                "page_profile_uri": f"https://facebook.com/{slug}",
                "urlAll": f"https://www.facebook.com/ads/library/?view_all_page_id={page_id}",
                "igFollowers": ig,
                "page_like_count": int(ig * 0.6),
                "store_traffic": {"monthly_visits": visits},
                "display_format": fmt,
                "image": img,
                "poster": poster,
                "video": video,
                "cards": cards,
                "adscore": adscore,
                "adscore_reasons": ["high ctr"] if adscore == "Winning" else ["low ctr", "new creative"],
                "total_adspend": f"${(i + 1) * 1200}",
                "daysrunning": 3 + a * 2,
                "lastSeen": "2026-09-16",
                "country_code": cc,
                "technologies": ["SH", "KL"] if i % 3 else ["WC"],
                "copy": f"{name}: the serum that actually works. Clinically tested, "
                        f"dermatologist approved, 30-day results or your money back. "
                        f"Ad variant {a + 1}.",
                "caption": f"{slug}.myshopify.com",
            }
            row.update(STALE)
            rows.append(row)

    random.shuffle(rows)  # the real API does not group a brand's ads together
    return {"data": rows, "limit": 20, "scroll": None, "total": 383806}


def write() -> Path:
    payload = build()
    OUT.write_text(json.dumps(payload, indent=1))
    return OUT


if __name__ == "__main__":
    path = write()
    data = json.loads(path.read_text())["data"]
    print(f"{path.name}: {len(data)} ad rows across {len(BRANDS)} brands")
    print("expected after dedupe + creative filter: 13 brands (2 dropped)")
