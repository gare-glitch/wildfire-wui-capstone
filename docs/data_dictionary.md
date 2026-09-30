# Data Dictionary (Stages 1–2)

Every variable in every Stage 1 and Stage 2 output, with types, units, and
provenance. Original source attributes are preserved unchanged in the cleaned
artifacts; derived analysis fields are marked. GEOID is the canonical county
key (STATEFP+COUNTYFP, 5 chars); place GEOID (STATEFP+PLACEFP, 7 chars) is
the canonical place key and is never mixed into county tables;
`fire_id` (= MTBS `event_id`) is the canonical fire-event key.

## data/interim/mtbs_all_events.geoparquet

All 30,933 MTBS records (all fire types, all years, national coverage),
original CRS (EPSG:4269), original attributes unchanged plus derived fields.

| Column | Type | Description |
|---|---|---|
| `event_id` … `comment` | as source | All 22 original MTBS attributes, unchanged (`event_id`, `irwinid`, `incid_name`, `incid_type`, `map_id`, `map_prog`, `asmnt_type`, `burnbndac`, `burnbndlat`, `burnbndlon`, `ig_date`, `pre_id`, `post_id`, `perim_id`, `dnbr_offst`, `dnbr_stddv`, `nodata_t`, `incgreen_t`, `low_t`, `mod_t`, `high_t`, `comment`). `burnbndac` = mapped acres (source units); `burnbndlat`/`burnbndlon` are stored as strings in the source DBF and are preserved as-is. |
| `fire_id` | str | **Derived.** Canonical event key = `event_id`. |
| `fire_name` | str | **Derived.** = `incid_name` (`"UNNAMED"` preserved when source has no name). |
| `fire_type` | str | **Derived.** = `incid_type` (Wildfire / Prescribed Fire / Wildland Fire Use / Other). |
| `year` | int64 | **Derived.** Calendar year of `ig_date`; verified equal to the date embedded in `event_id` for all records. |
| `ignition_date` | date | **Derived.** Parsed `ig_date`. |
| `state_abbr_event_id` | str | **Derived.** Two-letter state abbreviation prefix of `event_id` (producer-defined format; used for the CONUS split). |
| `geometry` | Polygon | Burned-area boundary (EPSG:4269). |

## data/processed/mtbs_wildfires_conus.geoparquet

Analysis set: `fire_type ∈ {Wildfire, Wildland Fire Use}`, year 1984–2024,
CONUS only. Same columns as above. CRS EPSG:4269 (native; reprojected to
EPSG:5070 only inside the overlay stage).

## data/interim/mtbs_qualifying_nonconus.geoparquet

Qualifying wildfire events (same type/year filters) located in AK/HI/PR,
preserved for a future stage with jurisdiction-appropriate projections.
Same columns as `mtbs_all_events`.

## data/processed/mtbs_filter_report.json

Machine-readable accounting of every filtering step (counts before/after
geometry cleaning, by fire type, by excluded year, non-CONUS exclusions).

## data/processed/counties_conus.geoparquet

3,109 CONUS county equivalents, **EPSG:5070**, original TIGER attributes
unchanged plus derived fields:

| Column | Type | Description |
|---|---|---|
| `STATEFP`…`INTPTLON` | as source | Original TIGER/Line 2025 attributes (incl. `GEOID`, `NAME`, `NAMELSAD`, `ALAND`, `AWATER`). |
| `county_name` | str | **Derived.** = `NAMELSAD` legal name (e.g., "Fairfax County", "Alexandria city", "District of Columbia"). |
| `state_fips` | str | **Derived.** = `STATEFP` (2-digit, zero-padded). |
| `state_name` | str | **Derived.** State name from `config/analysis.yaml` standard FIPS mapping. |
| `county_area_km2` | float | **Derived.** Polygon area in EPSG:5070 ÷ 10⁶. Legal boundary area (land + included water), not land-only. |
| `geometry` | Polygon | County boundary (EPSG:5070). |

## data/interim/fires_county_intersections.geoparquet

One row per (fire, county) intersecting pair, **EPSG:5070**:

| Column | Type | Description |
|---|---|---|
| `GEOID` | str | County key (STATEFP+COUNTYFP). |
| `FIRE_ID` | str | Fire event key (= MTBS `event_id`). |
| `fire_name` | str | Fire name (`UNNAMED` where absent). |
| `fire_type` | str | Wildfire or Wildland Fire Use. |
| `year` | int64 | Ignition year. |
| `intersection_area_km2` | float | Geometrically intersected area of this fire within this county (km²), EPSG:5070. |
| `geometry` | Polygon/MultiPolygon | The intersected fragment (EPSG:5070). |

## data/processed/county_year_wildfire.parquet (+ .csv in outputs/tables/)

One row per `GEOID × year` with ≥1 qualifying fire (counties with no fire in
a year are absent, not zero-filled — the all-years summary covers all
counties):

| Column | Type | Description |
|---|---|---|
| `GEOID` | str | County key. |
| `year` | int64 | Fire year (1984–2024). |
| `fire_count` | int64 | Count of **distinct** fires (`FIRE_ID`) touching the county that year — never fragment counts. |
| `total_burned_area_km2` | float | Sum of intersected fragment areas that year. Can exceed county area only if distinct perimeters overlap (see methodology §9). |
| `county_name` | str | Legal county name. |
| `state_fips` | str | 2-digit state FIPS. |
| `state_name` | str | State name. |
| `county_area_km2` | float | Full county area (km²). |
| `pct_county_burned_in_year` | float | `total_burned_area_km2 / county_area_km2 × 100`. |

## data/processed/county_all_years_wildfire_summary.parquet (+ .csv)

One row per CONUS county (3,109 rows; zero-filled where no qualifying fire):

| Column | Type | Description |
|---|---|---|
| `GEOID` | str | County key. |
| `county_name` / `state_fips` / `state_name` | str | As above. |
| `number_of_fire_years` | int64 | Distinct years (1984–2024) with ≥1 qualifying fire. |
| `unique_fire_count` | int64 | Distinct fires over the full record. |
| `cumulative_burned_area_km2` | float | Sum of intersected areas across all years. **Can exceed county area** (reburning; intended). |
| `county_area_km2` | float | Full county area (km²). |
| `unique_historical_burn_footprint_km2` | float | Area of county covered by the **dissolved union** of all its historical fire perimeters (reburned land counted once). Bounded by county area. |
| `pct_unique_historical_burn_footprint` | float | Footprint ÷ county area × 100. In [0, 100] by construction. |

## outputs/tables/data_validation_report.csv

One row per validation check: `check_id`, `name`, `dataset`, `severity`
(CRITICAL/WARNING/INFO), `status` (PASS/FAIL/WARN/INFO), `details`, plus
`metric_*` columns with check-specific quantities. See
`validation_summary.md` for the human-readable rendering.

## outputs/tables/schema_mtbs_raw.{txt,json} and schema_counties_raw.{txt,json}

Schema snapshots (feature counts, CRS, geometry types, bounds, per-column
dtypes) taken before any processing, as required by AGENTS.md rule #2.

## outputs/figures/map_conus_*.png

QA choropleths (EPSG:5070): cumulative burned area, wildfire count, and
percent unique historical burn footprint per county. Gray = no qualifying
fire. Not publication graphics.

---

# Stage 2 Data Dictionary

All Stage 2 areas are computed in **EPSG:5070** (km²). Vintage semantics:
`wui_year`/`assigned_wui_vintage` refer to the SILVIS decadal snapshot
(1990/2000/2010/2020), never the fire year. `assigned_wui_vintage` is a
nullable integer. Fire years 1984–1989 store null in that column and
`assigned_wui_vintage_label = "pre_1990_no_wui_baseline"`, with null WUI
exposure. Aligned years store the snapshot year in both columns (integer,
and the same year as text).

## data/processed/wui/silvis_wui_fragments.geoparquet

One row per (2020 census block fragment, county), **EPSG:5070**. Blocks nest
within counties (verified `FIPS == BLK20[:5]`), so county assignment uses the
dataset's own FIPS field.

| Column | Type | Description |
|---|---|---|
| `BLK20` | str | 2020 census block ID (SILVIS). |
| `GEOID` | str | County key from the SILVIS `FIPS` field. |
| `fragment_area_km2` | float | Block-fragment area (km²), EPSG:5070. |
| `wui_class_{1990\|2000\|2010\|2020}` | str/nullable | SILVIS class string that vintage; **NULL where the block is not WUI that vintage**. |
| `hu_{1990\|2000\|2010\|2020}` | float | Block housing units allocated to this fragment by area share (block totals preserved exactly). |
| `geometry` | Polygon/MultiPolygon | Fragment geometry (EPSG:5070). |

## data/processed/county_wui_by_vintage.parquet (+ .csv in outputs/tables/)

One row per **GEOID × WUI vintage** (3,109 counties × 4 = 12,436 rows;
counties with no WUI are explicitly zero-filled):

| Column | Type | Description |
|---|---|---|
| `GEOID` | str | County key. |
| `county_name` / `state_fips` / `state_name` | str | As Stage 1. |
| `wui_year` | int64 | SILVIS snapshot year (1990/2000/2010/2020). |
| `county_area_km2` | float | County area (km², EPSG:5070; legal boundary, land + water). |
| `county_land_area_km2` | float | Census `ALAND / 1e6` (km²). Preferred denominator for WUI exposure. |
| `county_water_area_km2` | float | Census `AWATER / 1e6` (km²). |
| `wui_area_km2` | float | Area of WUI blocks (intermix + interface) that vintage. |
| `pct_county_wui` | float | `wui_area_km2 / county_area_km2 × 100` (legal polygon, land + water). |
| `pct_total_county_area_wui` | float | Alias of `pct_county_wui`. |
| `pct_county_land_area_wui` | float | `wui_area_km2 / county_land_area_km2 × 100`. |
| `interface_wui_area_km2` | float | Interface WUI area that vintage. |
| `intermix_wui_area_km2` | float | Intermix WUI area that vintage. |
| `pct_wui_interface` | float | Interface area ÷ **county** area × 100 (so `pct_wui_interface + pct_wui_intermix = pct_county_wui`). |
| `pct_wui_intermix` | float | Intermix area ÷ county area × 100. |
| `pct_of_wui_that_is_interface` | float | Interface share **of the county's WUI** × 100. NaN where WUI = 0. |
| `pct_of_wui_that_is_intermix` | float | Intermix share of the county's WUI × 100. NaN where WUI = 0. |
| `hu_in_wui` | float | Housing units inside WUI blocks that vintage. |
| `hu_total_county` | float | Total housing units in the county that vintage (SILVIS blocks; vintage-specific). |
| `pct_county_hu_in_wui` | float | `hu_in_wui / hu_total_county × 100`. NaN where no housing units. |

## data/interim/fires_wui_intersections.geoparquet

One row per (fire perimeter × WUI fragment) intersecting pair,
**EPSG:5070** — the true geometric intersection used for all WUI-burn
metrics (never burned area × county WUI percentage):

| Column | Type | Description |
|---|---|---|
| `fire_id` | str | Fire event key (= MTBS `event_id`). |
| `year` | int64 | Ignition year. |
| `GEOID` | str | County containing the fragment. |
| `BLK20` | str | WUI block fragment ID. |
| `fragment_area_km2` | float | WUI fragment area (km²). |
| `wui_class_{v}` | str/nullable | Fragment WUI class per vintage. |
| `intersection_area_km2` | float | Fire ∩ fragment area (km²). Slivers below `stage2.min_intersection_area_km2` dropped. |
| `geometry` | Polygon | The intersection piece (EPSG:5070). |

## data/processed/county_year_wui_fire.parquet (+ .csv in outputs/tables/)

One row per **GEOID × fire year** (all Stage 1 county-years, including
zero-fire county-years):

| Column | Type | Description |
|---|---|---|
| `GEOID`, `year`, `county_name`, `state_fips`, `state_name`, `county_area_km2`, `fire_count`, `burned_area_km2` | — | Stage 1 fire history (`burned_area_km2` = Stage 1 `total_burned_area_km2`; **concept A**). |
| `assigned_wui_vintage` | Int64 (nullable) | SILVIS snapshot matched to the fire year (1990→1990-99, 2000→2000-09, 2010→2010-19, 2020→2020-24). **Null for 1984–1989** (no snapshot to assign). | 
| `assigned_wui_vintage_label` | str | `"1990"` / `"2000"` / `"2010"` / `"2020"`, or `"pre_1990_no_wui_baseline"` for 1984–1989. The pre-baseline marker lives here so the integer vintage column stays one dtype. |
| `wui_area_km2` | float | Vintage-matched county WUI area. NaN for pre-1990 rows. |
| `wui_burned_area_km2` | float | **Concept B**: burned area inside WUI (sum of intersection pieces). NaN for pre-1990 rows; 0 where the vintage exists but no WUI burned. |
| `interface_wui_burned_km2` | float | WUI burned area on interface-class fragments. |
| `intermix_wui_burned_km2` | float | WUI burned area on intermix-class fragments. |
| `pct_wui_burned_in_year` | float | **Concept C**: `wui_burned_area_km2 / wui_area_km2 × 100` (share of the county's WUI that burned that year). |
| `pct_burned_area_that_was_wui` | float | **Concept D**: `wui_burned_area_km2 / burned_area_km2 × 100` (share of the fire footprint that was WUI). |

Concepts A–D answer different scientific questions and are never combined
into a single "risk" score. Pre-1990 rows keep full fire history with **null**
WUI exposure — never backfilled with 1990 WUI.

## data/processed/county_period_wui_fire.parquet (+ .csv in outputs/tables/)

One row per **GEOID × period** for the four SILVIS periods
(`1990-1999`, `2000-2009`, `2010-2019`, `2020-2024`):

| Column | Type | Description |
|---|---|---|
| `GEOID`, `county_name`, `state_fips`, `state_name`, `county_area_km2` | — | As Stage 1. |
| `period` | str | Period label, formatted from the configured span. |
| `assigned_wui_vintage` | Int64 | The period's SILVIS snapshot (non-null; every period has a snapshot). |
| `assigned_wui_vintage_label` | str | That snapshot year as text (`"1990"`, `"2000"`, `"2010"`, `"2020"`). |
| `n_fire_years` | int64 | Fire years in the period with ≥1 qualifying fire. |
| `distinct_fires` | int64 | **Distinct** FIRE_ID count over the period. |
| `burned_area_km2` | float | Total burned area over the period (per-event sums; reburn counted per event). |
| `wui_area_km2` | float | Vintage-matched county WUI area. |
| `wui_burned_area_km2` | float | Cumulative WUI burned area (per-event sums). |
| `unique_wui_burned_footprint_km2` | float | Area of the dissolved union of WUI burn pieces in the period (reburned land counted once); ≤ `wui_area_km2`. |
| `pct_wui_burned_footprint_in_period` | float | `unique_wui_burned_footprint_km2 / wui_area_km2 × 100`. |
| `pct_burned_area_that_was_wui` | float | Period aggregate of concept D. |
| `unique_interface_wui_burned_km2` / `unique_intermix_wui_burned_km2` | float | Unique-footprint split by WUI type. |

## data/processed/county_wui_change_1990_2020.csv (+ .parquet)

One row per **GEOID × transition** (transitions `1990->2000`, `2000->2010`,
`2010->2020`, `1990->2020`; 3,109 × 4 rows):

| Column | Type | Description |
|---|---|---|
| `GEOID`, `county_name`, `state_fips`, `state_name` | — | As Stage 1. |
| `transition` | str | `"base->end"` label. |
| `base_year` / `end_year` | int64 | Transition endpoints. |
| `wui_area_km2_old` / `wui_area_km2_new` | float | WUI area at endpoints. |
| `wui_area_change_km2` | float | Absolute change (end − base). |
| `pct_wui_area_change` | float | Percentage change; **NaN where base = 0** (never infinity). |
| `interface_wui_area_change_km2` / `intermix_wui_area_change_km2` | float | Component changes (sum to total change). |
| `pct_interface_area_change` / `pct_intermix_area_change` | float | Component percentage changes; NaN on zero base. |
| `hu_in_wui_change` | float | Change in housing units inside WUI. |
| `pct_hu_in_wui_change` | float | Percentage change in WUI housing units; NaN on zero base. |

These are **WUI development** metrics. They are not causal claims about
wildfire exposure.

## data/processed/county_nature_wui_2020.parquet (+ .csv in outputs/tables/)

One row per CONUS county — Schug et al. (2023) 10 m raster product,
**~2020 conditions only**:

| Column | Type | Description |
|---|---|---|
| `GEOID`, `county_name`, `state_fips`, `state_name`, `county_area_km2` | — | As Stage 1 (legal polygon). |
| `county_land_area_km2` / `county_water_area_km2` | float | Census ALAND / AWATER, km². |
| `nature_wui_area_km2` | float | Nominal WUI pixel area (classes 1–4). 10 m EQUI7 cells counted as 100 m² on the map, not ground area. |
| `nature_intermix_wui_area_km2` / `nature_interface_wui_area_km2` | float | Type split (nominal). |
| `nature_class{1..8}_*_km2` | float | Per-class nominal pixel areas. |
| `nature_classified_area_km2` | float | Sum of classes 1–8 (nominal). Value 0 is unclassified and is excluded. VRT NoData 255 does not occur on county-covered pixels. |
| `equi7_ground_over_nominal` | float | County polygon area in EPSG:5070 ÷ the same polygon in EPSG:27705. |
| `nature_wui_ground_km2` / `nature_classified_ground_km2` | float | Nominal area × `equi7_ground_over_nominal`. Not a clip of the nominal measurement. |
| `pct_county_nature_wui` | float | Nominal WUI ÷ legal county area × 100. Can exceed 100 where the azimuthal-equidistant pixel overstates ground area. |
| `pct_total_county_area_nature_wui` | float | Alias of `pct_county_nature_wui`. |
| `pct_county_land_area_nature_wui` | float | Nominal WUI ÷ Census land area × 100. |
| `pct_total_county_area_nature_wui_ground` | float | Ground-corrected WUI ÷ legal county area × 100. |
| `pct_county_land_area_nature_wui_ground` | float | Ground-corrected WUI ÷ Census land area × 100. Can exceed 100 when WUI pixels cover the county's water sliver and the denominator drops water. |
| `pct_of_nature_wui_that_is_interface` / `pct_of_nature_wui_that_is_intermix` | float | Composition shares. |
| `pct_county_covered_by_nature_raster` | float | Nominal classified area (classes 1–8) ÷ legal county area × 100. Low values track Census water plus raster value 0. |

## data/processed/county_wui_silvis_vs_nature_2020.parquet (+ .csv)

One row per CONUS county — 2020 sensitivity comparison; **neither product is
treated as correct**:

| Column | Type | Description |
|---|---|---|
| `GEOID`, `county_name`, `state_fips`, `state_name`, `county_area_km2` | — | As Stage 1. |
| `silvis_wui_area_km2` / `nature_wui_area_km2` | float | County WUI area under each product. |
| `pct_county_silvis_wui` / `pct_county_nature_wui` | float | WUI percentage under each. |
| `wui_area_diff_km2` | float | Nature − SILVIS. |
| `wui_pct_point_diff` | float | Percentage-point difference in county WUI share (Nature − SILVIS). |
| `wui_area_pct_diff` | float | Relative difference vs SILVIS base; NaN where SILVIS WUI = 0. |
| `pct_of_silvis_wui_that_is_interface` / `pct_of_nature_wui_that_is_interface` | float | Interface share of WUI under each. |
| `interface_share_pct_point_diff` | float | Share difference (Nature − SILVIS). |
| `wui_presence_disagreement` | bool | True where one product maps WUI and the other does not. |
| `pct_county_covered_by_nature_raster` | float | Raster coverage flag (see above). |

## data/processed/wui/places_conus.geoparquet

Merged TIGER 2025 places (49 state files), **EPSG:5070**: `STATEFP`,
`PLACEFP`, `GEOID` (7-char place key), `NAME`, `NAMELSAD`, `LSAD`, `FUNCSTAT`,
`place_area_km2` (legal polygon in EPSG:5070), `place_land_area_km2`
(`ALAND / 1e6`), `place_water_area_km2`, `geometry`.

## data/processed/wui/place_wui_by_vintage.parquet (+ .csv)

One row per **place × vintage** (all four vintages; same column semantics as
`county_wui_by_vintage` but with `place_area_km2` denominators,
`pct_place_wui` / `pct_total_place_area_wui` (legal polygon) and
`pct_place_land_area_wui` (Census land)). True geometric intersection of fragments with place
polygons.

## data/processed/wui/place_wui_2020.parquet (+ .csv)

The 2020-vintage slice of the above.

## data/processed/wui/place_historical_fire_summary.parquet (+ .csv)

One row per **place × fire year** with ≥1 qualifying fire (true geometric
intersection; distinct FIRE_ID counts; `burned_area_km2` clipped to the
place). Only place-years with fires — places without any fire are absent.

## data/processed/wui/place_wui_fire_exposure.parquet (+ .csv)

One row per **place × fire year** (base = fire summary rows): vintage-matched
WUI exposure with the same concept A–D semantics and pre-1990 null rules as
the county table, clipped to place polygons. `assigned_wui_vintage` (nullable
integer) and `assigned_wui_vintage_label` follow the county-year table.
**Never combined with county rows into one table** (places do not tile U.S.
land area).

## outputs/tables/schema_silvis_raw.{txt,json}, schema_nature_raw.{txt,json}, schema_places_raw.{txt,json}

Schema snapshots taken before any processing (AGENTS.md rule #2): SILVIS GDB
field names/dtypes/CRS/feature counts and per-vintage class/flag domains;
Nature raster dimensions/resolution/CRS/class domain; Places field
inventory across all 49 state files.

## outputs/tables/wui_silvis_vs_nature_summary.json

Comparison summary: CONUS totals under each product, Pearson + Spearman
correlations (all counties and the ≥95%-raster-coverage subset), mean/median
absolute differences, presence-disagreement counts, and an explicit
**methodological note** on why the products differ (no ground truth assumed).

## outputs/tables/places_runtimes.json

Runtime record for the Stage 2 heavy steps (fragments, intersections, raster
tiles, place intersections), for performance documentation.

## outputs/figures/wui_*.png

Stage 2 QA figures (research graphics, not publication): WUI% 1990, WUI%
2020, WUI change 1990–2020, historical WUI burned area, percent of WUI
historically burned (period-weighted), SILVIS-vs-Nature scatter (with
correlations annotated), and SILVIS-vs-Nature disagreement map.