# Stage 1 Methodology — MTBS Wildfire × U.S. County Spatial Database

This document records every material methodological choice for Stage 1, with
justifications, so results are interpretable and reproducible. Companion
documents: [`data_sources.md`](data_sources.md) (what was downloaded, from
where) and [`data_dictionary.md`](data_dictionary.md) (output variables).

## 1. Source datasets

| Dataset | Vintage used | Key properties |
|---|---|---|
| MTBS Burned Area Boundaries (national) | downloaded 2026-09-30; contains fires 1984–2026 | 30,933 polygon perimeters; NAD83 geographic (EPSG:4269); event key `event_id`; acres `burnbndac` |
| Census TIGER/Line counties (national) | 2025 release (boundaries as of 2025-01-01) | 3,235 county equivalents incl. VA independent cities; GEOID key; NAD83 (EPSG:4269) |

## 2. Temporal scope: analysis years 1984–2024

The MTBS archive retrieved on 2026-09-30 contains records with ignition years
through 2026, but event counts for recent years are drastically incomplete:
**2024: 1,051 events; 2025: 69; 2026: 21** versus ~800–1,200 per completed
year. MTBS extended assessments for a fire year are completed during the
following growing season, so the archive is right-censored.

**Decision:** restrict all Stage 1 analysis to ignition years **1984–2024**
(`config/analysis.yaml: years`). 2024 is the last year with a full, stable
event count consistent with the historical distribution. Records with year
1984–2024 account for 30,843 of 30,933 events; 90 recent-year records
(2025–2026) are excluded and the counts are recorded in the filter report.

## 3. Fire-type inclusion rules

MTBS documents its fire type in `incid_type`. Observed values in this release
and their disposition:

| Observed `incid_type` | Count (all years) | Disposition | Rationale |
|---|---|---|---|
| Wildfire | 16,780 | **included** | Target category: "unplanned, unwanted wildland fire" (FGDC domain definition). |
| Wildland Fire Use | 203 | **included** | Naturally ignited fires managed for resource benefit; wildfires under current interagency terminology. Not including them would undercount true wildfire activity, especially pre-2010 western fires. |
| Prescribed Fire | 9,639 | **excluded** | Management ignitions, not wildfire exposure. This follows the research convention of separating wildfire from prescribed burning and the project instruction to exclude prescribed fire. |
| Other | 4,311 | **excluded** | Cannot be verified as wildfire. The value "Other" does not appear in the FGDC metadata's enumerated domain (which lists Unknown/Complex/Out-of-area response), so its meaning is undocumented. Observed pattern: concentrated in 1984–2009 (4,063 of 4,311) and in the Southeast (Florida alone: 1,898). Excluded from the primary set; counts and events preserved for later sensitivity analysis. |

The filter chain and full accounting (raw → fire-type → year → CONUS) are
written to `data/processed/mtbs_filter_report.json` and echoed in the
validation report.

## 4. Geographic scope: CONUS only (documented restriction)

Stage 1 is restricted to the **48 contiguous states + District of Columbia**
(3,109 county equivalents). Reasons:

1. Area measurement uses **EPSG:5070 (NAD83 / Conus Albers)**, an equal-area
   projection valid only for CONUS. Applying it to Alaska (spanning ~20° of
   high-latitude longitude), Hawaii, or Puerto Rico would distort areas.
2. Jurisdiction-appropriate equal-area projections exist (e.g., NAD83 /
   Alaska Albers for AK) but were out of scope for Stage 1; adopting them is a
   documented future step, and the qualifying non-CONUS wildfire events
   (1,378 AK + 29 HI + 7 PR events; 1,369 of them qualifying types/years) are
   preserved in `data/interim/mtbs_qualifying_nonconus.geoparquet`.

Non-CONUS fires are identified by the two-letter state abbreviation embedded
in `event_id` (producer-defined format), cross-checked against perimeter
centroid coordinates (AK centroids lon ≤ −140.7; HI lon ≤ −155.1; PR lon
≥ −67.1; CONUS centroids lon ≥ −124.4) — the separation is unambiguous.

## 5. Coordinate reference systems and area measurement

- Source data: NAD83 geographic (EPSG:4269). **No areas are computed in
  geographic coordinates.**
- All area math occurs after reprojection to **EPSG:5070 (meters)**; areas are
  converted to km² (×10⁻⁶).
- `county_area_km2` is the full TIGER county polygon area (land + inland
  coastal/Great Lakes water included in the legal boundary) projected to
  EPSG:5070. It is cross-checked against the published `ALAND + AWATER`
  attributes; small differences reflect TIGER line generalization and are
  logged, not "corrected".
- Consequence (documented caveat): for coastal and Great Lakes counties,
  `county_area_km2` exceeds land area, so burn percentages are conservative
  relative to a land-only denominator.

## 6. Geometric intersection procedure

A true geometric overlay — **not** centroid assignment:

1. Candidate pairs: spatial join (`intersects`) between fire perimeters and
   county polygons (both EPSG:5070, both validity-checked).
2. For each (fire, county) pair, the exact geometric intersection
   (`shapely.intersection`) is computed, and the fragment area measured in
   km².
3. Degenerate touch artifacts (boundary-only contacts yielding zero-area
   lines/points) and fragments below 10⁻⁴ km² (100 m²) are dropped as
   numerical noise — far below MTBS's 30-m mapping resolution.
4. Invalid geometries, if any, are repaired with `make_valid` **before**
   intersection, with repair counts logged; the validation suite fails the
   run if any invalid geometry survives into outputs.

A fire spanning county boundaries therefore contributes its true intersected
area to each county it touches. Multi-county fires are never counted as
multiple *events*: event counts are `COUNT(DISTINCT fire_id)`.

**Identifier note:** the MTBS perimeter product identifies events by
`event_id`; the `FIRE_ID` field belongs to the separate MTBS
fire-occurrence-point product and does not exist in this shapefile. The
pipeline defines `fire_id = event_id` (unique across all 30,933 records,
verified) and preserves the original `event_id` unchanged.

## 7. Aggregation

### County-year table

One row per `GEOID × year` **with at least one qualifying fire**:

- `fire_count` — distinct fires touching the county in that year.
- `total_burned_area_km2` — sum of intersected fragment areas.
- `county_area_km2` — full county area (constant per county).
- `pct_county_burned_in_year` — `total_burned_area_km2 / county_area_km2 × 100`.

### All-years county summary

One row per CONUS county (zero-filled where untouched by any qualifying fire):

- `number_of_fire_years`, `unique_fire_count`,
  `cumulative_burned_area_km2` (sum of intersected areas across all years).
- `unique_historical_burn_footprint_km2` — see §8.
- `pct_unique_historical_burn_footprint`.

## 8. Cumulative burned area vs. unique historical burn footprint

These measure different things and must never be conflated:

- **Cumulative burned area** sums perimeters event-by-event. Land that burned
  three times contributes **3×**. It can legitimately exceed county area —
  across 41 years, reburning makes `cumulative > county_area` normal.
- **Unique historical burn footprint** first **dissolves (unions) all fire
  perimeters touching a county**, then intersects that union with the county
  and measures the result. Repeatedly burned land contributes **once**. It is
  bounded above by county area (≤100%).

Both are reported; the footprint answers "what share of the county has ever
burned", the cumulative sum answers "how much burning has the county
accumulated". `unique footprint ≤ cumulative` always, and this is asserted in
validation.

Implementation constraint: the footprint union/intersection uses raw shapely
set operations, which are CRS-agnostic. Fire geometries are therefore
reprojected from their native storage CRS (NAD83, EPSG:4269) into the county
CRS (EPSG:5070) **before** the union — mixing degree-space and meter-space
coordinates silently yields ~0 km² footprints. Validation check `agg-09`
(footprint > 0 wherever fires occurred) guards against any regression, and a
pytest regression test reproduces the failure mode.

## 9. Annual burned area exceeding county area

`pct_county_burned_in_year > 100` is geometrically impossible for a single
perimeter but possible when **distinct fire perimeters in the same year
overlap each other** (MTBS maps some co-occurring/adjacent incidents with
overlapping or jointly-digitized perimeters; the `comment` field documents
some such cases). The validation suite therefore flags every county-year with
annual pct > 100 for investigation (status WARN, listed in the report) rather
than clipping — and treats *negative* percentages as critical failures.

## 10. Known limitations and caveats

1. **MTBS maps only large fires** — approximately ≥405 ha (1,000 ac) in the
   western U.S. and ≥202 ha (500 ac) in the eastern U.S. All Stage 1 counts
   are counts of *large* fires; small fires are systematically absent. A
   handful of mapped events fall slightly below nominal thresholds (minimum
   observed: 396 ac) — these are "undersized" mapped fires per the FGDC
   metadata and are retained.
2. **MTBS perimeters are burned-area boundaries**, not suppression perimeters
   from incident reports; they can differ substantially from ICS perimeters
   because they are derived from satellite change detection (dNBR) and
   analyst digitization.
3. **Fires omitted from MTBS**: where suitable imagery was unavailable or the
   burn was not discernible (clouds, smoke, shadows, short northern growing
   seasons).
4. **Right-censoring** after 2024 (see §2); 2024 itself could still gain a
   small number of late extended assessments in future MTBS releases.
5. **County boundaries are a single 2025 vintage** applied to 1984–2024 fires;
   counties that changed boundaries during the record (rare, e.g., additions
   since 1984 such as Broomfield CO, 2001) are represented by their 2025
   geometry for all years. This is a standard fixed-geography panel choice and
   is applied consistently.
6. **County areas include legal water area** (§5), diluting percentages in
   coastal/lakeside counties.
7. **Perimeter overlap across incidents** can inflate same-year percentages
   (§9) and, very slightly, the cumulative sum (perimeters of different
   incidents that overlap). The *unique footprint* metric is immune.
8. **"Wildland Fire Use"** records end in the early 2010s because the WFU
   category was discontinued in reporting; after that such fires are coded as
   Wildfire.
9. MTBS is a *mapping* program, not a fire-occurrence census: counts reflect
   mapped, qualifying incidents.

## 11. Reproducibility

`python run_pipeline.py` (or `make stage1`) executes download → preprocess →
overlay → aggregate → validate → maps, failing loudly on any critical
validation failure. Raw archives are immutable and checksummed
(`data/raw/provenance.json`); intermediate and final products are GeoParquet
(spatial) / Parquet+CSV (tabular). Because USGS refreshes the MTBS composite
archive quarterly *in place*, a later re-download may yield a slightly
different file (different SHA-256, more completed fires); research results
should cite the retrieval date and SHA-256 recorded here.