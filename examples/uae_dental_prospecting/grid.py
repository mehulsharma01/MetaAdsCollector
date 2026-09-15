"""Geographic and category grid for UAE dental clinic discovery.

The point of this module is coverage. A single "dental clinic in Dubai" query
returns whatever Google decides to rank that day -- typically 20 to 60 results.
Running a grid of overlapping geographic circles crossed with category sets
instead returns the underlying business database, which is roughly 25x larger.

Two failure modes this grid is built to avoid:

1. **Radius too large.** Google Business data is capped per response. One
   200km circle over the whole UAE returns far fewer businesses than 33 small
   circles covering the same ground, because each circle gets its own cap.

2. **Terminology drift.** A clinic that categorises itself only as
   "Medical center" or "Prosthodontist" never appears in a "dental clinic"
   search. The category sets below cover every dental-adjacent Google
   category, including the medical-center catch-all.

Circles deliberately overlap. Duplicates are expected and removed downstream
by :mod:`dedupe`; a gap in coverage cannot be recovered later.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Category sets -------------------------------------------------------
#
# The DataForSEO Business Listings API accepts at most 10 categories per
# request, so the dental taxonomy is split into four sets. Every location is
# crossed with CORE, COSMETIC and SPECIALIST. ADJACENT is run only over the
# dense urban centres, where multi-speciality centres that bury dentistry
# under "Medical center" are concentrated.

CORE = [
    "dental_clinic",
    "dentist",
    "dental_hygienist",
    "emergency_dental_service",
    "dental_radiology",
]

# The money categories. A clinic carrying these sells treatments in the
# thousands of dirhams, which is what makes paid acquisition profitable.
COSMETIC = [
    "cosmetic_dentist",
    "teeth_whitening_service",
    "dental_implants_periodontist",
    "dental_implants_provider",
    "prosthodontist",
    "periodontist",
]

SPECIALIST = [
    "orthodontist",
    "pediatric_dentist",
    "endodontist",
    "oral_surgeon",
    "denture_care_center",
]

# Catch-all for clinics that do dentistry but do not categorise as dental.
ADJACENT = [
    "medical_center",
    "medical_clinic",
    "medical_group",
    "specialized_clinic",
]

CATEGORY_SETS = {
    "core": CORE,
    "cosmetic": COSMETIC,
    "specialist": SPECIALIST,
    "adjacent": ADJACENT,
}


@dataclass(frozen=True)
class Area:
    """One geographic circle in the discovery grid.

    Attributes:
        emirate: Emirate name, used for reporting and territory splits.
        name: Neighbourhood or city name.
        lat: Latitude, max 7 decimal places.
        lng: Longitude, max 7 decimal places.
        radius_km: Circle radius in kilometres.
        tier: Commercial priority of the area itself. ``1`` marks
            high-income catchments where cosmetic dentistry sells; these are
            swept with the ADJACENT set as well.
    """

    emirate: str
    name: str
    lat: float
    lng: float
    radius_km: int
    tier: int


# --- Dubai ---------------------------------------------------------------
# Radii are tuned to the density of each area: 4km in dense clinic corridors
# like Karama and Deira, up to 10km in the low-density southern suburbs.

DUBAI = [
    Area("Dubai", "Jumeirah & Umm Suqeim", 25.2048, 55.2400, 6, 1),
    Area("Dubai", "Business Bay", 25.1857, 55.2766, 4, 1),
    Area("Dubai", "Downtown Dubai", 25.1972, 55.2744, 4, 1),
    Area("Dubai", "Dubai Marina & JBR", 25.0805, 55.1403, 5, 1),
    Area("Dubai", "Jumeirah Lakes Towers", 25.0693, 55.1413, 4, 1),
    Area("Dubai", "Al Barsha", 25.1000, 55.2000, 6, 1),
    Area("Dubai", "Dubai Healthcare City", 25.2290, 55.3220, 4, 1),
    Area("Dubai", "Sheikh Zayed Rd & Trade Centre", 25.2200, 55.2800, 5, 1),
    Area("Dubai", "Al Karama", 25.2450, 55.3020, 4, 2),
    Area("Dubai", "Bur Dubai & Mankhool", 25.2532, 55.2960, 4, 2),
    Area("Dubai", "Deira", 25.2697, 55.3095, 5, 2),
    Area("Dubai", "Al Qusais & Al Nahda", 25.2800, 55.3800, 6, 2),
    Area("Dubai", "Mirdif", 25.2200, 55.4200, 6, 2),
    Area("Dubai", "Jumeirah Village Circle", 25.0600, 55.2100, 6, 2),
    Area("Dubai", "Dubai Silicon Oasis", 25.1200, 55.3800, 7, 2),
]

# --- Abu Dhabi -----------------------------------------------------------

ABU_DHABI = [
    Area("Abu Dhabi", "Al Khalidiyah", 24.4700, 54.3400, 4, 1),
    Area("Abu Dhabi", "Corniche & Al Markaziyah", 24.4900, 54.3600, 4, 1),
    Area("Abu Dhabi", "Al Bateen", 24.4500, 54.3300, 4, 1),
    Area("Abu Dhabi", "Al Reem Island", 24.4950, 54.4050, 4, 1),
    Area("Abu Dhabi", "Khalifa City", 24.4200, 54.5800, 8, 1),
    Area("Abu Dhabi", "Al Nahyan & Al Mamoura", 24.4600, 54.3800, 4, 2),
    Area("Abu Dhabi", "Mussafah", 24.3500, 54.5000, 8, 2),
    Area("Abu Dhabi", "Al Shamkha", 24.3400, 54.7200, 10, 2),
    Area("Abu Dhabi", "Al Ain", 24.2075, 55.7447, 20, 2),
]

# --- Northern Emirates ---------------------------------------------------

NORTHERN = [
    Area("Sharjah", "Al Majaz & Al Qasimia", 25.3300, 55.3850, 5, 2),
    Area("Sharjah", "Al Nahda Sharjah", 25.3050, 55.3700, 4, 2),
    Area("Sharjah", "Muwaileh & University City", 25.2900, 55.4700, 7, 2),
    Area("Sharjah", "Al Khan & Corniche", 25.3350, 55.3600, 4, 2),
    Area("Ajman", "Ajman Corniche", 25.4100, 55.4400, 5, 2),
    Area("Ajman", "Al Nuaimiya & Al Rashidiya", 25.3900, 55.4600, 5, 2),
    Area("Ras Al Khaimah", "RAK City & Al Nakheel", 25.7850, 55.9450, 12, 3),
    Area("Fujairah", "Fujairah City", 25.1288, 56.3265, 15, 3),
    Area("Umm Al Quwain", "Umm Al Quwain City", 25.5641, 55.5553, 12, 3),
]

AREAS = DUBAI + ABU_DHABI + NORTHERN


@dataclass(frozen=True)
class Query:
    """A single discovery request: one circle crossed with one category set."""

    index: int
    area: Area
    set_name: str
    categories: list[str]

    @property
    def location_coordinate(self) -> str:
        """Format the circle as DataForSEO expects: ``lat,lng,radius``."""
        return f"{self.area.lat},{self.area.lng},{self.area.radius_km}"

    @property
    def label(self) -> str:
        return f"{self.area.emirate} / {self.area.name} / {self.set_name}"


def build_queries() -> list[Query]:
    """Build the full discovery grid.

    Every area is crossed with CORE, COSMETIC and SPECIALIST. Tier-1 areas
    additionally get the ADJACENT sweep, because multi-speciality centres
    that hide dentistry under "Medical center" cluster in exactly those
    high-income districts.

    Returns:
        The ordered list of queries to execute. Run them in order: the
        list is sorted so that the highest-value territory is discovered
        first, which matters if you stop early or run out of credit.
    """
    queries: list[Query] = []
    index = 1

    # Tier-1 areas first, so an interrupted run still covers the best ground.
    for tier in (1, 2, 3):
        for area in AREAS:
            if area.tier != tier:
                continue
            set_names = ["core", "cosmetic", "specialist"]
            if area.tier == 1:
                set_names.append("adjacent")
            for set_name in set_names:
                queries.append(
                    Query(
                        index=index,
                        area=area,
                        set_name=set_name,
                        categories=CATEGORY_SETS[set_name],
                    )
                )
                index += 1

    return queries


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    qs = build_queries()
    print(f"{len(qs)} queries across {len(AREAS)} areas\n")
    for q in qs:
        print(f"{q.index:3d}  {q.location_coordinate:28s}  {q.label}")
