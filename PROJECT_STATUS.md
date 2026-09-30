# PROJECT STATUS — wildfire-wui-capstone

_Last updated: 2026-09-30 (start of Stage 1 build)_

## Current Stage

**Stage 1 — Build and validate the historical MTBS wildfire × U.S. county
spatial database.** No WUI integration, no environmental predictors, no
modeling.

## Completed

- [x] Project repository structure created (config/, src/, tests/, docs/,
      outputs/, data/ with raw|interim|processed tiers).
- [x] Authoritative sources identified and verified live:
      MTBS national perimeter archive (USGS EROS host) and 2025 TIGER/Line
      national county file (U.S. Census Bureau).

## In Progress

- [ ] Environment setup (Python 3.12 venv, geospatial stack).
- [ ] Raw data download + archive validation + provenance manifest.
- [ ] Schema inspection of both datasets.
- [ ] Preprocessing (clean GeoParquet outputs).
- [ ] County fire intersection (EPSG:5070).
- [ ] County-year table + all-years county summary.
- [ ] Validation suite, report, and maps.
- [ ] Documentation (README, methodology, data dictionary, data sources).
- [ ] Automated tests passing.
- [ ] Initial git commits.

## Validation Results

_Not yet available — pipeline has not been run._

## Problems / Blockers

_None yet._

## Key Decisions

1. **Authoritative MTBS source**: national burned-area-boundary composite
   shapefile `mtbs_perimeter_data.zip` served from USGS EROS
   (`edcintl.cr.usgs.gov`), the distribution host for the MTBS program
   (program pages: mtbs.gov, burnseverity.cr.usgs.gov). Verified live via
   HTTP 200 on 2026-09-30.
2. **Authoritative county source**: 2025 TIGER/Line national county file
   (`tl_2025_us_county.zip`, boundaries as of 2025-01-01, released
   2025-09-23). TIGER 2026 is not yet published (404 as of 2026-09-30).
3. **Spatial scope**: Stage 1 restricted to CONUS (48 states + DC) because
   EPSG:5070 is only valid for area measurement in CONUS. Non-CONUS events
   preserved separately for a later stage with jurisdiction-appropriate CRSs.
4. **Python version**: 3.12 (widest wheel support for the geospatial stack).

## Next Step

Download raw data, inspect schemas, and record actual FIRE_TYPE values and
year completeness before finalizing preprocessing rules.