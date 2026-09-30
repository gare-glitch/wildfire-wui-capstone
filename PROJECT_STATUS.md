# PROJECT STATUS — wildfire-wui-capstone

_Last updated: 2026-09-30 (Stage 1 complete; Stage 2 WUI integration complete
and validated — 0 critical failures)_

## Current Stage

**Stage 2 COMPLETE — SILVIS WUI × fire × county integration, Nature 2020
sensitivity comparison, and Census Places, all validated.** Modeling remains
prohibited until a later stage (AGENTS.md #11); Stage 1 and Stage 2 spatial
validation have both passed.

## Completed

Stage 1 (unchanged; see Validation Results below):
- [x] MTBS × county spatial database, 25-check validation, QA maps.

Stage 2 (executed 2026-09-30; pipeline exit 0):
- [x] SILVIS GDB schema inspection + WUI fragments + county WUI by vintage
      (3,109 counties × 4 vintages = 12,436 rows).
- [x] Vintage-aligned fire × WUI × county intersection
      (9,671 county-year rows; 12,436 county-period rows).
- [x] County WUI change 1990→2000→2010→2020 (12,436 rows).
- [x] Schug et al. 2023 Nature raster zonal stats (3,109 county rows;
      929 CONUS-intersecting tiles processed of 1,180 bbox candidates /
      3,157 mosaic tiles).
- [x] SILVIS vs Nature 2020 comparison (3,109 rows; Pearson r=0.7898).
- [x] Census Places WUI / fire / exposure (31,540 places; 126,160
      place-vintage rows).
- [x] Stage 2 validation: 26 checks, 25 PASS + 1 WARN, 0 FAIL.
- [x] 7 Stage 2 QA figures in `outputs/figures/`.
- [x] pytest: 111 passed (includes VRT rasterXSize/rasterYSize regression).

## In Progress

- Nothing for Stage 2. Awaiting review before commit, then Stage 3 direction.

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

Stage 2 (`validate_stage2`, same report file, 2026-09-30 13:46):
- 26 checks, 25 PASS, 1 WARN (nat-02), 0 FAIL.
- SILVIS domains match documentation; Nature observed classes {0–8}.
- County WUI grid complete: 12,436 GEOID×vintage rows; max SILVIS
  pct_county_wui = 91.77%.
- Temporal alignment: 0 rows with 2020 WUI on pre-2020 fires; 898 pre-1990
  baseline rows have null WUI exposure.
- County-year WUI fire: 9,671 fire-positive rows (not a complete county × year
  panel). Annual WUI burn ≤ total burn and annual WUI burn ≤ vintage WUI area.
  Total burn is not required to be ≤ WUI area (1,876 county-years have more
  burn than WUI, which is non-WUI land).
- County-period: 4 periods 1990-99 … 2020-24; 0 footprint violations.
- Change table: 4 transitions, 3,109 counties, internally consistent.
- Nature vs county area: nominal pixel areas can exceed the legal polygon
  by the EQUI7 azimuthal-equidistant scale (7 Virginia independent cities,
  max 101.87%). Ground-corrected WUI / legal area is ≤ 99.98% everywhere.
  nat-02 WARN: 292 counties have nominal classified coverage < 90% of the
  legal polygon; that tracks Census water (r = −0.994). 14 remain < 90% of
  ALAND because raster value 0 covers land. See
  `outputs/tables/stage2_methodological_audit.md`.
- SILVIS vs Nature: CONUS WUI 749,991 vs 1,147,967 km²; Pearson r=0.7898;
  32 presence disagreements. Products are independent definitions.
- Places: 7-digit GEOIDs only; WUI ≤ place area; pre-baseline null exposure.

## Problems / Blockers

None blocking Stage 2 completion. Data-quality observations:

1. MTBS `incid_type` includes 4,311 "Other" events not described in the FGDC
   metadata domain, concentrated 1984–2009 (1,898 in Florida). Excluded from
   the primary analysis per config; revisit if the research question needs
   them.
2. MTBS release years 2025 (69 events) and 2026 (21 events) are incomplete
   mapping years; analysis truncated to the stable 1984–2024 window.
3. Nature nominal coverage is below 90% of the legal polygon in 292
   counties because that denominator includes water (correlation with water
   fraction −0.994). 14 counties remain below 90% of Census land area;
   their gap is raster value 0 on land (mostly coastal marsh, plus Davis
   County, UT and Day County, SD). Documented in the Stage 2 audit; not
   clipped.
4. Nature CONUS WUI area is ~53% larger than SILVIS 2020 (1.15M vs 0.75M
   km²) on nominal pixel area. The EQUI7 scale is only about 1.5% and does
   not explain that gap. The products use independent definitions.

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
11. **Nature WUI is a 2020-only sensitivity layer**, never applied to historic
    fire years. The NA mosaic VRT uses `rasterXSize`/`rasterYSize` (not
    `sizeX`/`sizeY`), `SrcRect`/`DstRect` `xOff`/`yOff`/`xSize`/`ySize`, and
    band `NoDataValue` 255. Parser regression tests lock these names.
12. **Large artifacts may live in `$WILDFIRE_WUI_CACHE`** (default
    `~/.cache/wildfire-wui-capstone`) or `$WILDFIRE_WUI_DATA_ROOT`; source
    never hardcodes `/Users/<username>/...`.

## Next Step

Scientific review of Stage 2 outputs (this report), then a commit. Stage 3
(environmental predictors) is next after that review. Modeling stays gated
on AGENTS.md #11.