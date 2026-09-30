# Stage 2 Methodology — Wildland-Urban Interface (WUI) Exposure Layer

Stage 2 adds a scientifically defensible, **time-varying WUI exposure layer** to
the validated Stage 1 MTBS wildfire × county database. It produces:

1. County-level WUI metrics for four decadal vintages (SILVIS, §2).
2. A true geometric fire-perimeter × WUI intersection, temporally aligned so
   each fire year is compared against the WUI map of its own decade (§4).
3. County-level WUI change metrics 1990→2000→2010→2020 (§6).
4. An independent 2020 WUI product (Schug et al. 2023, *Nature*) used **only**
   as a modern comparison/sensitivity layer (§7).
5. The same framework applied to U.S. Census Places (§8).

**Terminology guardrails.** Nothing in Stage 2 is a "risk" model. The outputs
separate: **hazard/history** (MTBS wildfire occurrence and burned area),
**exposure** (fire overlap with WUI), and **WUI development** (where housing
and wildland vegetation co-occur). No causal interpretation is made anywhere:
WUI growth is reported as land-cover/housing change, not as a cause of
wildfire exposure, and correlation between WUI and fire is never stated as
causation. Predictive modeling is out of scope for Stage 2.

---

## 1. Datasets

All source URLs, versions, and retrieval metadata are recorded in
`config/datasets.yaml` (human) and `data/raw/provenance.json` (machine-readable,
written by `src/download_data.py` at download time). Summary:

| Property | SILVIS WUI Change v4 | Schug et al. 2023 (Nature) | Census TIGER/Line Places |
|---|---|---|---|
| Provider | SILVIS Lab, UW-Madison (also USDA FS RDS-2015-0012-4) | SILVIS Lab / Humboldt-Univ. Berlin, via Zenodo | U.S. Census Bureau |
| URL | `geoserver.silvis.forest.wisc.edu/geodata/wui_change_2020_v4/CONUS_WUI_block_1990_2020_change_v4_gcs_na83_GDB.zip` | `zenodo.org/api/records/7941460/files/NA.zip/content` (DOI 10.5281/zenodo.7941460) | `www2.census.gov/geo/tiger/TIGER2025/PLACE/tl_2025_{STATEFP}_place.zip` |
| Version / release | v4, released 2023-08-10 (SILVIS page updated Feb 2025) | v1.1 (2023-04-01); paper DOI 10.1038/s41586-023-06320-0 | TIGER 2025 (boundaries as of 2025-01-01) |
| Format | Zipped Esri File Geodatabase (polygon layer `CONUS_WUI_block_1990_2020_change_v4_gcs_na83` (name verified by inspection)) | Zipped 10 m GeoTIFF EQUI7 tiles (100 km × 100 km) + GDAL VRT | Zipped ESRI polygon shapefiles (49 state files) |
| CRS | NAD83 geographic (EPSG:4269) | EQUI7 North America, EPSG:27705 (azimuthal equidistant, not equal-area). Nominal 10 m pixels are 100 m² on the map; ground area uses the EPSG:5070 correction in §7 | NAD83 geographic (EPSG:4269) |
| Units of analysis | 2020 census blocks (uniform housing/land-cover areas) | 10 m pixels | Place polygons (STATEFP+PLACEFP) |
| Coverage | CONUS only (48 states + DC) | Global; NA.zip = North America; clipped to CONUS | CONUS states + DC |
| Temporal content | WUI classes for 1990, 2000, 2010, 2020; housing 1990–2020; vegetation from NLCD 1992/2001/2011/2019 | Approximately 2020 conditions | 2025 boundaries (see §8 limitation) |

Retrieval dates and SHA-256 hashes for the exact files used live in
`data/raw/provenance.json`; raw files are immutable (never modified in place;
corrupt downloads are quarantined as `*.corrupt` and re-fetched).

### SILVIS raw schema (verified from the archive, not assumed)

Inspection is performed by `src/preprocess_wui.py::inspect_schema`, which
prints and writes the actual fields to `outputs/tables/schema_silvis_raw.json`
before any processing. The FGDC metadata shipped in the archive
(`data/raw/silvis_wui/CONUS_WUI_block_1990_2020_change_v4_FGDC_MetadataUpdate.xml`)
documents, per vintage year `Y ∈ {1990, 2000, 2010, 2020}`:

- `WUIFLAG{Y}` — 1 = intermix WUI, 2 = interface WUI, 0 = not WUI.
- `WUICLASS{Y}` — class string (domain below).
- `HU{Y}` — housing units in the block (census counts decennial; SILVIS
  interpolates non-census years for intermediate products, which we do not use).
- `HUDEN{Y}` — housing units per km².
- `BLK20` — 2020 census block ID (block polygons are fixed 2020 geometry;
  vintage classes/housing are attributed onto those polygons).
- `fips` — 5-digit county FIPS (lowercase field name, verified by inspection); verified to equal `BLK20[:5]` so county
  assignment of blocks uses the dataset's own field, not an overlay.

**WUI class domain (exact strings from FGDC metadata):**
`Water`, `Low_Dens_Intermix`, `Med_Dens_Intermix`, `High_Dens_Intermix`,
`Low_Dens_Interface`, `Med_Dens_Interface`, `High_Dens_Interface`,
`Low_Dens_NoVeg`, `Med_Dens_NoVeg`, `High_Dens_NoVeg`, `Uninhabited_Veg`,
`Uninhabited_NoVeg`, `Very_Low_Dens_Veg`, `Very_Low_Dens_NoVeg`.

Validation check `sch-01` fails loudly if any observed class string falls
outside this domain.

### WUI definition (Federal Register 66 FR 751, 2001; mapped by Radeloff et al. 2005/2018)

- **Intermix WUI**: areas with **>50% wildland vegetation** and housing
  density **≥ 6.177635 housing units/km²** (1 unit per 40 acres).
- **Interface WUI**: areas with **≤50% vegetation** and housing density
  ≥ 6.177635 units/km² that lie **within 2.414 km (1.5 miles)** of a
  contiguous vegetation patch that is **≥75% wildland vegetation** and
  **≥500 ha** in size.
- Housing-density bands (Low/Med/High) appear in class names, but their
  band edges are **not documented in the shipped FGDC metadata**; our
  processing only requires WUI vs non-WUI and intermix vs interface, both
  unambiguous (`WUIFLAG{Y}` and the class suffix), so band edges are
  neither used nor asserted anywhere.
- Inputs per the FGDC process description: SILVIS allocated 1990, 2000,
  and 2010 housing units onto block geography using Census Block
  Relationship Files; vegetation shares come from NLCD 2001/2011/2019 and
  the NLCD 1992/2001 Retrofit Change Product (1992 values) via Tabulate
  Area.
- Citation: Radeloff, V.C., Helmers, D.P., Kramer, H.A., Mockrin, M.H.,
  Alexandre, P.M., Bar-Massada, A., … Stewart, S.I. (2018). Rapid growth of
  the US wildland-urban interface raises wildfire risk. *PNAS* 115(13).
  (Note: the *PNAS* title uses "risk" for its own framing; our outputs
  deliberately do not.)

**FGDC internal discrepancy (documented, no processing impact):** the
archive's process description states interface buffers of **"2,414 m
(1.5 miles)"** — consistent with FR 66:751 — while a later descriptive line
in the same metadata says **"2.14 km"**, an apparent typo in SILVIS's own
metadata. The pipeline consumes the delivered WUI classes as-is and never
recomputes buffers, so this does not affect any Stage 2 output; it is
recorded here per the no-unverified-facts rule.

---

## 2. County WUI metrics per vintage (`county_wui_by_vintage`)

`src/preprocess_wui.py` streams the FileGDB layer, reprojects block polygons
to **EPSG:5070 (NAD83 / Conus Albers, meters)** — never computing areas in
geographic coordinates — and builds:

- `data/processed/wui/silvis_wui_fragments.geoparquet` — one row per (block,
  county): `BLK20`, `GEOID`, `fragment_area_km2`, `wui_class_{Y}` (NULL where
  the block is not WUI that vintage), `hu_{Y}` (block housing units allocated
  to the fragment by area share), geometry (EPSG:5070).
- `data/processed/county_wui_by_vintage.parquet` (+ CSV in
  `outputs/tables/`) — one row per **GEOID × WUI vintage**:
  `wui_area_km2`, `pct_county_wui`, `interface_wui_area_km2`,
  `intermix_wui_area_km2`, `pct_wui_interface`, `pct_wui_intermix` (shares of
  the **county** area, so interface% + intermix% = pct_county_wui),
  `pct_of_wui_that_is_interface/intermix`, `hu_in_wui` (housing units inside
  WUI blocks), `hu_total_county`, `pct_county_hu_in_wui`, `county_area_km2`.

Because blocks nest within counties, county assignment is by the dataset's
own `FIPS` field (verified against `BLK20[:5]`), avoiding any spatial-overlay
CRS risk at this step. Housing-unit allocation to fragments is by fragment
area share within the block (area-weighted; block HU totals are preserved
exactly — regression-tested).

Counties with no WUI in a vintage are explicitly zero-filled (present in all
vintages), so absence is distinguishable from missingness.

---

## 3. Temporal alignment — the central design decision

The WUI is **time-varying**. Each SILVIS decadal snapshot is used as a
period-specific representation of WUI conditions:

| SILVIS snapshot | Represents MTBS fire years |
|---|---|
| 1990 | 1990–1999 |
| 2000 | 2000–2009 |
| 2010 | 2010–2019 |
| 2020 | 2020–2024 (2024 = last stable MTBS year) |

- Implemented in `src/build_wui_fire.py::assign_vintage`; the mapping lives in
  `config/analysis.yaml stage2.vintage_periods` and is the single source of
  truth (reused by every Stage 2 module).
- **No interpolation** of WUI classes between decades is performed; the
  snapshots are used as-is because no authoritative time-continuous WUI
  product exists.
- **MTBS fire years 1984–1989** precede the earliest snapshot. Fire history
  is retained for those years, but temporally aligned WUI exposure is marked
  unavailable: `assigned_wui_vintage` is null and
  `assigned_wui_vintage_label = "pre_1990_no_wui_baseline"`, with NULL
  `wui_area_km2` / `wui_burned_area_km2`. The integer vintage column stays
  a single nullable-integer dtype (1990/2000/2010/2020 or null) so the
  county-year table can be written to Parquet. **1990 WUI is never backfilled**
  into 1984–1989 in the primary metric. A backcast sensitivity analysis using
  the 1990 map for 1984–1989 may be added later only if clearly labeled a
  backcast approximation (not implemented in Stage 2).
- The 2020 snapshot's period ends in 2024 by configuration, because MTBS
  years after 2024 are not yet complete.
- Validation checks `tmp-01…04` enforce: assigned vintage matches fire year;
  **no 2020 WUI is applied to any year before 2020**; 1984–1989 rows carry the
  pre-baseline label with null WUI exposure; and each row's `wui_area_km2`
  equals the vintage-matched county WUI area.

---

## 4. Fire × WUI × county intersection

`src/build_wui_fire.py::intersect_fires_wui` performs a **true geometric
intersection** of each MTBS fire perimeter with each WUI block fragment
(EPSG:5070, spatial index), writing
`data/interim/fires_wui_intersections.geoparquet`: one row per
(fire, WUI fragment) pair with `fire_id`, `year`, county `GEOID`, `BLK20`,
`fragment_area_km2`, `wui_class_{Y}`, `intersection_area_km2`, geometry.
Intersections smaller than `min_intersection_area_km2` are dropped as
slivers. **WUI burned area is measured as actual fire perimeter ∩ WUI
geometry — never as burned area × county WUI percentage.**

From the pieces, per **county-year** (`county_year_wui_fire`):

- `burned_area_km2` — total wildfire burned area (Stage 1, concept A)
- `wui_burned_area_km2` — burned area inside WUI (concept B), split into
  `interface_wui_burned_km2` + `intermix_wui_burned_km2` by the vintage class
- `pct_wui_burned_in_year` = wui_burned / vintage WUI area × 100 (concept C)
- `pct_burned_area_that_was_wui` = wui_burned / total burned × 100 (concept D)
- `wui_area_km2` — the vintage-matched county WUI area (concept C denominator)
- `fire_count` — **distinct FIRE_ID** count (never geometry fragments)
- `assigned_wui_vintage` — nullable integer snapshot year per §3 (null for 1984–1989)
- `assigned_wui_vintage_label` — `"1990"`/`"2000"`/`"2010"`/`"2020"` or `"pre_1990_no_wui_baseline"`

Concepts A–D answer different scientific questions (how much burned overall;
how much of the burning hit WUI; what share of the WUI burned; what share of
the fire footprint was WUI) and are kept as separate columns.

The **county-period** summary (`county_period_wui_fire`) aggregates the same
pieces over the four periods (1990–1999, 2000–2009, 2010–2019, 2020–2024) and
adds `unique_wui_burned_footprint_km2` — the geometric union of WUI burn
footprints within the period — because cumulative per-event burned area can
legitimately exceed the unique footprint when land reburns (MTBS perimeter
overlap across incidents is expected; Stage 1 documented the same effect for
total burned area). Validation enforces
`unique_wui_burned_footprint_km2 ≤ wui_area_km2` and
`interface + intermix = WUI` for burned areas.

**Known caveat (documented, not corrected):** MTBS perimeters are incident
footprints; overlapping perimeters of distinct incidents can double-count
area, and unburned islands inside perimeters are included in the footprint.
This is inherent to the MTBS product and was inherited from validated Stage 1.

---

## 5. County-year table construction details

`build_county_year_wui_fire` starts from Stage 1 `county_year_wildfire`, which
contains only county-years with at least one qualifying MTBS wildfire (9,671
rows). It does not contain structural zeros for county-years with no mapped
fire. A later model needs an explicit complete panel; do not treat a missing
row as a stored zero, and do not turn 1984–1989 WUI nulls into zeros.

It left-joins:

1. vintage-matched `wui_area_km2` from `county_wui_by_vintage` (via
   `assign_vintage(year)`),
2. WUI burned area summed from intersection pieces by (GEOID, year).

Pre-1990 county-years get null WUI columns and the pre-baseline label. Annual
`wui_burned_area_km2` is only non-null where a vintage exists. Validation
requires that annual quantity to be ≤ total burned area (wf-01) and ≤ the
vintage WUI area (wf-02). It does **not** require total burned area ≤ WUI
area: fires burn non-WUI land. Summing annual WUI burn across years is
cumulative exposure (reburn counts again) and is not a percent of unique
coverage. The within-period unique footprint is a separate column.

---

## 6. WUI change metrics (`county_wui_change_1990_2020.csv`)

`src/wui_change.py` computes, per county, for transitions 1990→2000,
2000→2010, 2010→2020, and 1990→2020: absolute and percentage change in WUI
area, interface area, intermix area, and housing units in WUI. Percentage
change is **NaN where the base is 0** (never infinity or a fabricated
growth-from-zero number). Change components reconcile to endpoints
(interface change + intermix change = total WUI change; check `chg-01`).

Interpretation: these describe **WUI development** (housing growth into/next
to vegetation). They are *not* claims about wildfire exposure causation; any
linkage of WUI change to fire outcomes would require a causal design that
Stage 2 deliberately does not attempt.

---

## 7. Nature 2020 sensitivity layer (Schug et al. 2023)

`src/nature_wui.py` processes the North America 10 m raster product as an
**independent, modern (~2020) WUI product** for comparison against SILVIS
2020 only. It is **never applied to historic fire years**.

- Tiles are read via GDAL's `/vsizip` virtual filesystem from the raw
  archive (no full extraction), mosaicked through the shipped VRT, and
  processed **tile-by-tile** (windowed/chunked; the full raster is never
  loaded into RAM — the mosaic is ~1.5 TB uncompressed, far beyond available
  memory).
- For each tile, counties are rasterized to the tile grid and a joint
  (county × class) histogram accumulates pixel counts; progress checkpoints
  to `data/interim/nature_county_class_pixels.json` every 25 tiles so the step
  is resumable.
- Areas start as **pixel counts × the nominal 10 m × 10 m cell** (100 m²
  on the EQUI7 map). EPSG:27705 is azimuthal equidistant, centered at 52°N,
  97.5°W, so that nominal cell is not exact ground area. Across CONUS the
  county polygon's area in EPSG:5070 divided by its area in EPSG:27705 is
  about 0.956–0.999 (median 0.985): nominal pixels overstate ground area.
  `nature_*_area_km2` keeps the nominal measurement.
  `nature_*_ground_km2` multiplies it by that county-level scale
  (`equi7_ground_over_nominal`). Nominal values are not clipped.
- County output `county_nature_wui_2020.{parquet,csv}`:
  `nature_wui_area_km2`, `nature_interface/intermix_wui_area_km2`
  (classes: intermix = 1,3; interface = 2,4), per-class areas for all 8
  classes, `nature_classified_area_km2`, the ground-corrected companions
  above, `pct_county_nature_wui`,
  `pct_of_nature_wui_that_is_interface/intermix`,
  `pct_county_covered_by_nature_raster`.

Class scheme (from the Zenodo record / paper): 1 forest/shrubland/wetland
**intermix** WUI; 2 forest/shrubland/wetland **interface** WUI; 3 grassland
**intermix** WUI; 4 grassland **interface** WUI; 5 non-WUI forest/shrub/
wetland; 6 non-WUI grassland; 7 non-WUI urban; 8 non-WUI other.

**Definition differences vs SILVIS (methodological, not errors):**
- Nature defines WUI from **building footprints** (built-up pixels) and
  land cover; SILVIS uses **housing-unit density** from census blocks.
- Nature uses a **global** land-cover and settlement layer (coarser inputs
  than NLCD+census); SILVIS is tuned to U.S. data.
- Nature's vegetation edge distance classes differ from the FR
  66:751 interface rule; SILVIS implements the Federal Register definition.
- Nature class 8 ("other") includes areas not classified by its inputs; a
  county's `pct_county_covered_by_nature_raster` < 100% flags cells that are
  water/unclassified.
- Spatial support differs: 10 m pixels vs census-block polygons, so along
  WUI boundaries the two products cannot agree exactly even in principle.

Because of these differences the comparison (`src/compare_wui.py` →
`county_wui_silvis_vs_nature_2020.{parquet,csv}` +
`wui_silvis_vs_nature_summary.json`) reports **both products symmetrically;
neither is treated as ground truth**. It computes per-county WUI areas and
percentages under each product, absolute and percentage differences,
interface-share differences, a WUI presence/absence disagreement flag,
Pearson and Spearman correlations (all counties and the well-covered
subset), and CONUS totals. The summary JSON carries an explicit
methodological note; check `cmp-01` fails if that note is missing.

---

## 8. Census Places analysis (Stage 2C)

`src/places_wui.py` merges the 49 TIGER 2025 state PLACE files
(48 CONUS states + DC), preserving `STATEFP`, `PLACEFP`, `GEOID`
(STATEFP+PLACEFP, 7 chars), `NAME`, `NAMELSAD`, `LSAD`, `FUNCSTAT`, and
computes place area in EPSG:5070. Outputs (all keyed by place GEOID, **kept
strictly separate from county outputs** — check `plc-04` fails if any
5-digit county GEOID appears in a place table):

- `place_wui_by_vintage` — WUI area/composition per place × vintage
  (all four SILVIS vintages; true geometric intersection, batched over
  fragment GeoParquet to bound memory).
- `place_wui_2020` — the 2020 slice.
- `place_historical_fire_summary` — place × year MTBS summary (distinct
  FIRE_ID counts + true intersection burned area; only place-years with
  fires).
- `place_wui_fire_exposure` — place × year vintage-matched WUI burned area
  and percentages, with 1984–1989 marked `pre_1990_no_wui_baseline` and
  null exposure, identical logic to the county workflow.

**Conceptual caveats (documented prominently):**
- Census Places cover only **incorporated places and CDPs** — they do not
  tile U.S. land area, and unincorporated communities are absent. Place
  totals are **not** comparable to county totals and must never be summed
  into one table.
- **Boundary-vintage limitation (important):** TIGER 2025 place boundaries
  are 2025 geometry. SILVIS WUI vintages and MTBS fire years are attributed
  onto 2025 place footprints; places that annexed or de-annexed land between
  decades are represented by their 2025 extent in every vintage. The same
  limitation applies to counties (TIGER 2025 boundaries) but is more
  material for places, whose boundaries change more frequently. Historical
  place-level WUI/fire numbers are therefore **period-aligned
  approximations on fixed modern footprints**, labeled as such.
- WUI burned area within a place is clipped to the place polygon (pieces are
  intersected with places), so `wui_burned_area_km2 ≤ burned_area_km2`
  per place-year (check hierarchy `plc-03`).

---

## 9. Validation

`src/validate_stage2.py` runs the Stage 2 check suite (IDs
`sch-*`, `wui-*`, `tmp-*`, `wf-*`, `chg-*`, `nat-*`, `cmp-*`, `plc-*`,
`geo-05`, `prv-*`) and **appends** Stage 2 rows to the Stage 1
`outputs/tables/data_validation_report.csv`, rewriting
`validation_summary.md`. It exits non-zero on any CRITICAL failure, and
`run_pipeline.py` fails loudly in turn. Areas are compared with the
tolerances in `config/analysis.yaml stage2.validation` (boundary/pixel
effects), CRS is verified from GeoParquet metadata without loading
geometries (`geo-05`), and Stage 2 raw archives must have provenance records
marked archive-validated (`prv-02`).

Automated tests: `tests/test_stage2_wui.py` covers every pure transformation
on synthetic data — temporal alignment (including "no 2020 vintage before
2020" regressions), housing allocation, county metrics per vintage, the
county-year/county-period builders, change-table math, the Nature county
table, the SILVIS-vs-Nature comparison, place builders, all validation
checks (pass and fail paths), and downloader filename derivation for
Zenodo-style `/content` URLs.

---

## 10. Known limitations and assumptions

1. SILVIS is CONUS-only; Alaska/Hawaii/PR fires (excluded from Stage 1
   CONUS scope anyway) have no WUI layer.
2. WUI snapshots are decadal; within-decade WUI change (e.g., 2005 vs
   2000) is not represented (no interpolation — §3).
3. SILVIS block geometry is 2020 census blocks with vintage attributes; the
   1990/2000/2010 classes are assigned to 2020 block polygons by SILVIS, an
   unavoidable feature of the change product (they harmonized vintage data
   onto 2020 blocks so change is measurable on fixed geometry).
4. MTBS covers only **large fires** (roughly ≥405 ha western / ≥202 ha
   eastern U.S.); WUI exposure metrics therefore describe large-fire
   exposure, not all-fire exposure.
5. MTBS perimeter overlap/unburned islands (§4 caveat).
6. Nature 2020 raster coverage/class-8 caveats (§7); counties with
   incomplete raster coverage are flagged, not silently summed.
7. Place boundary-vintage limitation (§8).
8. TIGER 2025 county/place boundaries used across all vintages and fire
   years (fixed modern footprints); county boundary changes since 1990 are
   not represented. Stage 1 documented this for counties; Stage 2 inherits
   it for WUI intersection (blocks are assigned to counties by the SILVIS
   dataset's own 2020-consistent FIPS field).

## 11. Data-integrity note (recovery incident)

During Stage 2 development the repository's `~/Documents` location was synced
by iCloud with "Optimize Mac Storage"; under disk pressure macOS evicted
cold files and several could not be re-materialized (read as zero-filled).
All corrupted Stage 1 parquet artifacts were deleted and regenerated, and
two corrupted raw archives were quarantined and re-downloaded. Raw-data
immutability rules were honored throughout (quarantine + re-download, never
in-place modification). **Recommendation: move this repository out of an
iCloud-synced Documents folder before Stage 3** to eliminate recurrence.