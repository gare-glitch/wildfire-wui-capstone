# Stage 1 Data Dictionary

Every variable in every Stage 1 output, with types, units, and provenance.
Original source attributes are preserved unchanged in the cleaned artifacts;
derived analysis fields are marked. GEOID is the canonical county key;
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