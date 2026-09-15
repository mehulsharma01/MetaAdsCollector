"""End-to-end UAE dental prospecting pipeline.

Stages, in the order they must run::

    1  discover   DataForSEO Business Listings over the geographic grid
    2  dedupe     collapse overlapping circles into unique clinics
    3  score      rank without enrichment, to decide who is worth enriching
    4  enrich     Meta ads + domain metrics, on the top slice only
    5  rescore    re-rank now that the opportunity signal exists
    6  export     write the CRM sheet

Stage 4 is deliberately not run over everything. Enrichment is the only
expensive part of this pipeline, in credits and in wall-clock time, and
most of the list will never be contacted. Scoring first and enriching the
top few hundred is what keeps a full UAE sweep cheap.

Typical run::

    export DATAFORSEO_LOGIN=... DATAFORSEO_PASSWORD=...

    python pipeline.py discover --out raw.json
    python pipeline.py build --raw raw.json --enrich-top 250 --out uae_dental.csv

Discovery is separated from the rest so that the expensive part happens
once. Re-run ``build`` as often as you like to retune the scoring without
paying for discovery again.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from typing import Any

import dedupe as dedupe_mod
import score as score_mod
from enrich_domain import PAID_ACTIVE, SEO_SOME, SEO_STRONG
from grid import build_queries

logger = logging.getLogger("pipeline")

# The CRM sheet. Columns fall into three groups: facts collected by the
# pipeline, judgements made by the scorer, and blanks you fill in as you
# work the list. The blanks are part of the design -- this file is meant to
# be opened in Sheets and edited, not just read.
COLUMNS = [
    # --- identity -------------------------------------------------------
    "clinic_name",
    "emirate",
    "area",
    "address",
    "website",
    "phone",
    "whatsapp",
    "email",
    "instagram",
    "linkedin",
    "maps_url",
    # --- decision maker (filled during stage 7 research) ----------------
    "decision_maker",
    "decision_maker_title",
    # --- facts from discovery -------------------------------------------
    "number_of_locations",
    "google_rating",
    "number_of_reviews",
    "main_treatments",
    "high_ticket_treatments",
    "published_prices",
    # --- marketing signals ----------------------------------------------
    "meta_ads_running",
    "meta_active_ad_count",
    "meta_has_video",
    "ad_library_url",
    "meta_sample_headline",
    "google_ads_running",
    "seo_presence",
    "existing_agency_signals",
    "website_quality",
    "lead_gen_opportunity",
    # --- scoring ---------------------------------------------------------
    "priority_score",
    "tier",
    "score_high_ticket",
    "score_demand",
    "score_capability",
    "score_opportunity",
    "score_reachability",
    "score_size",
    "why",
    # --- outreach workflow (yours to fill) -------------------------------
    "outreach_channel",
    "outreach_status",
    "last_contacted",
    "response",
    "call_booked",
    "proposal_sent",
    "closed_won_lost",
    "notes",
    # --- keys -------------------------------------------------------------
    "place_id",
    "cid",
    "group_key",
]

# Treatments worth naming in the sheet, so you can sort by what they sell.
HIGH_TICKET_LABELS = {
    "veneers": "Veneers",
    "hollywood_smile": "Hollywood smile",
    "smile_makeover": "Smile makeover",
    "implants": "Implants",
    "full_mouth": "Full-mouth / All-on-X",
    "invisalign": "Invisalign / aligners",
    "zirconia": "Zirconia crowns",
    "orthodontics": "Orthodontics",
}


def _whatsapp(phone: str | None) -> str:
    """Return a click-to-chat link when the number is a UAE mobile.

    Landlines are left blank rather than linked, because a wa.me link to a
    clinic switchboard goes nowhere and wastes a first touch.
    """
    digits = dedupe_mod.normalize_phone(phone)
    if digits and digits[:2] in score_mod.MOBILE_PREFIXES:
        return f"https://wa.me/971{digits}"
    return ""


def _website_quality(clinic: dict[str, Any]) -> str:
    """A coarse read on whether the site can carry paid traffic."""
    domain = dedupe_mod.normalize_domain(clinic.get("domain") or clinic.get("url"))
    if not domain:
        return "no website"
    if not dedupe_mod.is_own_domain(domain):
        return f"social only ({domain})"

    metrics = clinic.get("_domain_metrics") or {}
    organic = metrics.get("organic_count")
    if organic is None:
        return "own domain (not assessed)"
    if organic >= SEO_STRONG:
        return "own domain, strong search footprint"
    if organic >= SEO_SOME:
        return "own domain, moderate search footprint"
    return "own domain, thin search footprint"


def _agency_signals(clinic: dict[str, Any]) -> str:
    """Summarise every reason to think someone is already being paid."""
    signals: list[str] = []
    metrics = clinic.get("_domain_metrics") or {}
    meta = clinic.get("_meta_ads") or {}

    organic = metrics.get("organic_count") or 0
    paid = metrics.get("paid_count") or 0
    if organic >= SEO_STRONG:
        signals.append(f"SEO agency likely ({organic} keywords)")
    elif organic >= SEO_SOME:
        signals.append(f"some SEO work ({organic} keywords)")
    if paid >= PAID_ACTIVE:
        signals.append(f"Google Ads active ({paid} keywords)")
    if meta.get("running"):
        count = meta.get("active_ad_count") or 0
        signals.append(f"Meta ads active ({count})")
        if count >= 10:
            signals.append("media buyer likely engaged")

    return "; ".join(signals) if signals else "no agency signals detected"


def _lead_gen_opportunity(clinic: dict[str, Any], card: score_mod.ScoreCard) -> str:
    """The single most useful sentence in the sheet: what to open with.

    This is what goes into the first message. It is phrased as an
    observation about something publicly visible, never as a claim about
    the clinic's results, which you have no way of knowing.
    """
    meta = clinic.get("_meta_ads") or {}
    metrics = clinic.get("_domain_metrics") or {}

    if meta.get("running") and (meta.get("active_ad_count") or 0) <= 3:
        return "Running ads but rotating very few creatives - ask what they are testing"
    if meta.get("running") and meta.get("has_video") is False:
        return "Running ads, all static - video is the obvious untested angle"
    if metrics.get("opportunity_gap"):
        return (
            f"Ranks for {metrics.get('organic_count')} keywords organically but buys almost none "
            "- the intent is there and unclaimed"
        )
    if meta.get("running") is False and dedupe_mod.is_own_domain(
        dedupe_mod.normalize_domain(clinic.get("domain") or clinic.get("url"))
    ):
        return "Good site and reviews, no Meta presence at all - whole channel untouched"
    if not clinic.get("phone"):
        return "No phone on the Google listing - losing calls before any ad spend matters"
    if card.demand >= 12 and card.capability >= 10:
        return "Established and capable - worth a direct approach even without a visible gap"
    return "No clear gap identified - deprioritise"


def to_row(clinic: dict[str, Any], card: score_mod.ScoreCard) -> dict[str, Any]:
    """Flatten one scored clinic into a CRM row."""
    address_info = clinic.get("address_info") or {}
    rating = clinic.get("rating") or {}
    meta = clinic.get("_meta_ads") or {}
    metrics = clinic.get("_domain_metrics") or {}

    _, found = score_mod.score_high_ticket(clinic)
    high_ticket = sorted({HIGH_TICKET_LABELS[f] for f in found if f in HIGH_TICKET_LABELS})

    services = clinic.get("services") or []
    main_treatments = sorted({(s.get("title") or "").strip() for s in services if s.get("title")})
    priced = [
        f"{s.get('title')}: {s['price'].get('displayed_price')}"
        for s in services
        if s.get("price") and s.get("title")
    ]

    headlines = meta.get("sample_headlines") or []

    return {
        "clinic_name": clinic.get("title") or "",
        "emirate": address_info.get("region") or "",
        "area": address_info.get("borough") or "",
        "address": clinic.get("address") or "",
        "website": clinic.get("url") or clinic.get("domain") or "",
        "phone": clinic.get("phone") or "",
        "whatsapp": _whatsapp(clinic.get("phone")),
        "email": "",
        "instagram": "",
        "linkedin": "",
        "maps_url": clinic.get("check_url") or "",
        "decision_maker": "",
        "decision_maker_title": "",
        "number_of_locations": clinic.get("location_count") or 1,
        "google_rating": rating.get("value") or "",
        "number_of_reviews": rating.get("votes_count") or 0,
        # Keep the sheet readable; the full list stays in the raw JSON.
        "main_treatments": ", ".join(main_treatments[:12]),
        "high_ticket_treatments": ", ".join(high_ticket),
        "published_prices": " | ".join(priced[:6]),
        "meta_ads_running": {True: "yes", False: "no"}.get(meta.get("running"), "unknown"),
        "meta_active_ad_count": meta.get("active_ad_count") or "",
        "meta_has_video": {True: "yes", False: "no"}.get(meta.get("has_video"), ""),
        "ad_library_url": meta.get("ad_library_url") or "",
        "meta_sample_headline": headlines[0] if headlines else "",
        "google_ads_running": metrics.get("google_ads") or "unknown",
        "seo_presence": metrics.get("seo_presence") or "unknown",
        "existing_agency_signals": _agency_signals(clinic),
        "website_quality": _website_quality(clinic),
        "lead_gen_opportunity": _lead_gen_opportunity(clinic, card),
        "priority_score": card.total,
        "tier": card.tier,
        "score_high_ticket": card.high_ticket,
        "score_demand": card.demand,
        "score_capability": card.capability,
        "score_opportunity": card.opportunity,
        "score_reachability": card.reachability,
        "score_size": card.size,
        "why": card.why,
        "outreach_channel": "",
        "outreach_status": "not started",
        "last_contacted": "",
        "response": "",
        "call_booked": "",
        "proposal_sent": "",
        "closed_won_lost": "",
        "notes": "",
        "place_id": clinic.get("place_id") or "",
        "cid": clinic.get("cid") or "",
        "group_key": clinic.get("group_key") or "",
    }


def cmd_discover(args: argparse.Namespace) -> int:
    """Stage 1: run the grid and save raw listings."""
    import discover as discover_mod

    queries = build_queries()
    if args.limit:
        queries = queries[: args.limit]

    def progress(done: int, total: int, query: Any) -> None:
        print(f"  [{done:3d}/{total}] {query.label}", file=sys.stderr)

    print(f"Running {len(queries)} discovery queries...", file=sys.stderr)
    raw = discover_mod.discover(queries, delay=args.delay, on_progress=progress)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False)

    print(f"\n{len(raw)} raw listings -> {args.out}", file=sys.stderr)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Stages 2-6: dedupe, score, enrich the top slice, rescore, export."""
    with open(args.raw, encoding="utf-8") as handle:
        raw = json.load(handle)
    print(f"{len(raw)} raw listings loaded", file=sys.stderr)

    clinics = dedupe_mod.deduplicate(raw)
    print(f"{len(clinics)} unique clinics after dedupe", file=sys.stderr)

    # First pass: rank without enrichment so we know who deserves it.
    ranked = sorted(
        ((clinic, score_mod.score_clinic(clinic)) for clinic in clinics),
        key=lambda pair: pair[1].total,
        reverse=True,
    )
    kept = [(c, s) for c, s in ranked if s.disqualified is None]
    dropped = len(ranked) - len(kept)
    print(f"{dropped} disqualified, {len(kept)} in play", file=sys.stderr)

    if args.enrich_top:
        top = [clinic for clinic, _ in kept[: args.enrich_top]]
        print(f"\nEnriching top {len(top)} clinics...", file=sys.stderr)

        if not args.skip_domain:
            import enrich_domain

            def domain_progress(done: int, total: int, clinic: dict) -> None:
                if done % 25 == 0 or done == total:
                    print(f"  domain {done}/{total}", file=sys.stderr)

            enrich_domain.enrich(top, delay=args.delay, on_progress=domain_progress)

        if not args.skip_meta:
            import enrich_meta

            def meta_progress(done: int, total: int, clinic: dict) -> None:
                if done % 10 == 0 or done == total:
                    print(f"  meta {done}/{total}", file=sys.stderr)

            enrich_meta.enrich(top, delay=args.meta_delay, on_progress=meta_progress)

        # Rescore: the opportunity component only has data now.
        kept = sorted(
            ((clinic, score_mod.score_clinic(clinic)) for clinic, _ in kept),
            key=lambda pair: pair[1].total,
            reverse=True,
        )

    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for clinic, card in kept:
            writer.writerow(to_row(clinic, card))

    tiers: dict[str, int] = {}
    for _, card in kept:
        tiers[card.tier] = tiers.get(card.tier, 0) + 1

    print(f"\n{len(kept)} clinics -> {args.out}", file=sys.stderr)
    print(f"  Tier A (contact first): {tiers.get('A', 0)}", file=sys.stderr)
    print(f"  Tier B:                 {tiers.get('B', 0)}", file=sys.stderr)
    print(f"  Tier C:                 {tiers.get('C', 0)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    discover_parser = sub.add_parser("discover", help="stage 1: run the discovery grid")
    discover_parser.add_argument("--out", default="raw.json")
    discover_parser.add_argument("--limit", type=int, help="run only the first N queries")
    discover_parser.add_argument("--delay", type=float, default=1.0)
    discover_parser.set_defaults(func=cmd_discover)

    build_parser = sub.add_parser("build", help="stages 2-6: dedupe, score, enrich, export")
    build_parser.add_argument("--raw", default="raw.json")
    build_parser.add_argument("--out", default="uae_dental.csv")
    build_parser.add_argument(
        "--enrich-top",
        type=int,
        default=250,
        help="enrich this many top-scoring clinics (0 to skip enrichment entirely)",
    )
    build_parser.add_argument("--skip-meta", action="store_true")
    build_parser.add_argument("--skip-domain", action="store_true")
    build_parser.add_argument("--delay", type=float, default=0.5)
    build_parser.add_argument("--meta-delay", type=float, default=2.0)
    build_parser.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
