# Stage 2 methodological audit

_2026-09-30. Existing outputs and targeted calculations only. The Nature raster was not reprocessed. No models were fit._

## 1. WUI-burn validation inequalities

The completion note that said “WUI burn ≤ total burn ≤ WUI area” chained two different comparisons. The code does not do that.

`src/validate_stage2.py` tests:

| Check | Inequality | What it does not test |
|---|---|---|
| wf-01 | annual `wui_burned_area_km2` ≤ `burned_area_km2` | |
| wf-02 | annual `wui_burned_area_km2` ≤ vintage-matched `wui_area_km2` | |
| wf-05 | period `unique_wui_burned_footprint_km2` ≤ that period’s `wui_area_km2` | period cumulative `wui_burned_area_km2` ≤ WUI area |

There is no check that `burned_area_km2` ≤ `wui_area_km2`. On the built county-year table, **1,876** aligned county-years have total burn larger than vintage WUI area (non-WUI land burned). **0** rows have annual WUI burn above total burn or above vintage WUI area. Maximum `pct_wui_burned_in_year` is **95.88%**.

wf-02 applies to a single year. Adding those annual values across years is cumulative exposure. One county’s 1990–2024 cumulative WUI burn divided by its **2020** WUI area is **104.8%**. That ratio is not a column and is not a unique-coverage percent.

Within vintage periods, cumulative WUI burn exceeds the unique footprint on **135** county-period rows (max gap **55.6 km²**; sum of positive gaps **189 km²**). The published percent of that footprint, `pct_wui_burned_footprint_in_period`, stays ≤ 100%. The map quantity `pct_wui_burned_weighted` (sum of period unique footprints / sum of period WUI areas) has a maximum of **24.5%**.

## 2. Area denominators

`pct_county_wui` uses **A**: full legal county area (`county_area_km2`, EPSG:5070 polygon, land + water). It does not use Census land area.

Both are now stored:

- `county_land_area_km2` = `ALAND / 1e6`
- `county_water_area_km2` = `AWATER / 1e6`
- `pct_total_county_area_wui` — alias of `pct_county_wui` (legal polygon)
- `pct_county_land_area_wui` — WUI ÷ ALAND

SILVIS 2020 max legal-polygon WUI share is **91.05%**. Across all vintages the land-area share max is **92.44%** (0 counties above 100%).

**Which denominator to use.** Land area is the better denominator for “what share of the county’s land is WUI,” because water is not interface or intermix and a legal-polygon percentage understates that share in coastal and Great Lakes counties. The legal-polygon percentage answers “what share of the county jurisdiction is WUI” and matches Stage 1 burn percentages. Neither column was dropped.

Census Places: `place_area_km2` is the EPSG:5070 legal polygon. `ALAND`/`AWATER` were in the TIGER files and had been dropped on write; they are restored as `place_land_area_km2` and `place_water_area_km2`, with `pct_total_place_area_wui` and `pct_place_land_area_wui`. Legal-polygon place WUI share max is **100%** (floating-point dust above 100 on one row). Land-area place WUI share exceeds 100% on **1,689** place-vintage rows (max **110.8%**), for the same reason as the Virginia cities below: WUI geometry can cover the water part of the legal polygon, so dividing by ALAND alone goes over 100%. Do not cap it.

The 1990 and 2020 choropleths were redrawn from `pct_county_land_area_wui`. Their previous title said “land area” while the values were the legal polygon.

## 3. Nature coverage below 90% of legal county area

**292** counties have nominal classified area (classes 1–8) < 90% of `county_area_km2`.

This is not a generic boundary effect.

- Pearson correlation of that coverage with Census water fraction is **−0.994**.
- Median water fraction in the 292 is **30.1%**, versus **1.0%** in the other counties.
- **278 of 292** rise to ≥ 90% of Census **land** area. **14** stay below 90% of ALAND (15 after the EQUI7 ground correction, which slightly lowers nominal coverage).
- EQUI7 area scale is almost uncorrelated with legal-area coverage (r ≈ 0.02) and cannot explain under-coverage: the projection makes nominal pixels **larger** than ground area, which pushes coverage up, not down.
- County-covered pixels never take VRT NoData **255**. They do take value **0**, which this pipeline excludes from `nature_classified_area_km2`. In the 14 counties, class-0 area exceeds Census water, so part of Census land is raster value 0.

The 14 are in `outputs/tables/nature_coverage_land_below_90.csv`. Twelve are delta, estuary, or barrier-island counties (Louisiana, South Carolina, Georgia, Virginia Eastern Shore) where Census land includes marsh the 10 m product leaves as 0. The other two are Davis County, Utah (Great Salt Lake fringe; class 0 928 km² vs water 804 km²) and Day County, South Dakota (class 0 496 km² vs water 162 km²). Those two are not explained by AWATER alone. The worst legal-area coverages are Keweenaw County, Michigan (9.0% of the legal polygon, 99.8% of ALAND) and other Great Lakes counties whose “missing” area is water.

Worst 20 by legal-area coverage: `outputs/tables/nature_coverage_worst20.csv`.

## 4. Seven Virginia independent cities above 100%

Nominal Nature WUI ÷ legal polygon area is 100.4–101.9% for Charlottesville, Lexington, Winchester, Harrisonburg, Salem, Galax, and Martinsville. Detail: `outputs/tables/nature_va_independent_cities.csv`.

| GEOID | City | Nominal WUI / legal area | Ground-corrected WUI / legal area | Ground-corrected WUI / ALAND |
|---|---|---:|---:|---:|
| 51540 | Charlottesville | 101.87% | 99.96% | 100.13% |
| 51678 | Lexington | 101.84% | 99.98% | 101.02% |
| 51840 | Winchester | 101.64% | 99.88% | 100.10% |
| 51660 | Harrisonburg | 101.53% | 99.72% | 100.02% |
| 51775 | Salem | 100.84% | 98.97% | 99.69% |
| 51640 | Galax | 100.71% | 98.82% | 99.30% |
| 51690 | Martinsville | 100.42% | 98.45% | 98.92% |

Cause, in order:

1. **Not an equal-area grid.** The NA mosaic CRS is EPSG:27705, azimuthal equidistant (center 52°N, 97.5°W). The pipeline had described it as equal-area and multiplied pixel counts by 100 m². County polygon area in EPSG:5070 divided by the same polygon in EPSG:27705 is **0.956–0.999** across CONUS (median **0.985**). In these cities the scale is about **0.981**, which removes the entire excess versus the legal polygon. After that correction, **0** of 3,109 counties have ground-corrected classified area above the legal polygon (maximum **99.98%**).
2. **Rasterization is center-in-polygon** (`all_touched=False`), which does not add a ring of outside pixels. Pixel size is the nominal 10 m cell (`0.0001` km² on the EQUI7 map).
3. **Denominator.** These cities are almost entirely WUI, including pixels over the small Census water sliver. Ground-corrected WUI ÷ ALAND therefore sits about one water-area above 100% for Lexington (water 0.068 km²; ground WUI exceeds land by ~0.066 km²). That is the land denominator, not area outside the city polygon.

Nominal areas are unchanged. Ground-corrected columns are `nature_wui_ground_km2` and `nature_classified_ground_km2`. Nothing was clipped to 100%.

Vector areas used for the denominator are EPSG:5070. The raster CRS during extraction was EPSG:27705.

## 5. Cumulative vs unique historical WUI burn

Two different quantities already exist inside each vintage period:

- **Cumulative:** `wui_burned_area_km2` on the county-year table, and the same name on the period table. Per-event intersection areas are summed. Reburned WUI counts again.
- **Unique within a period:** `unique_wui_burned_footprint_km2`. Dissolved union of vintage-matched fire ∩ WUI pieces. Reburned land counts once. This is what `pct_wui_burned_footprint_in_period` and the “percent of WUI burned” map use. Neither exceeds 100%.

The historical burned-area **map** (`wui_burned_area_historical.png`) sums annual `wui_burned_area_km2`. Its title says cumulative perimeters. It is not a percent.

An all-years unique footprint is feasible from `fires_wui_intersections.geoparquet` (28,982 pieces, 45 MB). Vintage-aligned union (keep a piece only when `wui_class_{vintage(year)}` is non-null) took **9.2 s** and produced 788 counties. Sum of cumulative vintage-aligned WUI burn: **7,739 km²**. Sum of the unique footprint: **7,126 km²**. Unique never exceeds cumulative. Cumulative exceeds unique in **177** counties (max ratio **1.80**). Union of every piece regardless of vintage is larger (8,960 km²) because fragments that are WUI in some other decade are included; that union is not the exposure metric.

County table: `outputs/tables/wui_burn_cumulative_vs_unique_by_county.csv`.

Do not divide either all-years total by a single vintage’s WUI area and call it “percent of WUI historically burned.” That ratio exceeds 100% for at least one county even though each within-year percent does not.

## 6. Panel completeness

`county_year_wui_fire` has **9,671** rows, **1,408** counties, years 1984–2024, **0** rows with `fire_count == 0` or `burned_area_km2 == 0`. It is the Stage 1 fire-positive county-years plus vintage-matched WUI. A complete CONUS panel would be 3,109 × 41 = **127,469** rows.

Acceptable for this spatial product. Stage 3 modeling needs an explicit balanced panel with true zero fire outcomes. Missing 1984–1989 WUI fields mean no vintage, not zero exposure, and must stay null.

## Code and table changes

- Validation logic was already the two inequalities. The summary written by `validate_stage2.py` now states that total burn ≤ WUI area is not required.
- `src/area_denominators.py` adds land/water denominators and the EQUI7 ground scale without rereading tiles.
- Those columns were written onto the county, SILVIS, Nature, county-year, and place tables (parquet and CSV).
- Nature module text no longer calls EQUI7 equal-area.
- 1990/2020 WUI maps use Census land area and say so.
- Regression tests: total burn above WUI area still passes wf-01/wf-02; EQUI7 scale < 1 in Virginia and closer to 1 in North Dakota.

## Tests and validation

- `pytest tests/`: **113 passed**.
- Stage 2 validation rerun after the column additions: **26 checks, 25 PASS, 1 WARN (nat-02), 0 FAIL**, exit 0. The combined report is 25 Stage 1 checks + these 26 (51 rows, no duplicated Stage 2 block).

## Commit recommendation

Stage 2 is scientifically ready to commit, with these caveats left visible rather than clipped:

- Nature nominal areas are map-pixel areas. Use `nature_*_ground_km2` when comparing to EPSG:5070 county area. The SILVIS–Nature gap (about 0.75 M vs 1.15 M km²) is not this 1–4% scale effect.
- 14 counties still have Nature classes 1–8 covering under 90% of Census land, because raster value 0 covers land (marsh and two inland cases).
- Do not treat `county_year_wui_fire` as a modeling panel.
- Do not label a multi-year sum of WUI burn, divided by one vintage’s WUI area, as a percent of WUI historically burned.
