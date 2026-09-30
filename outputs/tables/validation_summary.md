# Data Validation Summary (Stage 1 + Stage 2)

(Stage 1: 25 checks carried over from the previous validation run; see its log for details.)

## Stage 2 — WUI integration

- Checks run: 26
- PASS: 25
- WARN: 1 (investigated; see details)
- FAIL: 0

| Check | Dataset | Severity | Status | Details |
|---|---|---|---|---|
| prv-02 Stage 2 datasets recorded in provenance manifest | provenance | CRITICAL | PASS | silvis_wui, nature_wui, census_places recorded and validated |
| sch-01 SILVIS WUI class/flag domains match documentation | silvis_schema | CRITICAL | PASS | 56 observed class values across vintages; all within the documented domain |
| sch-02 Nature WUI raster classes within documented 1-8 domain | nature_schema | CRITICAL | PASS | observed [0, 1, 2, 3, 4, 5, 6, 7, 8]; outside domain: none |
| wui-01 County WUI areas non-negative | county_wui_by_vintage | CRITICAL | PASS | 0 negative area values across ['county_area_km2', 'wui_area_km2', 'interface_wui_area_km2', 'intermix_wui_area_km2', 'county_land_area_km2', 'county_water_area_km2'] |
| wui-02 County WUI area <= county area (tolerance) | county_wui_by_vintage | CRITICAL | PASS | 0 county-vintages exceed county area beyond tolerance; max pct_county_wui=91.77% |
| wui-03 County WUI percentages within [0, 100] | county_wui_by_vintage | CRITICAL | PASS | 0 negative; 0 above 100.0% across ['pct_county_wui', 'pct_wui_interface', 'pct_wui_intermix', 'pct_of_wui_that_is_interface', 'pct_of_wui_that_is_intermix', 'pct_county_hu_in_wui'] |
| wui-04 interface + intermix == WUI area (SILVIS) | county_wui_by_vintage | CRITICAL | PASS | 0 county-vintages where the split does not sum to the total; max residual 0.0000 km2 |
| wui-05 County WUI table: unique GEOID x vintage, complete grid | county_wui_by_vintage | CRITICAL | PASS | 12436 rows, 12436 unique (GEOID, vintage); 3109 counties x 4 vintages expected |
| tmp-01 Assigned WUI vintage matches fire year (period mapping) | county_year_wui_fire | CRITICAL | PASS | 0 rows whose assigned_wui_vintage disagrees with the configured period mapping |
| tmp-02 No 2020 WUI silently applied to pre-2020 fire years | county_year_wui_fire | CRITICAL | PASS | 0 rows with year<2020 assigned the 2020 vintage |
| tmp-03 1984-1989 rows are pre-WUI baseline with null exposure | county_year_wui_fire | CRITICAL | PASS | 898 pre-1990 rows: 0 mislabeled, 0 with non-null WUI values; 0 aligned rows missing vintage-matched WUI area |
| tmp-04 Row WUI area equals its assigned vintage's county WUI area | county_year_wui_fire | CRITICAL | PASS | 0 rows whose wui_area_km2 is not the vintage-matched value |
| wf-01 WUI burned area <= total burned area (county-year) | county_year_wui_fire | CRITICAL | PASS | 0 county-years where WUI burn exceeds total burn |
| wf-02 Annual WUI burned area <= county WUI area | county_year_wui_fire | CRITICAL | PASS | 0 county-years where annual WUI burn exceeds the vintage-matched WUI area |
| wf-03 interface + intermix burn == WUI burn (county-year) | county_year_wui_fire | CRITICAL | PASS | 0 rows where the interface/intermix split does not sum |
| wf-04 No duplicate GEOID x year rows | county_year_wui_fire | CRITICAL | PASS | 9671 unique county-years over 9671 rows |
| wf-05 County-period table: 4 periods, unique keys, footprint <= WUI | county_period_wui_fire | CRITICAL | PASS | periods ['1990-1999', '2000-2009', '2010-2019', '2020-2024'] (expected ['1990-1999', '2000-2009', '2010-2019', '2020-2024']); 12436 rows / 12436 unique; 0 footprint violations |
| chg-01 WUI change table: transitions complete, unique, internally consistent | county_wui_change | CRITICAL | PASS | transitions [(1990, 2000), (1990, 2020), (2000, 2010), (2010, 2020)]; 3109 counties; 0 duplicate keys; 0 endpoint mismatches; 0 composition mismatches |
| nat-01 Nature WUI areas: non-negative, <= county, composition sums | county_nature_wui_2020 | CRITICAL | PASS | 0 negative, 0 beyond county area (+5% raster slack), 0 composition violations |
| nat-02 Nature raster coverage per county (diagnostic) | county_nature_wui_2020 | WARNING | WARN | median coverage 100.1% of legal county area; 292 counties below 90%. Coverage tracks Census water; counties still below 90% of ALAND are listed in outputs/tables/nature_coverage_land_below_90.csv (raster value 0 on land, not a passed check). |
| cmp-01 SILVIS vs Nature: separate columns, finite correlations, distinct definitions | county_wui_silvis_vs_nature_2020 | CRITICAL | PASS | separate silvis_/nature_ columns present; Pearson r=0.7898, Spearman rho=0.8304 |
| plc-01 Place GEOID format (7-digit) and key uniqueness | place_wui_by_vintage | CRITICAL | PASS | 0 malformed place GEOIDs; 0 duplicate (GEOID, vintage) rows |
| plc-02 Place WUI area within [0, place area] | place_wui_by_vintage | CRITICAL | PASS | 0 places with WUI > place area; 0 negative |
| plc-04 Place tables contain no county GEOIDs (separation) | place_outputs | CRITICAL | PASS | 0 5-digit (county-style) GEOIDs found in place outputs |
| plc-03 Place exposure: baseline nulls, burn hierarchy, vintage mapping | place_wui_fire_exposure | CRITICAL | PASS | pre-baseline null exposure: True; 0 burn-hierarchy violations; 0 vintage mismatches |
| geo-05 Stage 2 spatial artifacts carry expected CRS (no geographic area math) | stage2 spatial outputs | CRITICAL | PASS | all artifacts match expected EPSG |


### Burn-area inequalities (wf-01, wf-02, wf-05)

These checks do **not** require total burned area ≤ WUI area. Wildfire can burn non-WUI land, so `burned_area_km2` may exceed `wui_area_km2`.

- wf-01: annual `wui_burned_area_km2` ≤ `burned_area_km2` (the WUI portion of that year's burn).
- wf-02: annual `wui_burned_area_km2` ≤ vintage-matched `wui_area_km2`. This is a single-year quantity. Summing it across years is cumulative exposure and can exceed WUI area where land reburns; that sum is not tested here and is not a percent of unique coverage.
- wf-05: within each vintage period, `unique_wui_burned_footprint_km2` ≤ that period's `wui_area_km2`. The period's cumulative `wui_burned_area_km2` (per-event sum) is a different column and is allowed to exceed the unique footprint.

