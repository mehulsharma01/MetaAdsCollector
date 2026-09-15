# UAE dental prospecting pipeline

Builds a scored, deduplicated database of UAE dental clinics from DataForSEO
Business Listings, enriched with Meta Ad Library and search-footprint signals,
and exports a CRM sheet ranked by how likely each clinic is to buy paid patient
acquisition.

Built as a worked example of using `meta_ads_collector` for competitive
prospecting rather than ad archiving.

## Why this returns ~25x more clinics than a Maps scrape

A Google Maps search returns what fits on a results page. Scraping it harder
does not help, because the cap is on the result set, not the scrape.

This pipeline queries the **Business Listings database** instead, which is a
stored index of Google Business profiles. Two consequences:

- Results page through `offset` instead of hitting a ranking cap.
- One request returns the full profile: categories, services, **published
  prices**, ratings, hours, website, phone. No per-clinic detail call.

Measured totals from this grid (September 2026, `total_count` as reported by
the API, before dedupe):

| Circle | Categories | Listings |
|---|---|---:|
| Dubai, 60km | `dental_clinic` only | **1,295** |
| Dubai, 60km | `dentist` only | 697 |
| Sharjah 20km (overlaps Dubai) | all 10 dental | 1,054 |
| Abu Dhabi, 80km | all 10 dental | 362 |
| Ajman, 12km | all 10 dental | 154 |
| Ras Al Khaimah, 40km | all 10 dental | 105 |
| Fujairah, 40km | all 10 dental | 68 |

Circles overlap by design, so these do **not** sum to a national total.
Deduplication resolves the overlap; a coverage gap cannot be recovered later.

Applying a quality filter (rating ≥ 4.6, reviews > 400) to Dubai's cosmetic and
implant categories narrows 1,295 listings to **86** clinics. That filtered set,
not the raw list, is the thing worth working.

## Install

```bash
pip install -e ../..          # the meta_ads_collector package
export DATAFORSEO_LOGIN="..."
export DATAFORSEO_PASSWORD="..."
```

Run commands from inside this directory; the modules import each other by
plain module name.

## Run

```bash
# Stage 1 -- discovery. The expensive part. Run once.
python pipeline.py discover --out raw.json

# Stages 2-6 -- dedupe, score, enrich the top slice, rescore, export.
# Re-run freely to retune scoring without paying for discovery again.
python pipeline.py build --raw raw.json --enrich-top 250 --out uae_dental.csv
```

Useful variants:

```bash
python pipeline.py discover --limit 12          # smoke test: first 12 circles
python pipeline.py build --enrich-top 0         # score only, no enrichment
python pipeline.py build --skip-meta            # domain metrics only
```

## Stages

| # | Stage | Module | Cost |
|---|---|---|---|
| 1 | Discovery over 112 geo × category circles | `discover.py` | DataForSEO, per request |
| 2 | Deduplication | `dedupe.py` | free |
| 3 | First-pass scoring | `score.py` | free |
| 4 | Enrichment: search footprint | `enrich_domain.py` | DataForSEO, 1 per domain |
| 4 | Enrichment: Meta ads | `enrich_meta.py` | free (Ad Library) |
| 5 | Rescoring | `score.py` | free |
| 6 | CRM export | `pipeline.py` | free |

**Stage 4 is deliberately not run over everything.** Enrichment is the only
expensive part, in credits and wall-clock time, and most of the list will never
be contacted. Score first, enrich the top few hundred, then rescore. That
ordering is what keeps a full national sweep cheap.

## The grid

112 queries: 33 geographic circles crossed with 3 category sets, plus a fourth
`adjacent` sweep over the 13 tier-1 areas.

```bash
python grid.py     # print all 112 with coordinates
```

Radii are tuned to clinic density: 4km in dense corridors like Karama and
Deira, up to 20km in Al Ain. Areas are ordered so that an interrupted run still
covers the highest-value territory first.

**The `adjacent` set is the one that matters most for coverage.** Clinics that
categorise themselves only as "Medical center" never appear in a dental search.
Two of the eight clinics in the validation sample — Dr Sunny Medical Centre and
Dr. Sirajudeen Medical Centre — are exactly this case.

## Deduplication

Four matching signals, most reliable first:

1. `place_id` — Google's own identifier. Exact.
2. `cid` — Maps customer ID. Exact.
3. `domain` + `phone`, **within 400m** — same clinic listed twice.
4. Normalised `name` + `borough`, within 400m — for listings with neither.

The distance constraint on rules 3 and 4 is what keeps genuine multi-branch
groups intact. Dr. Joy Dental Clinic appears three times in Dubai sharing one
domain and one toll-free number; those are three clinics, not one. They stay as
three rows, linked by `group_key`, and `location_count` feeds the size score.

Social domains (`facebook.com`, `wa.me`, `linktr.ee`, …) are never used as a
merge key — thousands of clinics "have" a facebook.com website.

## Scoring

0–100, six components:

| Component | Max | Question it answers |
|---|---:|---|
| `high_ticket` | 25 | Does it sell treatments worth advertising? |
| `demand` | 20 | Do patients already choose it? |
| `capability` | 15 | Can it afford a retainer? |
| `opportunity` | 20 | Is there a gap you can visibly fix? |
| `reachability` | 10 | Can you get to the decision maker? |
| `size` | 10 | Is the sale short enough to close alone? |

Tiers: **A** ≥ 70, **B** 50–69, **C** < 50. Disqualified rows get tier `X`.

Three design decisions worth knowing:

- **Running Meta ads scores _highest_, not lowest.** It looks backwards —
  a competitor is already in the account — but it proves budget exists,
  internal approval exists, and the clinic believes paid works. Displacing an
  incumbent is a far shorter sale than creating a category.
- **A rating below 4.0 halves the demand score.** Ads amplify a reputation
  problem, they do not fix it. A clinic with 800 reviews at 3.2 stars is a bad
  first client.
- **Fewer than 10 reviews caps the total at 35**, whatever else it scores. An
  unreviewed listing cannot be verified as a going concern.

Disqualified outright: permanently closed, hospitals, dental schools,
laboratories and suppliers.

### The weights are guesses

Every number in `score.py` is a prior, not a measurement. Nothing here has been
validated against a single UAE reply. After 100 messages, feed the real
outcomes back:

```python
from score import recalibrate
print(recalibrate([(73, True), (68, False), ...]))   # (score, got_a_reply)
```

If tier A does not reply at a noticeably higher rate than tier B, the weights
are wrong for this market and should be changed before sending more.

## Output

49 columns, in four groups:

- **Facts** collected by the pipeline: name, area, website, phone, WhatsApp
  link, rating, reviews, treatments, published prices, location count.
- **Signals**: `meta_ads_running`, `meta_active_ad_count`, `ad_library_url`,
  `google_ads_running`, `seo_presence`, `existing_agency_signals`.
- **Judgements**: `priority_score`, `tier`, the six component scores, `why`,
  and `lead_gen_opportunity` — the observation to open the first message with.
- **Blanks you fill in**: decision maker, email, Instagram, LinkedIn, outreach
  status, last contacted, response, call booked, proposal, closed/lost.

The blanks are part of the design. This file is meant to be opened in Sheets
and worked, not just read.

## What this pipeline does not do

- **Email, Instagram and LinkedIn are left blank.** They are not in the
  Business Listings response. Fill them with an Apify website-contact scraper
  over the `website` column, on the A list only.
- **Decision-maker names are left blank.** No API resolves "who owns this
  clinic" reliably in the UAE. For doctor-named clinics the name is in the
  title, which is why `reachability` rewards them.
- **`paid.count` of zero is weak evidence, not proof.** A clinic running only
  Performance Max, or only on Meta, shows zero paid keywords while still
  spending.
- **"Running ads" says nothing about whether the ads work.** The Ad Library
  shows what is live, never its performance. Any claim about a clinic's
  results would be invented.

Use these signals to decide who to contact and what to ask about. Never quote
them to a clinic as facts about their business.
