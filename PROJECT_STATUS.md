# PROJECT STATUS — wildfire-wui-capstone

_Last updated: 2026-09-30 (Stage 1 complete — all validation checks passing)_

## Current Stage

**Stage 1 COMPLETE — historical MTBS wildfire × U.S. county spatial database
built and validated.** No WUI integration, no environmental predictors, no
modeling (per project rules, modeling stays prohibited until spatial data
validation passes — it now has).

## Completed

- [x] Project repository structure (config/, src/, tests/, docs/, outputs/,
      data/ with raw|interim|processed tiers).
- [x] Authoritative sources identified, downloaded, checksummed, documented
      (see docs/data_sources.md and data/raw/provenance.json):
      MTBS national burned-area boundaries (USGS EROS/USFS) and 2025
      TIGER/Line national counties (U.S. Census Bureau).
- [x] Environment: Python 3.12 venv, pinned requirements.txt
      (geopandas 1.2.0, pandas 3.0.6, shapely 2.1.2, pyogrio 0.13.0,
      pyarrow 25.0.1, matplotlib 3.11.2, numpy 2.5.3, pytest 9.1.1).
- [x] Schema inspection reports saved
      (outputs/tables/schema_mtbs_raw.{txt,json},
      schema_counties_raw.{txt,json}).
- [x] Preprocessing: cleaned GeoParquet outputs; 51 invalid MTBS geometries
      repaired via make_valid (logged); counties needed no repair; county
      areas cross-checked against Census ALAND+AWATER (max diff 0.14 km²).
- [x] Fire×county geometric intersection in EPSG:5070
      (19,009 fragments; 3,024 fires span >1 county, max 9 counties).
- [x] County-year table (9,671 rows) + all-years county summary (3,109 rows,
      zero-filled) with unique dissolved burn footprint.
- [x] Validation suite: 25 automated checks, 24 PASS + 1 INFO, 0 critical
      failures, 0 warnings
      (outputs/tables/data_validation_report.csv, validation_summary.md).
- [x] QA maps: cumulative burned area, fire count, % unique footprint
      (outputs/figures/map_conus_*.png).
- [x] Documentation: README (reproduction), AGENTS.md (13 rules),
      docs/methodology.md, docs/data_dictionary.md, docs/data_sources.md.
- [x] Automated tests: 61 passed, 0 failed (pytest; includes regression test
      for a CRS-mismatch footprint bug found and fixed during the run).
- [x] One-command reproduction: `python run_pipeline.py` (or `make stage1`).

## In Progress

- Nothing for Stage 1. Awaiting direction to begin Stage 2.

## Validation Results

Full report: `outputs/tables/data_validation_report.csv` — run 2026-09-30.

- 25 checks, 0 critical failures, 0 warnings, 1 informational.
- Provenance manifest complete (URLs, retrieval dates, sha256, sizes).
- Counties: 3,109 CONUS rows, GEOID unique, areas 5.3–52,073 km² (in bounds),
  49/49 CONUS state FIPS present, all 38 Virginia independent cities present.
- Fires: 15,540 CONUS wildfires 1984–2024, FIRE_ID (=event_id) unique and
  non-missing, fire types only {Wildfire, Wildland Fire Use}.
- County-year: 9,671 rows, no duplicate GEOID×year, no negative areas,
  no annual pct>100 (max 72.2%), fire_count counts distinct FIRE_IDs.
- All-years: 3,109 rows, no duplicate GEOIDs, footprint ≤ cumulative
  everywhere, footprint>0 for all 1,408 counties with fires, footprint pct
  within [0,100] (max 78.98%).
- Reburn sanity: 3 counties have cumulative burned area > county area
  (max ratio 1.30) — legitimate reburn, expected, not clipped.
- Event accounting: raw 30,933 → type filter 16,983 → year filter 16,909 →
  CONUS 15,540 retained; 22 intersection slivers <1e-4 km² dropped.

## Problems / Blockers

None. Two data-quality observations (documented, not blockers):

1. MTBS `incid_type` includes 4,311 "Other" events not described in the FGDC
   metadata domain, concentrated 1984–2009 (1,898 in Florida). Excluded from
   the primary analysis per config; revisit if the research question needs
   them.
2. MTBS release years 2025 (69 events) and 2026 (21 events) are incomplete
   mapping years; analysis truncated to the stable 1984–2024 window.

## Key Decisions

1. **Authoritative MTBS source**: national burned-area-boundary composite
   shapefile `mtbs_perimeter_data.zip` from USGS EROS
   (`edcintl.cr.usgs.gov`), DOI 10.5066/P9IED7RZ, 30,933 polygons, quarterly
   in-place refresh (retrieved 2026-09-30).
2. **Authoritative county source**: 2025 TIGER/Line national county file
   (`tl_2025_us_county.zip`, boundaries as of 2025-01-01, released
   2025-09-23). TIGER 2026 not yet published (404 as of 2026-09-30).
3. **FIRE_ID convention**: this MTBS product has no FIRE_ID field (that key
   belongs to the MTBS fire-occurrence point product); `event_id` is 100%
   unique and is used as FIRE_ID. Documented in methodology + data dictionary.
4. **Fire types retained**: Wildfire + Wildland Fire Use; excluded Prescribed
   Fire and "Other". Prescribed burns are intentional management ignitions —
   out of scope for wildfire exposure.
5. **Years**: 1984–2024 (2024 the latest stable complete year; 2025–2026 are
   partial mapping years in the current release).
6. **Spatial scope**: CONUS (48 states + DC) for Stage 1 because EPSG:5070 is
   only valid for CONUS area measurement. Non-CONUS qualifying events
   (AK 1,378 / HI 29 / PR 7 by state prefix) preserved in
   data/interim/mtbs_qualifying_nonconus.geoparquet for a later
   jurisdiction-appropriate stage.
7. **County area** = ALAND + AWATER-derived full polygon area in EPSG:5070
   (includes water). Documented; denominator caveat noted in methodology.
8. **All area math in EPSG:5070**; the processed fires file stays in native
   NAD83 (EPSG:4269) and is reprojected on demand. Validated by check geo-02
   (area-math artifacts in 5070) and geo-04 (fires geographic).
9. **Cumulative vs footprint**: cumulative burned area sums perimeters per
   event (rebuilt land counts many times, can exceed county area); unique
   footprint dissolves all perimeters per county first (rebuilt land counts
   once, ≤ county area). Both are kept as distinct metrics.
10. **Bug found and fixed during validation**: footprint was silently ~0
    because fire unions (degrees, EPSG:4269) were intersected with counties
    (meters, EPSG:5070). Fixed by reprojecting inside
    `compute_footprint_by_county`; new CRITICAL check agg-09
    (footprint>0 wherever fires occurred) plus a pytest regression test now
    guard this permanently.

## Next Step

Stage 2 — integrate WUI data: SILVIS U.S. WUI 1990/2000/2010/2020 and the
Schug et al. (2023) global WUI layer as comparison, joined to the county
tables by spatial overlay, with jurisdiction-appropriate handling for
AK/HI/PR if non-CONUS scope is added.