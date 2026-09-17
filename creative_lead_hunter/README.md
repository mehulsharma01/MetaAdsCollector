# Creative Lead Hunter

Find small beauty DTC brands running **static-only ad creative** — the ones who need
product video and don't have an agency to make it.

Pulls from the Winning Hunter public ad-library API, collapses per-ad rows into
per-brand records, scores each brand as a prospect, and puts their **actual ad
creative** on screen so you can judge quality in a second and triage with the
keyboard.

---

## Setup

```bash
cd creative_lead_hunter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --port 8000
```

Open **http://127.0.0.1:8000**, press **Hunt**.

That's it — no build step, no API key, no node.

---

## Using it

Set keywords (comma-separated) and pages-per-keyword, press **Hunt**, watch the
progress in the top bar. Everything after that is filtering and triage.

**Keyboard — this is how you get through 200 brands:**

| key | does |
|---|---|
| `j` / `k` | next / previous card |
| `s` | shortlist and advance |
| `x` | reject and advance |
| `enter` | open the Shopify store in a new tab |
| `esc` | close the lightbox or the template panel |

Click any thumbnail for full size. Video creatives show a play badge and play in
the lightbox. Click the ad copy to expand it past two lines.

Status and notes save the moment you change them — status on select, notes on
blur. The **Shortlist** tab gives you a plain-text `brand — store URL` list with
a copy button, for the outreach session itself.

**Copy DM** puts your template on the clipboard with `{brand}` filled in. Edit it
under **Template**; it is stored in the database, not the browser.

---

## Scoring

0–100, higher = better prospect. Four signals, then two penalties.

| Signal | Max | Why |
|---|---|---|
| **Static share** | 40 | Running only stills is the whole thesis — they need video, which is the offer |
| **1k–30k IG followers** | 25 | Big enough to pay, small enough to have no agency |
| **200–20k monthly visits** | 20 | A real store, still owner-operated |
| **Testing / Scaling** | 15 | Spending but not winning: creative is the bottleneck |

| Penalty | |
|---|---|
| **> 50k monthly visits** | up to −30 — they have an agency, not a gap |
| **> 100k IG followers** | up to −30 — same |

The bands are trapezoids, not cliffs: a brand just outside one still scores, it
just scores less.

**The fit signals scale with the creative gap.** Followers, traffic and ad-score
are multiplied by `0.35 + 0.65 × static%`. A perfectly-sized brand already
running 90% video does not need what you sell, and without this it outranked
brands that do.

Each brand gets a one-line reason, e.g.

```
100% static ads, 5.7k IG, 533 visits/mo — testing and losing
95% static ads, 420k IG, 380k visits/mo — big traffic and big following — probably already has an agency
```

Tune the weights in `score_brand()` in `server.py`.

---

## The API's three traps

All three are handled; this is what to know if you change the fetching code.

**1. Results are per-AD, not per-brand.** A brand with 40 live ads arrives 40
times. Everything is deduped on `page_id` in `aggregate()` before it goes
anywhere. The UI shows both numbers — `227 raw ads → 13 brands` — so you can
always see what the API actually returned versus what you're looking at.

**2. `product_title` / `product_image` / `product_price` / `product_handle` are
stale garbage.** Every record returns "High Waisted American Flag Shorts". They
are never read and never served. Verified by test.

**3. Pagination is undocumented and `scroll` comes back null.**

```python
PAGE_PARAM = "page"                                # server.py — change here
PAGE_PARAM_CANDIDATES = ["page", "offset", "scroll"]
AUTO_DETECT_PAGINATION = True
```

On a multi-page run it fetches page 1, then page 2, and compares the `page_id`
sets. If they're identical the parameter is wrong, so it retries with the next
candidate and keeps whichever one actually changes the results. What it settles
on is logged:

```
INFO  pagination: 'offset' works — set PAGE_PARAM = 'offset'
```

Pin that in `PAGE_PARAM` afterwards and set `AUTO_DETECT_PAGINATION = False` to
skip the probe. If **no** candidate works you get a loud warning in the console
and a banner in the UI:

```
WARNING  PAGINATION NOT WORKING: every candidate ['page', 'offset', 'scroll']
         returned the same page_id set. Only page 1 is real — extra pages are
         duplicates. Set pages=1, or find the right param and put it in PAGE_PARAM.
```

Requests are rate limited to one per **1.2s** across the whole run. Timeouts,
non-200s and non-JSON bodies are logged and skipped — one bad request never
kills a hunt.

---

## Files

| | |
|---|---|
| `server.py` | The whole backend. FastAPI, single file. |
| `index.html` | The whole frontend. Vanilla JS, no build. |
| `leads.db` | SQLite. Brands, **and your outreach state in its own table** so re-hunting never clobbers a status or a note. |
| `leads.json` | Raw cache of the last hunt. If `leads.db` is missing on boot, it is restored from here — so the UI works fully offline after the first hunt. |
| `mock_api.json` | Fixture reproducing all three API traps, for offline development. |

Delete `leads.db` to reset outreach state; delete `leads.json` too for a clean slate.

---

## Endpoints

```
POST   /api/hunt          {keywords: [...], pages: int}   starts a background hunt
GET    /api/hunt/status                                    progress, counts, pagination result
GET    /api/leads         ?status=&min_ig=&max_ig=&min_visits=&max_visits=
                          &format=all|static|video&adscore=&sort=score|static|followers|visits|ads
PATCH  /api/leads/{page_id}  {status, notes}
GET    /api/export        same filters, returns CSV
GET/PUT /api/settings     the DM template
```

---

## Offline / development mode

```bash
HUNTER_MOCK=1 uvicorn server:app --port 8000
```

Serves `mock_api.json` instead of the network — 227 ad rows across 15 brands,
including two with no usable creative (they get filtered out, as they should)
and one that publishes bare content hashes instead of image URLs.

---

## Things it deliberately will not do

- **Show a brand with no renderable creative.** You can't judge what you can't
  see, so those are dropped and the count of dropped brands is displayed.
- **Render a non-http creative URL.** Some records carry bare content hashes in
  the image fields; putting one in an `<img src>` is a broken-image icon.
- **Show a broken image.** Every thumbnail falls back to a grey placeholder —
  expected, since Meta's CDN URLs expire.
- **Touch the stale `product_*` fields.**
- **Overwrite your outreach state on a re-hunt.**

---

## Note on verification

The scoring, dedupe, filters, CSV, keyboard triage, persistence and the
pagination-failure path were all tested end to end against `mock_api.json`.

**The live API call itself is untested** — the network this was built on blocks
`app.winninghunter.com`, so the real response shape, the real pagination
parameter and the real field names could not be confirmed against the server.
The field handling is defensive (missing keys, wrong types, `"12.3k"`-style
numbers and non-JSON bodies all degrade rather than crash), but the first real
hunt is the first time that code path runs for real. If something looks wrong,
the console log prints exactly what came back.
