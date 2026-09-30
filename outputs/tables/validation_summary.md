# Stage 1 Validation Summary

- Checks run: 25
- PASS: 24
- WARN: 0 (investigated; see details)
- FAIL: 0

| Check | Dataset | Severity | Status | Details |
|---|---|---|---|---|
| prv-01 Provenance manifest present and complete | provenance | CRITICAL | PASS | All datasets recorded with url/retrieval/sha256/size/archive_valid. |
| cnt-01 GEOID uniqueness in county source | counties | CRITICAL | PASS | 3109 unique GEOIDs across 3109 rows. |
| cnt-02 GEOID format (5-digit STATEFP+COUNTYFP) | counties | CRITICAL | PASS | 0 malformed GEOIDs (all 5-digit numeric). |
| cnt-03 County area plausibility (CONUS bounds) | counties | CRITICAL | PASS | Areas range 5.3-52,073.4 km2; 0 outside [5.0, 55000.0] |
| cnt-04 CONUS state coverage in county source | counties | CRITICAL | PASS | 49 distinct STATEFPs present; missing CONUS FIPS: none |
| cnt-05 Virginia independent cities present | counties | CRITICAL | PASS | 38 VA county-equivalents with COUNTYFP>=500 out of 133 VA rows; all NAMELSAD end with 'city': True |
| geo-01 CRS defined on all spatial artifacts | all spatial outputs | CRITICAL | PASS | 3 spatial artifacts; missing CRS: none |
| geo-02 CONUS artifacts projected in EPSG:5070 | area-math spatial outputs | CRITICAL | PASS | Non-5070 artifacts: none |
| geo-04 MTBS fires artifact in a geographic CRS | mtbs_fires | CRITICAL | PASS | fires CRS epsg=4269; geographic=True |
| geo-03 Valid non-empty geometries in artifacts | all spatial outputs | CRITICAL | PASS | Invalid+empty per artifact: {'counties_conus': 0, 'mtbs_wildfires_conus': 0, 'fires_county_intersections': 0} |
| mtb-01 MTBS year range within configured completed years | mtbs_fires | CRITICAL | PASS | Processed year range 1984-2024; expected within 1984-2024; 0 rows outside |
| mtb-02 No missing FIRE_ID | mtbs_fires | CRITICAL | PASS | 0 rows missing FIRE_ID |
| mtb-03 FIRE_ID uniqueness after preprocessing | mtbs_fires | CRITICAL | PASS | 15540 unique FIRE_IDs over 15540 rows |
| mtb-04 Fire type domain matches configuration | mtbs_fires | CRITICAL | PASS | fire_type values present: ['Wildfire', 'Wildland Fire Use']; unexpected: none |
| agg-01 No duplicate GEOID x year rows | county_year | CRITICAL | PASS | 9671 unique county-years over 9671 rows |
| agg-02 No duplicate GEOID rows in all-years summary | county_all_years | CRITICAL | PASS | 3109 unique GEOIDs over 3109 rows |
| agg-03 No negative area values | all aggregates | CRITICAL | PASS | Negative areas by table: none |
| agg-04 Annual pct burned bounds (0..100+) with >100 investigation | county_year | CRITICAL | PASS | 0 negative pct (impossible); 0 county-years with pct>100 (investigate) |
| agg-05 Unique footprint pct within [0,100] | county_all_years | CRITICAL | PASS | 0 counties with footprint pct outside [0,100] |
| agg-06 Unique footprint <= cumulative burned area | county_all_years | CRITICAL | PASS | 0 counties where footprint exceeds cumulative area |
| agg-09 Footprint > 0 wherever fires occurred | county_all_years | CRITICAL | PASS | 1408 counties with fires; 0 have zero footprint (bug indicator) |
| agg-07 Cumulative burned area vs county area (reburn sanity) | county_all_years | WARNING | INFO | 3 counties with cumulative > county area (reburn — legitimate); max ratio 1.30; 0 counties above 20x (would indicate a bug) |
| agg-08 Count/area internal consistency | aggregates | CRITICAL | PASS | sum(county_year fire_count)=19,009 >= sum(all_years unique_fire_count)=19,009; zero-count-with-area rows=0; count>0-with-zero-area rows=0 |
| ovl-01 Intersection fragment <= county area | intersections | CRITICAL | PASS | 0 fragments larger than their county |
| ovl-02 Event count accounting (raw -> filters -> retained) | mtbs_fires | CRITICAL | PASS | raw=30,933 \| by FIRE_TYPE={'Other': 4311, 'Prescribed Fire': 9639, 'Wildfire': 16780, 'Wildland Fire Use': 203} \| retained (['Wildfire', 'Wildland Fire Use'], CONUS, 1984-2024)=15,540 |
