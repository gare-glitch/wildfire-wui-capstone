# Data Sources & Provenance

Authoritative datasets used by the Stage 1 pipeline. The machine-readable
provenance record (URLs, retrieval timestamps, sizes, SHA-256 checksums,
archive members) lives in [`data/raw/provenance.json`](../data/raw/provenance.json)
and is rewritten automatically by `src/download_data.py` on every download.
**Never** re-point these at unofficial mirrors (AGENTS.md rule #4).

## 1. MTBS Burned Area Boundaries (national fire perimeter dataset)

| Field | Value |
|---|---|
| Product | *Monitoring Trends in Burn Severity Burned Areas Boundaries for 1984–2026* (vector polygon shapefile of all currently completed MTBS fires) |
| Provider | MTBS program — U.S. Geological Survey Earth Resources Observation and Science (EROS) Center & USDA Forest Service |
| Official DOI (in FGDC metadata) | <https://doi.org/10.5066/P9IED7RZ> (USGS ScienceBase) |
| Direct archive URL used | <https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/MTBS_Fire/data/composite_data/burned_area_extent_shapefile/mtbs_perimeter_data.zip> |
| Program/documentation pages | <https://mtbs.gov/product-descriptions>, <https://burnseverity.cr.usgs.gov/products/mtbs>, <https://burnseverity.cr.usgs.gov/direct-download> |
| Archive file name (as documented) | `mtbs_perimeter_data.zip` (layer `mtbs_perims_DD.shp`) |
| Downloaded (UTC) | 2026-09-30T05:30:00Z |
| Upstream last-modified (HTTP) | Wed, 30 Sep 2026 05:08:03 GMT (quarterly release cycle; refreshed in place) |
| Size / SHA-256 | 389,976,591 bytes / `4b331fcee0dc4908d481f38b1a6fca1b12f0ae250a60cac656ca7d2556e5515f` |
| Coverage | 1984–2026* (CONUS, Alaska, Hawaii, Puerto Rico); 30,933 fire perimeter polygons |
| CRS (verified from `.prj` and metadata) | NAD83 geographic (EPSG:4269) — **areas must never be computed in this CRS** |
| FGDC metadata | shipped inside the archive: `mtbs_perims_DD_metadata.xml` |
| Update cadence | Quarterly releases; the composite archive is refreshed in place, so re-downloads yield a *different* file (different SHA-256) whenever USGS posts a new quarter |

\* The archive's own year field extends to 2026, but 2025–2026 records are
incomplete at retrieval time (69 and 21 events vs ~1,000–1,200 in a completed
year). The pipeline therefore restricts analysis to **1984–2024**; see
`docs/methodology.md`.

### Field reference (as-read names; FGDC XML documents mixed-case labels)

`event_id` (unique event identifier: 2-letter state abbreviation + latitude/
longitude + ignition date, e.g. `TX2672909780020110102`), `irwinid`, `incid_name`
(`UNNAMED` when not identifiable), `incid_type`, `map_id`, `map_prog`, `asmnt_type`,
`burnbndac` (mapped acres), `burnbndlat`, `burnbndlon`, `ig_date`, `pre_id`,
`post_id`, `perim_id`, `dnbr_offst`, `dnbr_stddv`, `nodata_t`, `incgreen_t`,
`low_t`, `mod_t`, `high_t`, `comment`.

**Data-vs-metadata discrepancy (verified by inspection 2026-09-30):** the FGDC
metadata enumerates `Incid_Type` domain values Wildfire / Prescribed fire /
Wildland fire use / Unknown / Out of area response / Complex, but the actual
attribute values in this release are **Wildfire (16,780), Prescribed Fire
(9,639), Other (4,311), Wildland Fire Use (203)**. The pipeline filters on the
*observed* values. See `docs/methodology.md` for the inclusion rules.

## 2. U.S. Census Bureau TIGER/Line Counties (national file)

| Field | Value |
|---|---|
| Product | 2025 TIGER/Line Shapefile: U.S. Counties and County Equivalents (national) |
| Provider | U.S. Census Bureau |
| Direct archive URL used | <https://www2.census.gov/geo/tiger/TIGER2025/COUNTY/tl_2025_us_county.zip> |
| Documentation | <https://www.census.gov/geographies/mapping-files/2025/geo/tiger-line-file.html>, <https://www2.census.gov/geo/tiger/TIGER2025/COUNTY/> |
| Release date / boundaries as of | Released 2025-09-23; legal boundaries and names effective 2025-01-01 |
| Downloaded (UTC) | 2026-09-30T05:30:57Z |
| Size / SHA-256 | 83,989,800 bytes / `9c6e9d9076abce2670d1de255de3710c35ecca00a7005d88e012dec52d95f763` |
| Coverage | 3,235 county equivalents — all states, DC, PR, and territories; includes all 38 Virginia independent cities |
| CRS (verified) | NAD83 geographic (EPSG:4269) |
| Currency note | Verified 2026-09-30 that TIGER 2026 is not yet published (HTTP 404 at `TIGER2026/COUNTY/`); the 2025 vintage is the most recent available |

### Field reference (as-read names)

`STATEFP`, `COUNTYFP`, `COUNTYNS`, `GEOID` (STATEFP+COUNTYFP — canonical county
key), `GEOIDFQ`, `NAME`, `NAMELSAD` (legal name, e.g. "Fairfax County",
"Alexandria city"), `LSAD`, `CLASSFP`, `MTFCC`, `CSAFP`, `CBSAFP`, `METDIVFP`,
`FUNCSTAT`, `ALAND` (land m²), `AWATER` (water m²), `INTPTLAT`, `INTPTLON`.

## Verification performed at acquisition

- HTTP 200 with expected `Content-Type: application/zip` on both URLs.
- ZIP integrity: full CRC pass (`zipfile.testzip`) on every member.
- Expected member check: at least one `.shp` member per archive.
- SHA-256 recorded in `data/raw/provenance.json` for tamper detection on
  re-runs; a valid existing archive is never re-downloaded.
- MTBS feature count (30,933) matches the FGDC metadata `ptvctcnt` exactly.
- Schemas dumped to `outputs/tables/schema_mtbs_raw.{txt,json}` and
  `outputs/tables/schema_counties_raw.{txt,json}` before any processing.

## Planned (not yet acquired — later stages)

These are recorded for project planning only; **none are used in Stage 1**:

3. SILVIS U.S. WUI 1990/2000/2010/2020 (University of Wisconsin–Madison).
4. Schug et al. (2023, *Nature*) global WUI dataset.
5. Environmental predictors (LST, precipitation, soil moisture, vegetation,
   drought, topography) from official sources (e.g., NASA/NOAA/USDA products).
6. Census demographic/housing variables for WUI communities.