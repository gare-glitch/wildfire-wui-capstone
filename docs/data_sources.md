# Data Sources & Provenance

Authoritative datasets used by the Stage 1 and Stage 2 pipelines. The
machine-readable provenance record (URLs, retrieval timestamps, sizes, SHA-256
checksums, archive members) lives in
[`data/raw/provenance.json`](../data/raw/provenance.json)
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

- HTTP 200 with expected `Content-Type: application/zip` on every URL.
- ZIP integrity: full CRC pass (`zipfile.testzip`) on every member of every
  archive; corrupt archives are quarantined as `*.corrupt` and re-downloaded
  (never modified in place).
- Expected member check per archive (`.shp`, `.gdbtable`, `.tif`, `.xlsx`).
- SHA-256 recorded in `data/raw/provenance.json` for tamper detection on
  re-runs; a valid existing archive is never re-downloaded.
- MTBS feature count (30,933) matches the FGDC metadata `ptvctcnt` exactly.
- Schemas dumped to `outputs/tables/schema_*_raw.{txt,json}` before any
  processing (MTBS, counties, SILVIS, Nature, Places).

## 3. SILVIS U.S. WUI Change 1990–2020 (version 4) — Stage 2A

| Field | Value |
|---|---|
| Product | *U.S. Wildland-Urban Interface (WUI) Change 1990–2020* — CONUS block-level WUI classes for four decadal snapshots (also archived as USDA Forest Service Research Data Archive product RDS-2015-0012-4) |
| Provider | SILVIS Lab, Department of Forest & Wildlife Ecology, University of Wisconsin–Madison |
| Direct archive URL used | <https://geoserver.silvis.forest.wisc.edu/geodata/wui_change_2020_v4/CONUS_WUI_block_1990_2020_change_v4_gcs_na83_GDB.zip> |
| Documentation pages | <https://silvis.forest.wisc.edu/data/wui-change/>, <https://www.fs.usda.gov/rds/archive/products/RDS-2015-0012-4/_metadata_RDS-2015-0012-4.html>, FGDC HTML mirror of the same metadata |
| Version / release | v4, released 2023-08-10 (SILVIS page last updated February 2025); USDA FS archive DOI 10.2737/RDS-2015-0012-4 |
| Downloaded (UTC) | 2026-09-30T09:47:09Z (initial acquisition earlier same day; re-validated on every run) |
| Size / SHA-256 | 4,300,350,918 bytes / `0b3d1bfabd80fdc75a3e776c26f8708ac5a55ab43b7f87b081d69e413fe447d4` |
| Format | Zipped Esri File Geodatabase; polygon layer `CONUS_WUI_block_1990_2020_change_v4_gcs_na83` (name verified by inspection) |
| CRS (verified from the GDB) | NAD83 geographic (EPSG:4269); the embedded `Shape_Area` field is in square degrees and is unusable for area math — all areas are recomputed in EPSG:5070 |
| Coverage | CONUS only (48 states + DC): 2020 census blocks (~8.7 million) as uniform housing/land-cover areas |
| Temporal content | WUI classes for 1990, 2000, 2010, 2020; housing units/density 1990–2020; vegetation shares from NLCD 1992 (Retrofit), 2001, 2011, 2019 (verified from the FGDC process description) |
| WUI definition | Federal Register 66 FR 751 (2001), mapped per Radeloff et al. (2005; 2018 *PNAS* 115(13)): intermix = >50% wildland vegetation and ≥6.177635 housing units/km²; interface = ≤50% vegetation at ≥6.177635 units/km² within 2.414 km (1.5 mi) of a ≥75%-vegetation patch ≥500 ha |
| Methodology citation | Radeloff, V.C., Helmers, D.P., Kramer, H.A., Mockrin, M.H., Alexandre, P.M., Bar-Massada, A., … Stewart, S.I. (2018). Rapid growth of the US wildland-urban interface raises wildfire risk. *PNAS* 115(13), 3311–3315 |

### Field reference (verified from the archive's FGDC metadata and by schema inspection — `outputs/tables/schema_silvis_raw.json`)

Per vintage `Y ∈ {1990, 2000, 2010, 2020}`: `WUIFLAG{Y}` (1 = intermix,
2 = interface, 0 = not WUI), `WUICLASS{Y}` (class string; 14-value domain
enumerated in `config/analysis.yaml stage2.silvis.class_domain`), `HU{Y}`
(housing units), `HUDEN{Y}` (housing units/km²); plus `BLK20` (2020 census
block ID), `fips` (5-digit county FIPS, lowercase field name verified by inspection, equal to `BLK20[:5]`).

**FGDC internal discrepancy (verified by inspection 2026-09-30):** the process
description states interface buffers of "2,414 m (1.5 miles)" (consistent with
FR 66:751) while a later descriptive line says "2.14 km" — an apparent typo in
SILVIS's own metadata. The pipeline consumes delivered classes and never
recomputes buffers, so no output is affected. See
`docs/wui_methodology.md`.

## 4. Schug et al. (2023) Global WUI, North America tiles (version 1.1) — Stage 2B

| Field | Value |
|---|---|
| Product | *Map of the global wildland–urban interface* (companion data to Schug et al. 2023) — North America subset |
| Provider | SILVIS Lab (UW–Madison) / Humboldt-Universität Berlin, published on Zenodo |
| Record / DOI | <https://zenodo.org/records/7941460> (DOI 10.5281/zenodo.7941460; version 1.1, released 2023-04-01; version 1.0 DOI 10.5281/zenodo.7791980) |
| Paper | Schug, F., Frantz, D., van der Linden, S., Hostert, P. (2023). The global wildland–urban interface. *Nature* 621, 94–99. DOI 10.1038/s41586-023-06320-0 |
| Direct archive URL used | <https://zenodo.org/api/records/7941460/files/NA.zip/content> (saved as `NA.zip`; Zenodo API URLs end in `/content` — the downloader records the documented file name separately) |
| Auxiliary archive | `tabular_script_meta_data.zip` (paper result tables `.xlsx` + scripts; member-checked, not used in processing) |
| Downloaded (UTC) | 2026-09-30T09:47:28Z |
| Size / SHA-256 (NA.zip) | 4,066,844,705 bytes / `63a13e12c4fcf415c9c42fc5ed27848f80f6af87bf8b0c0b3f22baa035077f28` |
| Format | Zipped GeoTIFF EQUI7 tiles (100 km × 100 km, 10 m resolution, 8 discrete classes) + GDAL VRT mosaic (`NA.vrt`); ~3,161 tiles in the NA subset |
| Projection | EQUI7 equal-area system (per-tile; equal-area pixels mean pixel counts × pixel area give exact areas — no resampling) |
| Coverage | North America; analysis clips to CONUS via tile-level processing (`/vsizip` reads; the full mosaic is never loaded into RAM) |
| Temporal content | Approximately 2020 conditions (built from ~2020 building-footprint and land-cover inputs) — **used only as a 2020 comparison/sensitivity product, never applied to historic years** |
| Class scheme (documented at the record/paper) | 1 forest/shrubland/wetland intermix WUI; 2 forest/shrubland/wetland interface WUI; 3 grassland intermix WUI; 4 grassland interface WUI; 5 non-WUI forest/shrub/wetland; 6 non-WUI grassland; 7 non-WUI urban; 8 non-WUI other |
| Definition difference vs SILVIS | building-footprint + land-cover based (global inputs) vs census housing density + NLCD vegetation (U.S.-tuned, FR 66:751 definition) — see `docs/wui_methodology.md` §7 |

## 5. U.S. Census Bureau TIGER/Line Places (state files) — Stage 2C

| Field | Value |
|---|---|
| Product | 2025 TIGER/Line Shapefiles: Places (state files, CONUS subset: 48 states + DC) |
| Provider | U.S. Census Bureau |
| URL pattern used | <https://www2.census.gov/geo/tiger/TIGER2025/PLACE/tl_2025_{STATEFP}_place.zip> (TIGER 2025 has no national PLACE file) |
| Documentation | <https://www.census.gov/geographies/mapping-files/2025/geo/tiger-line-file.html>, <https://www2.census.gov/geo/tiger/TIGER2025/PLACE/> |
| Release / boundaries as of | Released 2025-09-23; boundaries and names effective 2025-01-01 |
| Downloaded (UTC) | 2026-09-30 (49/49 state files validated; six files failed on first attempt with transient TLS errors and were re-fetched successfully) |
| Coverage | Incorporated places and census designated places (CDPs) — **places do not tile U.S. land area**; place outputs are kept strictly separate from county outputs |
| Fields preserved | `STATEFP`, `PLACEFP`, `GEOID` (STATEFP+PLACEFP, canonical place key), `NAME`, `NAMELSAD`, `LSAD`, `FUNCSTAT` (+ area computed in EPSG:5070) |
| CRS (verified) | NAD83 geographic (EPSG:4269) |

### Boundary-vintage limitation (documented prominently)

TIGER 2025 place boundaries are 2025 geometry. SILVIS WUI vintages and MTBS
fire years are attributed onto 2025 place footprints; places that annexed or
de-annexed land are represented by their 2025 extent in every vintage.
Historical place-level numbers are therefore period-aligned approximations on
fixed modern footprints. See `docs/wui_methodology.md` §8.

## Planned (not yet acquired — later stages)

These are recorded for project planning only; **none are used in Stages 1–2**:

6. Environmental predictors (LST, precipitation, soil moisture, vegetation,
   drought, topography) from official sources (e.g., NASA/NOAA/USDA products).
7. Census demographic/housing variables for WUI communities.
8. Structure-loss / vulnerability datasets (e.g., incident-level structure
   loss records).